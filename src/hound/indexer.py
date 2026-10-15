"""Indexador incremental de la carpeta compartida.

- Solo lee: nunca escribe, mueve ni borra nada en la carpeta compartida.
- Escaneo: detecta ficheros nuevos, modificados (tamaño/fecha), borrados y cambios de permisos.
- Proceso: extrae texto en procesos aparte (con tiempo máximo por fichero), trocea,
  calcula embeddings y guarda todo en el índice.
- Vigilancia: reacciona a los cambios del disco (ReadDirectoryChangesW vía watchdog) y
  hace un reescaneo completo periódico por si se pierde algún evento.
"""

from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import hashlib
import logging
import os
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, field, replace

import numpy as np

from . import db
from .chunking import Chunk, chunk_blocks, has_words
from .config import Config
from .costs import CostModel
from .embed import Embedder
from .extract import ExtractOptions, Extracted, capabilities, extract, extractor_tag, kind_of
from .paths import Excluder, norm_rel, to_abs
from .security import SecurityProvider
from .status import set_state

log = logging.getLogger("hound.indexer")

EMBED_BATCH_CHUNKS = 64
PDF_FIRST_PAGES = 30   # primera pasada de un PDF largo: ya se puede buscar
PDF_STEP_PAGES = 50    # pasadas siguientes, cuando no queda nada pendiente
VISUAL_LOCATION = "imagen (lo que se ve)"


@dataclass
class ScanStats:
    seen: int = 0
    new: int = 0
    changed: int = 0
    deleted: int = 0
    perms_changed: int = 0
    moved: int = 0
    errors: list[str] = field(default_factory=list)


def lower_priority() -> None:
    """Prioridad baja de CPU y E/S para no molestar durante la jornada."""
    try:
        import psutil

        p = psutil.Process()
        if sys.platform == "win32":
            p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
            p.ionice(psutil.IOPRIO_VERYLOW)
        else:
            p.nice(10)
    except Exception as exc:  # noqa: BLE001
        log.warning("No se pudo bajar la prioridad: %s", exc)


def _extract_worker(path: str, opts: ExtractOptions) -> tuple[Extracted | None, str | None]:
    try:
        return extract(path, opts), None
    except Exception as exc:  # noqa: BLE001 — cualquier fallo se registra y se sigue
        msg = str(exc) or exc.__class__.__name__
        return None, msg[:500]


def name_chunk(rel: str) -> Chunk:
    folder, _, name = rel.rpartition("/")
    stem = os.path.splitext(name)[0]
    readable = stem.replace("_", " ").replace("-", " ").replace(".", " ")
    text = readable + (f"\nCarpeta: {folder.replace('/', ' / ')}" if folder else "")
    return Chunk(text, "nombre del fichero")


def parse_schedule(cfg: Config, now: dt.datetime | None = None) -> bool:
    """True si ahora es horario laboral."""
    s = cfg.index.schedule
    now = now or dt.datetime.now()
    days = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
    try:
        d0, d1 = (days[x.strip().lower()[:3]] for x in s.busy_days.split("-"))
        h0, h1 = (dt.time.fromisoformat(x.strip()) for x in s.busy_hours.split("-"))
    except (ValueError, KeyError):
        return False

    def in_days(wd: int) -> bool:
        return d0 <= wd <= d1 if d0 <= d1 else (wd >= d0 or wd <= d1)

    t = now.time()
    if h0 <= h1:
        return in_days(now.weekday()) and h0 <= t < h1
    # Franja que cruza la medianoche (p. ej. 22:00-06:00): de madrugada cuenta el día anterior.
    return (t >= h0 and in_days(now.weekday())) or (t < h1 and in_days((now.weekday() - 1) % 7))


class LoadMeter:
    """CPU que usan los demás programas del servidor (sin contar al propio Hound), en %."""

    def __init__(self):
        import psutil

        self.psutil = psutil
        self.me = psutil.Process()
        self.ncpu = psutil.cpu_count() or 1
        psutil.cpu_percent(None)
        self._own: dict[int, object] = {}

    def others_percent(self) -> float:
        total = self.psutil.cpu_percent(None)
        own = 0.0
        try:
            procs = [self.me, *self.me.children(recursive=True)]
        except self.psutil.Error:
            procs = [self.me]
        alive = {}
        for p in procs:
            proc = self._own.get(p.pid, p)
            try:
                own += proc.cpu_percent(None)
                alive[p.pid] = proc
            except self.psutil.Error:
                pass
        self._own = alive
        return max(0.0, total - own / self.ncpu)


