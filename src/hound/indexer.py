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
from dataclasses import dataclass, field

import numpy as np

from . import db
from .chunking import Chunk, chunk_blocks
from .config import Config
from .embed import Embedder
from .extract import ExtractOptions, Extracted, capabilities, extract, extractor_tag, kind_of
from .paths import Excluder, norm_rel, to_abs
from .security import SecurityProvider
from .status import set_state

log = logging.getLogger("hound.indexer")

EMBED_BATCH_CHUNKS = 64


@dataclass
class ScanStats:
    seen: int = 0
    new: int = 0
    changed: int = 0
    deleted: int = 0
    perms_changed: int = 0
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
    in_days = d0 <= now.weekday() <= d1 if d0 <= d1 else (now.weekday() >= d0 or now.weekday() <= d1)
    return in_days and h0 <= now.time() < h1


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
        self.opts = ExtractOptions(ocr=cfg.ocr, cad=cfg.cad)
        self.caps = capabilities(self.opts)
        self._sd_ids: dict[str, int] = {r["hash"]: r["id"] for r in self.conn.execute(
            "SELECT id, hash FROM security_descriptors")}
        self._share_sd: bytes | None = None
        self._pool: cf.ProcessPoolExecutor | None = None
        self._changed: set[tuple[str, bool]] = set()
        self._changed_lock = threading.Lock()
        model = db.get_meta(self.conn, "model")
        if model is None:
            db.set_meta(self.conn, "model", embedder.name)
            self.conn.commit()
        elif model != embedder.name:
            raise RuntimeError(f"El índice se creó con el modelo {model!r} y la configuración usa "
                               f"{embedder.name!r}. Ejecuta 'hound reindex --all' para rehacerlo.")

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
        q = "SELECT id, path, size, mtime, sd_id, status, extractor, kind FROM files"
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
                        self._scan_file(e.path, rel, ext, known.pop(rel.lower(), None), stats)
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
        for key, row in known.items():
            if any(key == f.lower() or key.startswith(f.lower() + "/") for f in failed_dirs if f) or \
                    ("" in failed_dirs):
                continue
            self._delete_file(row["id"])
            stats.deleted += 1
        if not subtree and recursive:
            db.set_meta(self.conn, "last_full_scan", str(time.time()))
        db.prune_tombstones(self.conn)
        self.conn.commit()
        if stats.new or stats.changed or stats.deleted or stats.perms_changed:
            log.info("Escaneo %s: %d ficheros, %d nuevos, %d modificados, %d borrados, %d con permisos cambiados",
                     subtree or "/", stats.seen, stats.new, stats.changed, stats.deleted, stats.perms_changed)
        return stats

    def _scan_file(self, abs_path: str, rel: str, ext: str, row: sqlite3.Row | None, stats: ScanStats) -> None:
        st = os.stat(abs_path)
        sd_id = self._sd_id(abs_path)
        kind = kind_of(ext)
        if row is None:
            self.conn.execute(
                "INSERT INTO files(path, name, ext, kind, size, mtime, sd_id, status, seq) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (rel, rel.rpartition("/")[2], ext, kind, st.st_size, st.st_mtime, sd_id, db.next_seq(self.conn)))
            stats.new += 1
            return
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
                fut = self._pool.submit(_extract_worker, to_abs(row["path"], self.root), self.opts)
                running[fut] = (row, time.monotonic())
            done, _ = cf.wait(running, timeout=1, return_when=cf.FIRST_COMPLETED)
            for fut in done:
                row, _t = running.pop(fut)
                try:
                    ex, err = fut.result()
                except Exception as exc:  # noqa: BLE001 — p. ej. el proceso murió
                    ex, err = None, f"Fallo del extractor: {exc.__class__.__name__}"
                    self._kill_pool()
                    self._pool = self._new_pool()
                    queue = [r for r, _ in running.values()] + queue
                    running.clear()
                yield row, ex, err
            now = time.monotonic()
            late = [f for f, (_r, t) in running.items() if now - t > timeout]
            if late:
                # Un fichero se ha colgado: se reinicia el pool y se reintentan los demás.
                for f in late:
                    row, _t = running.pop(f)
                    yield row, None, f"Tiempo de extracción agotado ({timeout} s)"
                queue = [r for r, _ in running.values()] + queue
                running.clear()
                self._kill_pool()
                self._pool = self._new_pool()
            if stop is not None and stop.is_set():
                break

    def process_pending(self, max_files: int = 200, stop: threading.Event | None = None,
                        throttle: float = 0.0) -> int:
        rows = self.conn.execute(
            "SELECT id, path, ext, kind, size, mtime FROM files WHERE status = 'pending' "
            "ORDER BY mtime DESC LIMIT ?", (max_files,)).fetchall()
        if not rows:
            return 0
        batch: list[tuple[sqlite3.Row, list[Chunk], str, str | None, bool]] = []
        n_chunks = 0
        done = 0
        max_bytes = self.cfg.index.max_file_mb * 1024 * 1024
        big = [r for r in rows if r["size"] > max_bytes]
        small = [r for r in rows if r["size"] <= max_bytes]
        results = [(r, Extracted(notes=[f"Fichero mayor de {self.cfg.index.max_file_mb} MB: indexado solo por nombre"]),
                    None) for r in big]
        for item in [*results, *self._extract_many(small, stop)]:
            row, ex, err = item
            failed = ex is None and err is not None
            chunks = [name_chunk(row["path"])]
            title = ""
            if ex is not None:
                title = ex.title.strip()
                if title.lower() in ("untitled", "sin título", "documento", "document") or \
                        title.lower() == os.path.splitext(row["path"].rpartition("/")[2])[0].lower():
                    title = ""
                chunks += chunk_blocks(ex.blocks, self.cfg.search.chunk_chars, self.cfg.search.chunk_overlap)
                if ex.notes and not err:
                    err = "; ".join(ex.notes)[:500]
            batch.append((row, chunks, title, err, failed))
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
        items = []
        for row, chunks, title, _err, _failed in batch:
            name = row["path"].rpartition("/")[2]
            items.extend((title or name, c.text) for c in chunks)
        vecs = self.embedder.embed_documents(items).astype(np.float16)
        i = 0
        conn = self.conn
        with conn:
            for row, chunks, _title, err, failed in batch:
                cur = conn.execute("SELECT size, mtime, status FROM files WHERE id = ?", (row["id"],)).fetchone()
                if cur is None:  # se borró mientras se procesaba
                    i += len(chunks)
                    continue
                old = conn.execute("SELECT max(id) FROM segments WHERE file_id = ?", (row["id"],)).fetchone()[0]
                if old is not None:
                    conn.execute("DELETE FROM segments_fts WHERE rowid IN (SELECT id FROM segments WHERE file_id = ?)",
                                 (row["id"],))
                    conn.execute("DELETE FROM segments WHERE file_id = ?", (row["id"],))
                    conn.execute("INSERT INTO tombstones(file_id, upto_seg, ts) VALUES (?, ?, ?)",
                                 (row["id"], old, time.time()))
                name_text = row["path"].replace("/", " ")
                for ord_, c in enumerate(chunks):
                    seg = conn.execute(
                        "INSERT INTO segments(file_id, ord, location, text, vec) VALUES (?, ?, ?, ?, ?)",
                        (row["id"], ord_, c.location, c.text, vecs[i].tobytes()))
                    conn.execute("INSERT INTO segments_fts(rowid, text, name) VALUES (?, ?, ?)",
                                 (seg.lastrowid, c.text, name_text))
                    i += 1
                # Si cambió mientras se extraía, se deja pendiente para la próxima vuelta.
                still_same = cur["size"] == row["size"] and abs(cur["mtime"] - row["mtime"]) < 1e-3
                status = ("error" if failed else "indexed") if still_same else "pending"
                conn.execute("UPDATE files SET status = ?, error = ?, extractor = ?, indexed_at = ?, seq = ? "
                             "WHERE id = ?",
                             (status, err, extractor_tag(row["kind"], self.caps), time.time(),
                              db.next_seq(conn), row["id"]))
                if err:
                    log.info("%s: %s", row["path"], err)
            set_state(conn, "indexing")  # latido: un lote con OCR puede tardar mucho
        return len(batch)

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

    def _drain_changes(self) -> None:
        with self._changed_lock:
            changed, self._changed = self._changed, set()
        recursive_dirs = {d for d, r in changed if r}
        for d, rec in sorted(changed):
            if not rec and any(d == r or d.startswith(r + "/") for r in recursive_dirs if r):
                continue
            try:
                self.scan(d, recursive=rec)
            except Exception as exc:  # noqa: BLE001
                log.warning("Error al reescanear %s: %s", d or "/", exc)

    def run_forever(self, stop: threading.Event) -> None:
        lower_priority()
        log.info("Indexador iniciado. Carpeta: %s. Capacidades: %s", self.root, ", ".join(sorted(self.caps)) or "-")
        observer = self._start_watcher() if self.cfg.index.watch else None
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
                self._drain_changes()
                busy = parse_schedule(self.cfg)
                mode = self.cfg.index.schedule.busy_mode if busy else "normal"
                backlog = self.conn.execute("SELECT count(*) FROM files WHERE status = 'pending'").fetchone()[0]
                if mode == "pause" and backlog > 200:
                    set_state(self.conn, "paused")
                    self.conn.commit()
                    stop.wait(60)  # carga inicial: se espera a que acabe la jornada
                    continue
                throttle = self.cfg.index.schedule.busy_pause_ms / 1000 if mode in ("slow", "pause") else 0.0
                set_state(self.conn, "indexing" if backlog else "idle")
                self.conn.commit()
                n = self.process_pending(max_files=50 if busy else 200, stop=stop, throttle=throttle)
                if n == 0:
                    set_state(self.conn, "idle")
                    self.conn.commit()
                    stop.wait(5)
        finally:
            if observer is not None:
                observer.stop()
            self.close()
            log.info("Indexador detenido")
