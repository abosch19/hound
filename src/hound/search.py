"""Búsqueda híbrida con filtrado por permisos.

Los vectores de todos los fragmentos se cargan en memoria (numpy, por bloques) y se
comparan por fuerza bruta con la consulta. En paralelo se consulta el índice FTS5 de
palabras clave, y las dos listas se combinan con Reciprocal Rank Fusion.

Seguridad: la máscara de ficheros que el usuario puede leer se aplica ANTES de ordenar,
tanto en la parte vectorial como en la de palabras clave. Un fichero sin permiso nunca
llega a la lista de candidatos, así que ni su nombre ni sus fragmentos pueden aparecer.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass

import numpy as np

from . import db
from .config import Config
from .embed import Embedder
from .extract import KINDS
from .paths import display_path, is_under, parse_user_path
from .security import SecurityProvider

log = logging.getLogger("hound.search")

BLOCK_ROWS = 32768
KIND_CODES = {k: i for i, k in enumerate(["other", *KINDS.keys()])}
KIND_ALIASES = {
    "pdf": "pdf", "word": "word", "doc": "word", "docx": "word", "documento": "word",
    "excel": "excel", "xls": "excel", "xlsx": "excel", "hoja": "excel", "csv": "excel",
    "powerpoint": "powerpoint", "ppt": "powerpoint", "pptx": "powerpoint", "presentación": "powerpoint",
    "email": "email", "correo": "email", "msg": "email", "eml": "email",
    "text": "text", "texto": "text", "txt": "text",
    "image": "image", "imagen": "image", "foto": "image", "jpg": "image", "png": "image",
    "cad": "cad", "autocad": "cad", "dwg": "cad", "dxf": "cad", "plano": "cad",
    "other": "other", "otro": "other",
}

STOPWORDS = set("""
a al algo ante con contra de del desde donde durante e el en entre esta este esto hacia hasta la las le les lo los
mas me mi mis muy ni no o para pero por que se sin sobre su sus te tu un una unas unos y ya the of and to in for
""".split())


class AccessDenied(Exception):
    """El fichero no existe o el usuario no puede verlo (no se distingue a propósito)."""


@dataclass
class User:
    name: str
    sid: str


class _VecBlock:
    __slots__ = ("vecs", "seg_ids", "file_idx", "alive")

    def __init__(self, vecs: np.ndarray, seg_ids: np.ndarray, file_idx: np.ndarray):
        self.vecs = vecs
        self.seg_ids = seg_ids
        self.file_idx = file_idx
        self.alive = np.ones(len(seg_ids), dtype=bool)


class _FileTable:
    """Metadatos de ficheros en arrays con índice estable (se añaden, nunca se reordenan)."""

    def __init__(self):
        self.idx: dict[int, int] = {}
        self.n = 0
        cap = 1024
        self.ids = np.zeros(cap, dtype=np.int64)
        self.sd = np.full(cap, -1, dtype=np.int64)
        self.mtime = np.zeros(cap, dtype=np.float64)
        self.kind = np.zeros(cap, dtype=np.int8)
        self.alive = np.zeros(cap, dtype=bool)
        self.paths: list[str] = []

    def _grow(self):
        cap = len(self.ids) * 2
        for name in ("ids", "sd", "mtime", "kind", "alive"):
            old = getattr(self, name)
            new = np.zeros(cap, dtype=old.dtype) if name != "sd" else np.full(cap, -1, dtype=old.dtype)
            new[: len(old)] = old
            setattr(self, name, new)

    def upsert(self, fid: int, path: str, sd: int | None, mtime: float, kind: str) -> int:
        i = self.idx.get(fid)
        if i is None:
            if self.n == len(self.ids):
                self._grow()
            i = self.n
            self.n += 1
            self.idx[fid] = i
            self.paths.append(path)
            self.ids[i] = fid
        else:
            self.paths[i] = path
        self.sd[i] = -1 if sd is None else sd
        self.mtime[i] = mtime
        self.kind[i] = KIND_CODES.get(kind, 0)
        self.alive[i] = True
        return i

    def remove(self, fid: int):
        i = self.idx.get(fid)
        if i is not None:
            self.alive[i] = False


def _fold_char(c: str) -> str:
    d = unicodedata.normalize("NFD", c)
    return (d[0] if d else c).lower()[:1] or c


def _fold(s: str) -> str:
    """Minúsculas y sin tildes, conservando la longitud (para resaltar coincidencias)."""
    return "".join(_fold_char(c) for c in s)


def query_terms(query: str) -> list[str]:
    words = re.findall(r"\w+", _fold(query))
    return [w for w in dict.fromkeys(words) if len(w) >= 2 and w not in STOPWORDS]


def fts_query(query: str) -> str:
    return " OR ".join(f'"{w}"*' for w in query_terms(query))


def excerpt(text: str, terms: list[str], width: int = 360) -> str:
    """Fragmento centrado en la primera coincidencia, con las coincidencias en **negrita**."""
    folded = _fold(text)
    hits = []
    for t in terms:
        for m in re.finditer(r"\b" + re.escape(t), folded):
            hits.append((m.start(), m.end()))
    hits.sort()
    start = 0
    if hits and len(text) > width:
        start = max(0, hits[0][0] - width // 3)
        sp = text.rfind(" ", 0, start)
        start = sp + 1 if sp > start - 40 else start
    end = min(len(text), start + width)
    if end < len(text):
        sp = text.rfind(" ", start, end)
        end = sp if sp > start + width // 2 else end
    out, pos = [], start
    for s, e in hits:
        if s < pos or e > end:
            continue
        # extiende hasta el final de la palabra
        while e < end and (text[e].isalnum()):
            e += 1
        out.append(text[pos:s])
        out.append("**" + text[s:e] + "**")
        pos = e
    out.append(text[pos:end])
    snippet = "".join(out).strip().replace("\n", " ")
    return ("…" if start > 0 else "") + re.sub(r"\s+", " ", snippet) + ("…" if end < len(text) else "")


def _parse_date(s: str | None, end: bool = False) -> float | None:
    if not s:
        return None
    d = dt.date.fromisoformat(s.strip()[:10])
    t = dt.datetime.combine(d, dt.time.max if end else dt.time.min)
    return t.timestamp()


def _fmt_date(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


class SearchIndex:
    def __init__(self, cfg: Config, embedder: Embedder, security: SecurityProvider):
        self.cfg = cfg
        self.embedder = embedder
        self.security = security
        self.dtype = np.float16 if cfg.search.memory_dtype == "float16" else np.float32
        self._local = threading.local()
        self._lock = threading.RLock()
        self.blocks: list[_VecBlock] = []
        self.files = _FileTable()
        self.last_seg = 0
        self.last_seq = -1
        self.last_tomb = 0
        self.files_version = 0
        self._sd_cache: dict[int, bytes] = {}
        self._perm_cache: dict[tuple[str, int], tuple[bool, float]] = {}
        self._mask_cache: dict[str, tuple[np.ndarray, int, float]] = {}
        self._share_sd: bytes | None = None
        self._share_loaded = False
        self._stop = threading.Event()
        self.refresh()

    # --- conexión y recarga -------------------------------------------------------

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._local.conn = db.connect(self.cfg.db_path, readonly=True)
        return c

    def refresh(self) -> None:
        """Carga lo nuevo del índice: ficheros cambiados, fragmentos nuevos y retirados."""
        with self._lock:
            c = self.conn()
            seq = int(db.get_meta(c, "seq", "0"))
            epoch = db.get_meta(c, "epoch", "")
            if epoch != getattr(self, "epoch", epoch):
                log.info("El índice se ha rehecho: recarga completa")
                self.blocks, self.files = [], _FileTable()
                self.last_seg, self.last_seq, self.last_tomb = 0, -1, 0
                self._sd_cache.clear()
                self._perm_cache.clear()
            self.epoch = epoch
            model = db.get_meta(c, "model")
            self.model_ok = model is None or model == self.embedder.name
            if not self.model_ok:
                log.error("El índice se creó con el modelo %s y la configuración usa %s: ejecuta "
                          "'hound reindex --all'", model, self.embedder.name)
                return
            if seq == self.last_seq:
                return
            t0 = time.monotonic()
            changed_files = 0
            for r in c.execute("SELECT id, path, sd_id, mtime, kind FROM files WHERE seq > ?", (self.last_seq,)):
                self.files.upsert(r["id"], r["path"], r["sd_id"], r["mtime"], r["kind"])
                changed_files += 1
            # retirados: fragmentos de ficheros reindexados o borrados
            tombs: dict[int, int] = {}
            for r in c.execute("SELECT id, file_id, upto_seg FROM tombstones WHERE id > ?", (self.last_tomb,)):
                tombs[r["file_id"]] = max(tombs.get(r["file_id"], 0), r["upto_seg"])
                self.last_tomb = max(self.last_tomb, r["id"])
            if tombs:
                self._apply_tombstones(tombs)
            live_ids = None
            if tombs:
                live_ids = {r[0] for r in c.execute(
                    f"SELECT id FROM files WHERE id IN ({','.join('?' * len(tombs))})", list(tombs))}
                for fid in tombs:
                    if fid not in live_ids:
                        self.files.remove(fid)
            # fragmentos nuevos
            new = 0
            rows = c.execute("SELECT id, file_id, vec FROM segments WHERE id > ? ORDER BY id", (self.last_seg,))
            while True:
                part = rows.fetchmany(BLOCK_ROWS)
                if not part:
                    break
                ids = np.fromiter((r[0] for r in part), dtype=np.int64, count=len(part))
                fidx = np.fromiter((self.files.idx.get(r[1], -1) for r in part), dtype=np.int64, count=len(part))
                vecs = np.frombuffer(b"".join(r[2] for r in part), dtype=np.float16).reshape(len(part), -1)
                block = _VecBlock(vecs.astype(self.dtype), ids, fidx)
                block.alive &= fidx >= 0
                self.blocks.append(block)
                self.last_seg = int(ids[-1])
                new += len(part)
            self.last_seq = seq
            self.files_version += 1
            self._mask_cache.clear()
            if new or changed_files or tombs:
                log.info("Índice recargado: %d ficheros cambiados, %d fragmentos nuevos, %d retirados (%.2f s)",
                         changed_files, new, len(tombs), time.monotonic() - t0)

    def _apply_tombstones(self, tombs: dict[int, int]) -> None:
        keys = np.array(sorted(tombs), dtype=np.int64)
        upto = np.array([tombs[k] for k in keys], dtype=np.int64)
        fid_by_idx = self.files.ids
        for b in self.blocks:
            valid = b.file_idx >= 0
            fids = np.where(valid, fid_by_idx[np.clip(b.file_idx, 0, None)], -1)
            pos = np.searchsorted(keys, fids)
            pos = np.clip(pos, 0, len(keys) - 1)
            hit = (keys[pos] == fids) & (b.seg_ids <= upto[pos])
            if hit.any():
                b.alive &= ~hit

    def start_refresher(self, interval: float = 10.0) -> None:
        def loop():
            while not self._stop.wait(interval):
                try:
                    self.refresh()
                except Exception as exc:  # noqa: BLE001
                    log.warning("Error recargando el índice: %s", exc)

        threading.Thread(target=loop, name="hound-refresh", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    # --- permisos -----------------------------------------------------------------

    def _sd(self, sd_id: int) -> bytes | None:
        sd = self._sd_cache.get(sd_id)
        if sd is None:
            row = self.conn().execute("SELECT sd FROM security_descriptors WHERE id = ?", (sd_id,)).fetchone()
            if row is None:
                return None
            sd = self._sd_cache[sd_id] = bytes(row[0])
        return sd

    def _allowed(self, user: User, sd_id: int, ttl: float) -> bool:
        now = time.monotonic()
        hit = self._perm_cache.get((user.sid, sd_id))
        if hit and now - hit[1] < ttl:
            return hit[0]
        sd = self._sd(sd_id)
        try:
            ok = bool(sd) and self.security.check(user.sid, sd)
        except Exception as exc:  # noqa: BLE001 — ante la duda, denegar
            log.warning("Comprobación de permisos fallida para %s: %s", user.name, exc)
            ok = False
        self._perm_cache[(user.sid, sd_id)] = (ok, now)
        return ok

    def user_mask(self, user: User) -> np.ndarray:
        """Ficheros (por índice interno) que el usuario puede leer en el disco."""
        n = self.files.n
        ttl = self.cfg.permissions.cache_minutes * 60
        cached = self._mask_cache.get(user.sid)
        if cached and cached[1] == self.files_version and time.monotonic() - cached[2] < ttl:
            return cached[0]
        alive = self.files.alive[:n].copy()
        if not self.security.enforced:
            mask = alive
        else:
            mask = np.zeros(n, dtype=bool)
            share_ok = True
            if self.cfg.share.name:
                if not self._share_loaded:
                    try:
                        self._share_sd = self.security.share_sd(self.cfg.share.name)
                        self._share_loaded = True
                    except Exception as exc:  # noqa: BLE001 — sin poder leerlos, se deniega
                        log.error("No se pueden leer los permisos del recurso compartido: %s", exc)
                        share_ok = False
                if share_ok and self._share_sd is not None:
                    try:
                        share_ok = self.security.check(user.sid, self._share_sd)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("Permisos del recurso compartido para %s: %s", user.name, exc)
                        share_ok = False
            if share_ok:
                sds = self.files.sd[:n]
                present = np.unique(sds[alive])
                allowed = np.zeros(int(present.max()) + 2 if len(present) else 1, dtype=bool)
                for sd_id in present:
                    if sd_id >= 0:
                        allowed[sd_id + 1] = self._allowed(user, int(sd_id), ttl)
                # sd = -1 (sin descriptor) cae en allowed[0], que siempre es False
                mask = alive & allowed[np.clip(sds + 1, 0, len(allowed) - 1)]
        self._mask_cache[user.sid] = (mask, self.files_version, time.monotonic())
        return mask

    # --- filtros ------------------------------------------------------------------

    def filter_mask(self, user: User, folder: str | None = None, file_type: str | None = None,
                    date_from: str | None = None, date_to: str | None = None) -> np.ndarray:
        n = self.files.n
        mask = self.user_mask(user).copy()
        if folder:
            rel = parse_user_path(folder, self.cfg)
            if rel:
                paths = self.files.paths
                sel = np.fromiter((is_under(paths[i], rel) for i in range(n)), dtype=bool, count=n)
                mask &= sel
        if file_type:
            kinds = set()
            for t in re.split(r"[,\s]+", file_type.lower().strip(".")):
                if t:
                    k = KIND_ALIASES.get(t.lstrip("."))
                    if k is None:
                        raise ValueError(f"Tipo de fichero desconocido: {t}. Usa: {', '.join(sorted(set(KIND_ALIASES.values())))}")
                    kinds.add(KIND_CODES[k])
            mask &= np.isin(self.files.kind[:n], list(kinds))
        lo, hi = _parse_date(date_from), _parse_date(date_to, end=True)
        if lo is not None:
            mask &= self.files.mtime[:n] >= lo
        if hi is not None:
            mask &= self.files.mtime[:n] <= hi
        return mask

    # --- búsqueda -------------------------------------------------------------------

    def _vector_candidates(self, qv: np.ndarray, fmask: np.ndarray, k: int) -> list[tuple[int, float]]:
        q = qv.astype(np.float32)
        best_ids: list[np.ndarray] = []
        best_scores: list[np.ndarray] = []
        for b in list(self.blocks):
            fi = b.file_idx
            ok = b.alive & (fi >= 0)
            ok[ok] = fmask[fi[ok]]
            if not ok.any():
                continue
            vecs = b.vecs if b.vecs.dtype == np.float32 else b.vecs.astype(np.float32)
            scores = vecs @ q
            scores[~ok] = -np.inf
            kk = min(k, int(ok.sum()))
            top = np.argpartition(-scores, kk - 1)[:kk]
            best_ids.append(b.seg_ids[top])
            best_scores.append(scores[top])
        if not best_ids:
            return []
        ids = np.concatenate(best_ids)
        scores = np.concatenate(best_scores)
        order = np.argsort(-scores)[:k]
        return [(int(ids[i]), float(scores[i])) for i in order]

    def _fts_candidates(self, query: str, fmask: np.ndarray, k: int) -> list[int]:
        q = fts_query(query)
        if not q:
            return []
        out = []
        try:
            rows = self.conn().execute(
                "SELECT f.rowid, s.file_id FROM segments_fts f JOIN segments s ON s.id = f.rowid "
                "WHERE segments_fts MATCH ? ORDER BY bm25(segments_fts, 1.0, 0.6) LIMIT ?", (q, k * 20))
            for seg_id, file_id in rows:
                i = self.files.idx.get(file_id)
                if i is not None and i < len(fmask) and fmask[i]:
                    out.append(seg_id)
                    if len(out) >= k:
                        break
        except sqlite3.OperationalError as exc:
            log.warning("Consulta FTS fallida (%s): %s", q, exc)
        return out

    def search(self, user: User, query: str, folder: str | None = None, file_type: str | None = None,
               date_from: str | None = None, date_to: str | None = None, limit: int = 10) -> list[dict]:
        query = (query or "").strip()
        if not query:
            raise ValueError("La consulta está vacía")
        limit = max(1, min(int(limit or 10), 50))
        if not getattr(self, "model_ok", True):
            raise ValueError("El índice se creó con otro modelo de embeddings. "
                             "Un administrador debe ejecutar 'hound reindex --all' en el servidor.")
        fmask = self.filter_mask(user, folder, file_type, date_from, date_to)
        if not fmask.any():
            return []
        k = self.cfg.search.candidates
        vec = self._vector_candidates(self.embedder.embed_query(query), fmask, k)
        fts = self._fts_candidates(query, fmask, k)
        rrf_k = self.cfg.search.rrf_k
        fused: dict[int, float] = {}
        sim: dict[int, float] = {}
        for rank, (sid, s) in enumerate(vec):
            fused[sid] = fused.get(sid, 0.0) + 1.0 / (rrf_k + rank + 1)
            sim[sid] = s
        for rank, sid in enumerate(fts):
            fused[sid] = fused.get(sid, 0.0) + 1.0 / (rrf_k + rank + 1)
        if not fused:
            return []
        ranked = sorted(fused.items(), key=lambda x: -x[1])
        # Agrupa por fichero: el mejor fragmento de cada uno.
        c = self.conn()
        seg_rows = {}
        ids = [s for s, _ in ranked]
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            for r in c.execute(f"SELECT id, file_id, location, text, ord FROM segments WHERE id IN "
                               f"({','.join('?' * len(part))})", part):
                seg_rows[r["id"]] = r
        results: dict[int, dict] = {}
        terms = query_terms(query)
        max_score = 2.0 / (rrf_k + 1)
        for sid, score in ranked:
            r = seg_rows.get(sid)
            if r is None:
                continue
            fi = self.files.idx.get(r["file_id"])
            if fi is None or not fmask[fi]:  # doble comprobación
                continue
            res = results.get(r["file_id"])
            if res is not None:
                res["matches"] += 1
                continue
            if len(results) >= limit:
                continue
            rel = self.files.paths[fi]
            results[r["file_id"]] = {
                "path": display_path(rel, self.cfg),
                "name": rel.rpartition("/")[2],
                "type": next((k for k, v in KIND_CODES.items() if v == self.files.kind[fi]), "other"),
                "location": r["location"],
                "excerpt": excerpt(r["text"], terms),
                "modified": _fmt_date(self.files.mtime[fi]),
                "score": round(min(1.0, score / max_score), 3),
                "similarity": round(sim[sid], 3) if sid in sim else None,
                "matches": 1,
            }
        return list(results.values())

    # --- lectura y navegación -------------------------------------------------------

    def _check_file(self, user: User, path: str) -> tuple[int, int, str]:
        rel = parse_user_path(path, self.cfg)
        row = self.conn().execute("SELECT id, path FROM files WHERE path = ?", (rel,)).fetchone()
        if row is None:
            raise AccessDenied(path)
        fi = self.files.idx.get(row["id"])
        if fi is None or not self.user_mask(user)[fi]:
            raise AccessDenied(path)
        return row["id"], fi, row["path"]

    def read_document(self, user: User, path: str, start: int = 0, end: int | None = None,
                      max_chars: int = 30000) -> dict:
        fid, fi, rel = self._check_file(user, path)
        c = self.conn()
        total = c.execute("SELECT count(*) FROM segments WHERE file_id = ? AND ord > 0", (fid,)).fetchone()[0]
        start = max(0, int(start or 0))
        end = total if end is None else max(start, min(int(end), total))
        parts, used, last = [], 0, start
        for r in c.execute("SELECT ord, location, text FROM segments WHERE file_id = ? AND ord > 0 ORDER BY ord "
                           "LIMIT ? OFFSET ?", (fid, end - start, start)):
            if parts and used + len(r["text"]) > max_chars:
                break
            parts.append({"index": r["ord"] - 1, "location": r["location"], "text": r["text"]})
            used += len(r["text"])
            last = r["ord"]
        status = c.execute("SELECT status, error FROM files WHERE id = ?", (fid,)).fetchone()
        return {
            "path": display_path(rel, self.cfg),
            "modified": _fmt_date(self.files.mtime[fi]),
            "total_segments": total,
            "segments": parts,
            "next_start": last if last < total and parts else None,
            "note": status["error"] if status and status["error"] else None,
        }

    def list_folder(self, user: User, path: str = "", limit: int = 500) -> dict:
        rel = parse_user_path(path, self.cfg)
        mask = self.user_mask(user)
        n = self.files.n
        folders: dict[str, int] = {}
        files = []
        depth = len(rel.split("/")) if rel else 0
        for i in np.flatnonzero(mask[:n]):
            p = self.files.paths[i]
            if not is_under(p, rel) or p.lower() == rel.lower():
                continue
            parts = p.split("/")
            if len(parts) == depth + 1:
                files.append({"name": parts[-1], "path": display_path(p, self.cfg),
                              "modified": _fmt_date(self.files.mtime[i])})
            else:
                folders[parts[depth]] = folders.get(parts[depth], 0) + 1
        files.sort(key=lambda f: f["name"].lower())
        return {
            "path": display_path(rel, self.cfg),
            "folders": [{"name": k, "path": display_path(f"{rel}/{k}" if rel else k, self.cfg), "files": v}
                        for k, v in sorted(folders.items(), key=lambda x: x[0].lower())],
            "files": files[:limit],
            "truncated": len(files) > limit,
        }

    def recent_files(self, user: User, limit: int = 20, folder: str | None = None) -> list[dict]:
        limit = max(1, min(int(limit or 20), 200))
        mask = self.filter_mask(user, folder=folder)
        idx = np.flatnonzero(mask)
        if not len(idx):
            return []
        order = idx[np.argsort(-self.files.mtime[idx])[:limit]]
        return [{"name": self.files.paths[i].rpartition("/")[2],
                 "path": display_path(self.files.paths[i], self.cfg),
                 "modified": _fmt_date(self.files.mtime[i])} for i in order]