class Indexer:
    def __init__(self, cfg: Config, embedder: Embedder, security: SecurityProvider,
                 conn: sqlite3.Connection | None = None):
        if not cfg.share.path or not os.path.isdir(cfg.share.path):
            raise RuntimeError(f"La carpeta compartida no existe: {cfg.share.path!r} (share.path)")
        self.cfg = cfg
        self.root = cfg.share.path
        self.embedder = embedder
        self.security = security
        self.conn = conn or db.init_db(cfg.db_path)
        self.excluder = Excluder(cfg)
        self.extensions = set(cfg.index.extensions)
        self.vision = getattr(embedder, "vision", None) is not None
        self.opts = ExtractOptions(ocr=cfg.ocr, cad=cfg.cad, visual=self.vision, image_tokens=cfg.model.image_tokens)
        self.caps = capabilities(self.opts)
        self._sd_ids: dict[str, int] = {r["hash"]: r["id"] for r in self.conn.execute(
            "SELECT id, hash FROM security_descriptors")}
        self._share_sd: bytes | None = None
        self._pool: cf.ProcessPoolExecutor | None = None
        self._changed: set[tuple[str, bool]] = set()
        self._moves: list[tuple[str, str]] = []
        self._first_event = self._last_event = 0.0
        self._changed_lock = threading.Lock()
        self.costs = CostModel.load(self.conn)
        model = db.get_meta(self.conn, "model")
        if model is None:
            db.set_meta(self.conn, "model", embedder.name)
            self.conn.commit()
        elif model != embedder.name:
            # Modelo nuevo: los vectores se recalculan en segundo plano a partir del texto ya extraído
            # (sin repetir extracción ni OCR). Mientras, la búsqueda ignora los vectores antiguos.
            gen = int(db.get_meta(self.conn, "model_gen", "0")) + 1
            db.set_meta(self.conn, "model_gen", str(gen))
            db.set_meta(self.conn, "model", embedder.name)
            self.conn.commit()
            log.warning("Modelo cambiado (%s -> %s): se recalculan los vectores sin reextraer", model, embedder.name)
        self.gen = int(db.get_meta(self.conn, "model_gen", "0"))
        db.set_meta(self.conn, "vision", "1" if self.vision else "0")
        self.conn.commit()

    # --- permisos ---------------------------------------------------------------

    def _sd_id(self, abs_path: str) -> int | None:
        if not self.security.enforced:
            return None
        sd = self.security.read_sd(abs_path)
        if sd is None:
            return None
        h = hashlib.sha256(sd).hexdigest()
        sid = self._sd_ids.get(h)
        if sid is None:
            cur = self.conn.execute("INSERT INTO security_descriptors(hash, sd) VALUES (?, ?)", (h, sd))
            sid = self._sd_ids[h] = cur.lastrowid
        return sid

    # --- escaneo ----------------------------------------------------------------

    def _known(self, subtree: str, recursive: bool) -> dict[str, sqlite3.Row]:
        q = "SELECT id, path, size, mtime, sd_id, status, extractor, kind, inode, dev FROM files"
        if subtree:
            rows = self.conn.execute(q + " WHERE path >= ? AND path < ?", (subtree + "/", subtree + "0"))
        else:
            rows = self.conn.execute(q)
        out = {}
        for r in rows:
            rest = r["path"][len(subtree) + 1:] if subtree else r["path"]
            if recursive or "/" not in rest:
                out[r["path"].lower()] = r
        return out

    def scan(self, subtree: str = "", recursive: bool = True) -> ScanStats:
        """Compara el disco con el índice en `subtree` y marca lo que hay que (re)procesar."""
        subtree = norm_rel(subtree)
        stats = ScanStats()
        if subtree and self.excluder.excluded(subtree):
            known = self._known(subtree, recursive)
            for row in known.values():
                self._delete_file(row["id"])
                stats.deleted += 1
            self.conn.commit()
            return stats
        full = not subtree and recursive
        if full:
            set_state(self.conn, "scanning", seen=0)
            self.conn.commit()
        known = self._known(subtree, recursive)
        new_files: list[tuple[str, str, str, os.stat_result]] = []
        failed_dirs: list[str] = []
        start = to_abs(subtree, self.root)
        if not os.path.isdir(start):
            if not subtree:
                raise RuntimeError(f"La carpeta compartida no está accesible: {self.root}")
            failed_dirs = []  # la carpeta ya no existe: se borra lo que hubiera dentro
        else:
            stack = [(start, subtree)]
            pending_commit = 0
            while stack:
                d_abs, d_rel = stack.pop()
                try:
                    entries = list(os.scandir(d_abs))
                except OSError as exc:
                    failed_dirs.append(d_rel)
                    stats.errors.append(f"{d_rel or '/'}: {exc}")
                    log.warning("No se puede leer la carpeta %s: %s", d_rel or "/", exc)
                    continue
                for e in entries:
                    rel = f"{d_rel}/{e.name}" if d_rel else e.name
                    if self.excluder.name_excluded(e.name) or self.excluder.excluded(rel):
                        continue
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if recursive:
                                stack.append((e.path, rel))
                            continue
                        if not e.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    ext = os.path.splitext(e.name)[1].lower()
                    if ext not in self.extensions:
                        continue
                    stats.seen += 1
                    try:
                        row = known.pop(rel.lower(), None)
                        if row is None:  # nuevo… o movido: se decide al final, con lo que ya no está
                            new_files.append((e.path, rel, ext, os.stat(e.path)))
                        else:
                            self._scan_file(e.path, rel, ext, row, stats)
                    except Exception as exc:  # noqa: BLE001
                        failed_dirs.append(rel)
                        stats.errors.append(f"{rel}: {exc}")
                        log.warning("Error al examinar %s: %s", rel, exc)
                    pending_commit += 1
                    if pending_commit >= 1000:
                        if full:
                            set_state(self.conn, "scanning", seen=stats.seen)
                        self.conn.commit()
                        pending_commit = 0
        # Lo que queda en `known` ya no está en el disco (salvo en carpetas que no se pudieron leer).
        gone = {key: row for key, row in known.items()
                if not ("" in failed_dirs or
                        any(key == f.lower() or key.startswith(f.lower() + "/") for f in failed_dirs if f))}
        # Un fichero nuevo con el mismo inodo, tamaño y fecha que uno desaparecido es el mismo, movido o
        # renombrado: se cambia la ruta y se conserva lo indexado (sin repetir extracción ni OCR).
        by_inode = {(r["dev"], r["inode"], r["size"]): (k, r) for k, r in gone.items() if r["inode"]}
        for abs_path, rel, ext, st in new_files:
            hit = by_inode.get((st.st_dev, st.st_ino, st.st_size)) if st.st_ino else None
            if hit and abs(hit[1]["mtime"] - st.st_mtime) < 1e-3:
                key, row = hit
                del gone[key], by_inode[(row["dev"], row["inode"], row["size"])]
                self._move_file(row, rel, abs_path)
                stats.moved += 1
                continue
            try:
                self._scan_file(abs_path, rel, ext, None, stats, st)
            except Exception as exc:  # noqa: BLE001
                stats.errors.append(f"{rel}: {exc}")
                log.warning("Error al examinar %s: %s", rel, exc)
        for row in gone.values():
            self._delete_file(row["id"])
            stats.deleted += 1
        if not subtree and recursive:
            db.set_meta(self.conn, "last_full_scan", str(time.time()))
        db.prune_tombstones(self.conn)
        self.conn.commit()
        if stats.new or stats.changed or stats.deleted or stats.perms_changed or stats.moved:
            log.info("Escaneo %s: %d ficheros, %d nuevos, %d modificados, %d movidos, %d borrados, "
                     "%d con permisos cambiados", subtree or "/", stats.seen, stats.new, stats.changed,
                     stats.moved, stats.deleted, stats.perms_changed)
        return stats

    def _scan_file(self, abs_path: str, rel: str, ext: str, row: sqlite3.Row | None, stats: ScanStats,
                   st: os.stat_result | None = None) -> None:
        st = st or os.stat(abs_path)
        sd_id = self._sd_id(abs_path)
        kind = kind_of(ext)
        if row is None:
            self.conn.execute(
                "INSERT INTO files(path, name, ext, kind, size, mtime, sd_id, status, seq, inode, dev) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                (rel, rel.rpartition("/")[2], ext, kind, st.st_size, st.st_mtime, sd_id, db.next_seq(self.conn),
                 st.st_ino or None, st.st_dev))
            stats.new += 1
            return
        if row["inode"] != (st.st_ino or None):  # índices anteriores a esta columna, o fichero sustituido
            self.conn.execute("UPDATE files SET inode = ?, dev = ? WHERE id = ?",
                              (st.st_ino or None, st.st_dev, row["id"]))
        changed = row["size"] != st.st_size or abs(row["mtime"] - st.st_mtime) > 1e-3 or \
            (row["status"] != "pending" and row["extractor"] != extractor_tag(kind, self.caps))
        if changed:
            self.conn.execute("UPDATE files SET path = ?, name = ?, size = ?, mtime = ?, sd_id = ?, "
                              "status = 'pending', seq = ? WHERE id = ?",
                              (rel, rel.rpartition("/")[2], st.st_size, st.st_mtime, sd_id,
                               db.next_seq(self.conn), row["id"]))
            stats.changed += 1
        elif row["sd_id"] != sd_id or row["path"] != rel:
            self.conn.execute("UPDATE files SET sd_id = ?, path = ?, name = ?, seq = ? WHERE id = ?",
                              (sd_id, rel, rel.rpartition("/")[2], db.next_seq(self.conn), row["id"]))
            stats.perms_changed += 1

    def _move_file(self, row: sqlite3.Row, rel: str, abs_path: str) -> None:
        """Cambia la ruta de un fichero ya indexado y rehace su entrada de palabras clave (el nombre
        forma parte de ella). Los vectores se conservan."""
        name_text = rel.replace("/", " ")
        segs = self.conn.execute("SELECT id, text FROM segments WHERE file_id = ?", (row["id"],)).fetchall()
        self.conn.executemany("DELETE FROM segments_fts WHERE rowid = ?", [(r["id"],) for r in segs])
        self.conn.executemany("INSERT INTO segments_fts(rowid, text, name) VALUES (?, ?, ?)",
                              [(r["id"], r["text"], name_text) for r in segs])
        self.conn.execute("UPDATE files SET path = ?, name = ?, sd_id = ?, seq = ? WHERE id = ?",
                          (rel, rel.rpartition("/")[2], self._sd_id(abs_path), db.next_seq(self.conn), row["id"]))
        log.info("Movido: %s -> %s", row["path"], rel)

    def _delete_file(self, file_id: int) -> None:
        ids = [r[0] for r in self.conn.execute("SELECT id FROM segments WHERE file_id = ?", (file_id,))]
        if ids:
            self.conn.executemany("DELETE FROM segments_fts WHERE rowid = ?", [(i,) for i in ids])
            self.conn.execute("INSERT INTO tombstones(file_id, upto_seg, ts) VALUES (?, ?, ?)",
                              (file_id, max(ids), time.time()))
        self.conn.execute("DELETE FROM segments WHERE file_id = ?", (file_id,))
        self.conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
        if not ids:
            self.conn.execute("INSERT INTO tombstones(file_id, upto_seg, ts) VALUES (?, 0, ?)", (file_id, time.time()))
        db.next_seq(self.conn)

    # --- proceso ----------------------------------------------------------------

    def _new_pool(self) -> cf.ProcessPoolExecutor:
        return cf.ProcessPoolExecutor(max_workers=max(1, self.cfg.index.extract_workers), initializer=lower_priority)

    def _kill_pool(self) -> None:
        if self._pool is None:
            return
        procs = list(getattr(self._pool, "_processes", {}).values())
        self._pool.shutdown(wait=False, cancel_futures=True)
        for p in procs:
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass
        self._pool = None

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)
            self._pool = None

    def _extract_many(self, rows: list[sqlite3.Row], stop: threading.Event | None):
        """Extrae en paralelo con un tiempo máximo por fichero. Devuelve (row, Extracted|None, error)."""
        if self._pool is None:
            self._pool = self._new_pool()
        workers = max(1, self.cfg.index.extract_workers)
        queue = list(rows)
        running: dict[cf.Future, tuple[sqlite3.Row, float]] = {}
        timeout = self.cfg.index.file_timeout_s
        while queue or running:
            while queue and len(running) < workers:
                row = queue.pop(0)
                fut = self._pool.submit(_extract_worker, to_abs(row["path"], self.root), self._opts_for(row))
                running[fut] = (row, time.monotonic())
            done, _ = cf.wait(running, timeout=1, return_when=cf.FIRST_COMPLETED)
            for fut in done:
                row, t0 = running.pop(fut)
                try:
                    ex, err = fut.result()
                except Exception as exc:  # noqa: BLE001 — p. ej. el proceso murió
                    ex, err = None, f"Fallo del extractor: {exc.__class__.__name__}"
                    self._kill_pool()
                    self._pool = self._new_pool()
                    queue = [r for r, _ in running.values()] + queue
                    running.clear()
                yield row, ex, err, time.monotonic() - t0
            now = time.monotonic()
            late = [f for f, (_r, t) in running.items() if now - t > timeout]
            if late:
                # Un fichero se ha colgado: se reinicia el pool y se reintentan los demás.
                for f in late:
                    row, t0 = running.pop(f)
                    yield row, None, f"Tiempo de extracción agotado ({timeout} s)", now - t0
                queue = [r for r, _ in running.values()] + queue
                running.clear()
                self._kill_pool()
                self._pool = self._new_pool()
            if stop is not None and stop.is_set():
                break

    def process_pending(self, max_files: int = 200, stop: threading.Event | None = None,
                        throttle: float = 0.0) -> int:
        cols = "SELECT id, path, ext, kind, size, mtime, status, pages_done, title, '' AS job FROM files"
        # Primero lo nuevo y lo barato (las imágenes, con OCR, al final); después, tras un cambio de
        # modelo, recalcular vectores; luego el vector visual de las imágenes ya indexadas; y los PDF
        # largos se terminan cuando ya no queda nada más.
        rows = self.conn.execute(
            cols + " WHERE status = 'pending' ORDER BY kind IN ('image', 'cad'), mtime DESC LIMIT ?",
            (max_files,)).fetchall()
        if not rows:
            rows = self.conn.execute(cols + " WHERE emb < ? AND status != 'pending' ORDER BY mtime DESC LIMIT ?",
                                     (self.gen, max_files)).fetchall()
            if rows:
                return self._reembed(rows)
        if not rows and self.vision:
            rows = self.conn.execute(
                cols.replace("'' AS job", "'visual' AS job") + " WHERE kind = 'image' AND visual = 0 AND "
                "status = 'indexed' ORDER BY mtime DESC LIMIT ?", (max(1, max_files // 4),)).fetchall()
        if not rows:
            rows = self.conn.execute(cols + " WHERE status = 'partial' ORDER BY mtime DESC LIMIT ?",
                                     (max(1, max_files // 10),)).fetchall()
        if not rows:
            return 0
        batch: list[tuple[sqlite3.Row, list[Chunk], str, str | None, bool]] = []
        n_chunks = 0
        done = 0
        max_bytes = self.cfg.index.max_file_mb * 1024 * 1024
        big = [r for r in rows if r["size"] > max_bytes]
        small = [r for r in rows if r["size"] <= max_bytes]
        results = [(r, Extracted(notes=[f"Fichero mayor de {self.cfg.index.max_file_mb} MB: indexado solo por nombre"]),
                    None, 0.0) for r in big]
        workers = max(1, self.cfg.index.extract_workers)
        for item in [*results, *self._extract_many(small, stop)]:
            row, ex, err, elapsed = item
            failed = ex is None and err is not None
            continuing = row["status"] == "partial" or row["job"] == "visual"
            if row["job"] == "visual" and (failed or ex is None or ex.pixels is None):
                self.conn.execute("UPDATE files SET visual = -1 WHERE id = ?", (row["id"],))  # no reintentar
                self.conn.commit()
                continue
            if continuing and failed:
                if not os.path.isfile(to_abs(row["path"], self.root)):
                    continue  # movido o borrado mientras se leía: el escaneo lo resuelve y luego se sigue
                # El tramo falla de verdad (p. ej. tiempo agotado): se da por terminado con lo ya leído.
                done_pages = row["pages_done"] or 0
                ex = Extracted(pages_done=done_pages, pages_total=done_pages)
                err, failed = f"PDF indexado solo hasta la página {done_pages}: {err}", False
            chunks = [] if continuing else [name_chunk(row["path"])]
            title = ""
            if ex is not None:
                title = ex.title.strip()
                if title.lower() in ("untitled", "sin título", "documento", "document") or \
                        title.lower() == os.path.splitext(row["path"].rpartition("/")[2])[0].lower():
                    title = ""
                chunks += chunk_blocks(ex.blocks, self.cfg.search.chunk_chars, self.cfg.search.chunk_overlap)
                if ex.notes and not err:
                    err = "; ".join(ex.notes)[:500]
            batch.append((row, chunks, title, err, failed, ex, elapsed / workers))
            n_chunks += len(chunks)
            if n_chunks >= EMBED_BATCH_CHUNKS:
                done += self._write(batch)
                batch, n_chunks = [], 0
                if throttle:
                    time.sleep(throttle)
            if stop is not None and stop.is_set():
                break
        if batch:
            done += self._write(batch)
        return done

    def _write(self, batch) -> int:
        # Fragmentos casi sin letras (tablas de cifras) solo van al índice de palabras: un vector de
        # números no aporta significado, ocupa memoria y mete ruido en las búsquedas por sentido.
        items, want = [], []
        for row, chunks, title, _err, _failed, _ex, _cost in batch:
            name = row["path"].rpartition("/")[2]
            keep = [k == 0 and row["status"] != "partial" or has_words(c.text) for k, c in enumerate(chunks)]
            want.append(keep)
            items.extend((title or name, c.text) for c, k in zip(chunks, keep) if k)
        t0 = time.monotonic()
        vecs = self.embedder.embed_documents(items).astype(np.float16) if items else np.zeros((0, 1), np.float16)
        embed_s = time.monotonic() - t0
        # Vector de "lo que se ve" de cada imagen (EmbeddingGemma 2), con su coste por fichero.
        visual: dict[int, tuple[bytes, float]] = {}
        for b, (_row, _chunks, _title, _err, _failed, ex, _cost) in enumerate(batch):
            if ex is not None and ex.pixels is not None and self.vision:
                t1 = time.monotonic()
                try:
                    v = self.embedder.embed_images([ex.pixels])[0].astype(np.float16).tobytes()
                except Exception as exc:  # noqa: BLE001 — sin vector visual, el resto se indexa igual
                    log.warning("%s: vector visual fallido: %s", _row["path"], exc)
                    continue
                visual[b] = (v, time.monotonic() - t1)
        i = 0
        conn = self.conn
        with conn:
            for b, ((row, chunks, title, err, failed, ex, cost), keep) in enumerate(zip(batch, want)):
                n_vec = sum(keep)
                cur = conn.execute("SELECT size, mtime, status FROM files WHERE id = ?", (row["id"],)).fetchone()
                if cur is None:  # se borró mientras se procesaba
                    i += n_vec
                    continue
                continuing = row["status"] == "partial" or row["job"] == "visual"
                ord0 = 0
                if continuing:
                    ord0 = (conn.execute("SELECT max(ord) FROM segments WHERE file_id = ?",
                                         (row["id"],)).fetchone()[0] or 0) + 1
                else:
                    old = conn.execute("SELECT max(id) FROM segments WHERE file_id = ?", (row["id"],)).fetchone()[0]
                    if old is not None:
                        conn.execute("DELETE FROM segments_fts WHERE rowid IN "
                                     "(SELECT id FROM segments WHERE file_id = ?)", (row["id"],))
                        conn.execute("DELETE FROM segments WHERE file_id = ?", (row["id"],))
                        conn.execute("INSERT INTO tombstones(file_id, upto_seg, ts) VALUES (?, ?, ?)",
                                     (row["id"], old, time.time()))
                name_text = row["path"].replace("/", " ")
                for k, (c, vec_ok) in enumerate(zip(chunks, keep)):
                    vec = vecs[i].tobytes() if vec_ok else b""
                    i += vec_ok
                    seg = conn.execute(
                        "INSERT INTO segments(file_id, ord, location, text, vec) VALUES (?, ?, ?, ?, ?)",
                        (row["id"], ord0 + k, c.location, c.text, vec))
                    conn.execute("INSERT INTO segments_fts(rowid, text, name) VALUES (?, ?, ?)",
                                 (seg.lastrowid, c.text, name_text))
                vis = visual.get(b)
                if vis is not None:
                    seg = conn.execute(
                        "INSERT INTO segments(file_id, ord, location, text, vec) VALUES (?, ?, ?, '', ?)",
                        (row["id"], ord0 + len(chunks), VISUAL_LOCATION, vis[0]))
                    conn.execute("INSERT INTO segments_fts(rowid, text, name) VALUES (?, '', ?)",
                                 (seg.lastrowid, name_text))
                    cost += vis[1]
                if row["job"] == "visual":
                    conn.execute("UPDATE files SET visual = ?, seq = ? WHERE id = ?",
                                 (1 if vis else -1, db.next_seq(conn), row["id"]))
                    if vis:
                        self.costs.add("_visual", 0.0, cost)
                    continue
                pages_done = ex.pages_done if ex is not None else None
                pages_total = ex.pages_total if ex is not None else None
                # Si cambió mientras se extraía, se deja pendiente para la próxima vuelta.
                still_same = cur["size"] == row["size"] and abs(cur["mtime"] - row["mtime"]) < 1e-3
                if not still_same:
                    status = "pending"
                elif failed:
                    status = "error"
                elif pages_done is not None and pages_total and pages_done < pages_total:
                    status = "partial"
                else:
                    status = "indexed"
                conn.execute("UPDATE files SET status = ?, error = ?, extractor = ?, indexed_at = ?, seq = ?, "
                             "pages_done = ?, pages_total = ?, emb = ?, title = coalesce(nullif(?, ''), title), "
                             "visual = CASE WHEN ? THEN 1 WHEN ? THEN 0 ELSE visual END WHERE id = ?",
                             (status, err, extractor_tag(row["kind"], self.caps), time.time(),
                              db.next_seq(conn), pages_done or 0, pages_total, self.gen, title,
                              vis is not None, not continuing, row["id"]))
                if not failed and still_same:
                    mb = row["size"] / 1e6
                    if pages_total and pages_done is not None:
                        before = row["pages_done"] if continuing else 0
                        mb *= max(0, pages_done - before) / pages_total
                    self.costs.add(row["kind"], mb, cost + embed_s * n_vec / max(1, len(items)))
                if err:
                    log.info("%s: %s", row["path"], err)
            self.costs.save(conn)
            set_state(conn, "indexing")  # latido: un lote con OCR puede tardar mucho
        return len(batch)

    def _reembed(self, rows: list[sqlite3.Row]) -> int:
        """Tras cambiar de modelo: vectores nuevos a partir del texto guardado, sin reextraer.

        Se escriben como fragmentos nuevos y se retiran los antiguos, igual que al reindexar un
        fichero, para que los lectores con los vectores en memoria se actualicen solos. Los vectores
        visuales no salen del texto: esas imágenes vuelven a la cola del vector visual."""
        conn = self.conn
        work = []
        for row in rows:
            segs = conn.execute("SELECT id, ord, location, text, length(vec) > 0 AS has_vec FROM segments "
                                "WHERE file_id = ? ORDER BY ord", (row["id"],)).fetchall()
            work.append((row, [s for s in segs if s["text"]]))
        items, owners = [], []
        for k, (row, segs) in enumerate(work):
            name = row["path"].rpartition("/")[2]
            for s in segs:
                if s["has_vec"]:
                    items.append((row["title"] or name, s["text"]))
                    owners.append((k, s["id"]))
        t0 = time.monotonic()
        vecs = self.embedder.embed_documents(items).astype(np.float16) if items else None
        per_vec = (time.monotonic() - t0) / max(1, len(items))
        new_vec = {sid: vecs[i].tobytes() for i, (_k, sid) in enumerate(owners)}
        with conn:
            for row, segs in work:
                cur = conn.execute("SELECT max(id) FROM segments WHERE file_id = ?", (row["id"],)).fetchone()[0]
                if cur is not None:
                    conn.execute("DELETE FROM segments_fts WHERE rowid IN (SELECT id FROM segments WHERE file_id = ?)",
                                 (row["id"],))
                    conn.execute("DELETE FROM segments WHERE file_id = ?", (row["id"],))
                    conn.execute("INSERT INTO tombstones(file_id, upto_seg, ts) VALUES (?, ?, ?)",
                                 (row["id"], cur, time.time()))
                name_text = row["path"].replace("/", " ")
                for s in segs:
                    seg = conn.execute("INSERT INTO segments(file_id, ord, location, text, vec) VALUES (?, ?, ?, ?, ?)",
                                       (row["id"], s["ord"], s["location"], s["text"], new_vec.get(s["id"], b"")))
                    conn.execute("INSERT INTO segments_fts(rowid, text, name) VALUES (?, ?, ?)",
                                 (seg.lastrowid, s["text"], name_text))
                conn.execute("UPDATE files SET emb = ?, visual = 0, seq = ? WHERE id = ?",
                             (self.gen, db.next_seq(conn), row["id"]))
                n = sum(1 for s in segs if s["has_vec"])
                if n:
                    self.costs.add("_reembed", n / 100, n * per_vec)
            self.costs.save(conn)
            set_state(conn, "indexing")
        return len(rows)

    def _opts_for(self, row: sqlite3.Row) -> ExtractOptions:
        if row["job"] == "visual":
            return replace(self.opts, visual_only=True)
        if row["kind"] != "pdf":
            return self.opts
        if row["status"] == "partial":
            return replace(self.opts, page_start=row["pages_done"] or 0, page_limit=PDF_STEP_PAGES)
        return replace(self.opts, page_start=0, page_limit=PDF_FIRST_PAGES)

    # --- vigilancia ---------------------------------------------------------------

    def _start_watcher(self):
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            return None
        indexer = self

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                if event.event_type in ("opened", "closed", "closed_no_write"):
                    return
                if event.event_type == "moved" and event.dest_path:
                    try:
                        move = (norm_rel(os.path.relpath(event.src_path, indexer.root)),
                                norm_rel(os.path.relpath(event.dest_path, indexer.root)))
                    except ValueError:
                        move = None
                    if move:
                        with indexer._changed_lock:
                            indexer._moves.append(move)
                now = time.monotonic()
                with indexer._changed_lock:
                    indexer._first_event = indexer._first_event or now
                    indexer._last_event = now
                for p in (event.src_path, getattr(event, "dest_path", "")):
                    if not p:
                        continue
                    try:
                        rel = norm_rel(os.path.relpath(p, indexer.root))
                    except ValueError:
                        continue
                    with indexer._changed_lock:
                        if event.is_directory:
                            indexer._changed.add((rel, True))
                        else:
                            indexer._changed.add((rel.rpartition("/")[0], False))

        obs = Observer()
        obs.schedule(Handler(), self.root, recursive=True)
        obs.daemon = True
        obs.start()
        log.info("Vigilando cambios en %s", self.root)
        return obs

    QUIET_S = 2.0       # se espera a que los cambios paren un momento…
    MAX_WAIT_S = 30.0   # …pero no más de esto
    MAX_CHANGES = 2000  # con más carpetas cambiadas (o avisos perdidos) sale a cuenta un escaneo completo

    def changes_ready(self, now: float | None = None) -> bool:
        now = now or time.monotonic()
        with self._changed_lock:
            if not self._changed and not self._moves:
                return False
            return now - self._last_event >= self.QUIET_S or now - self._first_event >= self.MAX_WAIT_S

    def _apply_moves(self, moves: list[tuple[str, str]]) -> None:
        """Renombrados y movimientos que ha visto el vigilante: se cambia la ruta sin reindexar."""
        for src, dest in moves:
            if not src or not dest or self.excluder.excluded(dest):
                continue  # a una carpeta excluida: lo borrará el escaneo
            rows = self.conn.execute("SELECT id, path FROM files WHERE path = ? OR (path >= ? AND path < ?)",
                                     (src, src + "/", src + "0")).fetchall()
            for row in rows:
                new = dest + row["path"][len(src):]
                abs_new = to_abs(new, self.root)
                if not os.path.isfile(abs_new) or os.path.splitext(new)[1].lower() not in self.extensions:
                    continue
                if self.conn.execute("SELECT 1 FROM files WHERE path = ?", (new,)).fetchone():
                    continue
                self._move_file(row, new, abs_new)
        self.conn.commit()

    def _drain_changes(self) -> bool:
        """Procesa los cambios vistos. Devuelve False si hacen falta un escaneo completo."""
        with self._changed_lock:
            changed, self._changed = self._changed, set()
            moves, self._moves = self._moves, []
            self._first_event = self._last_event = 0.0
        if moves:
            self._apply_moves(moves)
        if len(changed) > self.MAX_CHANGES or ("", True) in changed:
            return False
        recursive_dirs = {d for d, r in changed if r}
        for d, rec in sorted(changed):
            if not rec and any(d == r or d.startswith(r + "/") for r in recursive_dirs if r):
                continue
            try:
                self.scan(d, recursive=rec)
            except Exception as exc:  # noqa: BLE001
                log.warning("Error al reescanear %s: %s", d or "/", exc)
        return True

    def run_forever(self, stop: threading.Event) -> None:
        lower_priority()
        log.info("Indexador iniciado. Carpeta: %s. Capacidades: %s", self.root, ", ".join(sorted(self.caps)) or "-")
        observer = self._start_watcher() if self.cfg.index.watch else None
        meter = None
        if self.cfg.index.schedule.max_cpu_percent:
            try:
                meter = LoadMeter()
            except Exception as exc:  # noqa: BLE001
                log.warning("No se puede medir la carga del servidor: %s", exc)
        last_full = 0.0
        epoch = db.get_meta(self.conn, "epoch", "")
        try:
            while not stop.is_set():
                if db.get_meta(self.conn, "epoch", "") != epoch:  # 'hound reindex --all'
                    epoch = db.get_meta(self.conn, "epoch", "")
                    self._sd_ids.clear()
                    db.set_meta(self.conn, "model", self.embedder.name)
                    self.conn.commit()
                    last_full = 0.0
                    log.info("Índice vaciado: se reconstruye desde cero")
                if time.time() - last_full > self.cfg.index.rescan_minutes * 60:
                    try:
                        self.scan()
                    except Exception as exc:  # noqa: BLE001
                        log.error("Escaneo completo fallido: %s", exc)
                    last_full = time.time()
                if self.changes_ready() and not self._drain_changes():
                    log.info("Demasiados cambios a la vez (o avisos perdidos): escaneo completo")
                    last_full = 0.0
                    continue
                sched = self.cfg.index.schedule
                busy_hours = parse_schedule(self.cfg)
                loaded = False
                if sched.max_cpu_percent and meter is not None:
                    loaded = meter.others_percent() > sched.max_cpu_percent
                busy = busy_hours or loaded
                mode = sched.busy_mode if busy_hours else ("slow" if loaded else "normal")
                backlog = self.conn.execute("SELECT count(*) FROM files WHERE status = 'pending'").fetchone()[0]
                if mode == "pause" and backlog > 200:
                    set_state(self.conn, "paused")
                    self.conn.commit()
                    stop.wait(60)  # carga inicial: se espera a que acabe la jornada
                    continue
                throttle = self.cfg.index.schedule.busy_pause_ms / 1000 if mode in ("slow", "pause") else 0.0
                extra = backlog or self.conn.execute(
                    "SELECT 1 FROM files WHERE (emb < ? AND status != 'pending') OR (? AND kind = 'image' "
                    "AND visual = 0 AND status = 'indexed') OR status = 'partial' LIMIT 1",
                    (self.gen, self.vision)).fetchone()
                set_state(self.conn, "indexing" if extra else "idle")
                self.conn.commit()
                n = self.process_pending(max_files=50 if busy else 200, stop=stop, throttle=throttle)
                if n == 0:
                    set_state(self.conn, "idle")
                    self.conn.commit()
                    stop.wait(2)
        finally:
            if observer is not None:
                observer.stop()
            self.close()
            log.info("Indexador detenido")
