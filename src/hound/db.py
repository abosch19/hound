"""Esquema del índice SQLite (modo WAL: un escritor, el indexador; varios lectores)."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Descriptores de seguridad NTFS (binario autorrelativo), deduplicados:
-- casi todos los ficheros heredan los mismos permisos de su carpeta.
CREATE TABLE IF NOT EXISTS security_descriptors (
    id   INTEGER PRIMARY KEY,
    hash TEXT NOT NULL UNIQUE,
    sd   BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY,
    path        TEXT NOT NULL UNIQUE COLLATE NOCASE,  -- relativa a la carpeta compartida, con "/"
    name        TEXT NOT NULL,
    ext         TEXT NOT NULL,
    kind        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    mtime       REAL NOT NULL,          -- segundos epoch
    sd_id       INTEGER REFERENCES security_descriptors(id),
    status      TEXT NOT NULL,          -- pending | partial | indexed | error
    error       TEXT,
    extractor   TEXT,                   -- versión del extractor que lo indexó
    indexed_at  REAL,
    seq         INTEGER NOT NULL DEFAULT 0,  -- último cambio, para recargas incrementales
    inode       INTEGER,                -- identificador en el disco: detecta ficheros movidos
    dev         INTEGER,
    pages_done  INTEGER NOT NULL DEFAULT 0,  -- PDF largos: páginas ya indexadas (status = partial)
    pages_total INTEGER
);
CREATE INDEX IF NOT EXISTS files_status ON files(status);
CREATE INDEX IF NOT EXISTS files_mtime ON files(mtime);
CREATE INDEX IF NOT EXISTS files_seq ON files(seq);

CREATE TABLE IF NOT EXISTS segments (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,  -- monótono: los lectores cargan id > último
    file_id  INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    ord      INTEGER NOT NULL,       -- posición dentro del fichero
    location TEXT NOT NULL,          -- "página 3", "hoja Ventas, filas 1–40"…
    text     TEXT NOT NULL,
    vec      BLOB NOT NULL           -- float16 normalizado
);
CREATE INDEX IF NOT EXISTS segments_file ON segments(file_id, ord);

CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5(
    text, name,
    content='', contentless_delete=1,
    tokenize='unicode61 remove_diacritics 2'
);

-- Fragmentos retirados (fichero borrado o reindexado), para que los lectores con los
-- vectores en memoria descarten los segmentos de file_id con id <= upto_seg.
CREATE TABLE IF NOT EXISTS tombstones (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id  INTEGER NOT NULL,
    upto_seg INTEGER NOT NULL,
    ts       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,  -- DOMINIO\\usuario o EQUIPO\\usuario
    sid        TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tokens (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    hash       TEXT NOT NULL UNIQUE,
    label      TEXT,
    created_at REAL NOT NULL,
    last_used  REAL,
    revoked_at REAL
);

CREATE TABLE IF NOT EXISTS query_log (
    id        INTEGER PRIMARY KEY,
    ts        REAL NOT NULL,
    user_name TEXT NOT NULL,
    tool      TEXT NOT NULL,
    args      TEXT NOT NULL,
    results   INTEGER,
    ms        INTEGER
);
CREATE INDEX IF NOT EXISTS query_log_ts ON query_log(ts);
"""


def connect(path: Path | str, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=30,
                               check_same_thread=False)
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


# Columnas añadidas después de la primera versión: se crean en índices existentes.
_MIGRATIONS = {
    "files": [("inode", "INTEGER"), ("dev", "INTEGER"), ("pages_done", "INTEGER NOT NULL DEFAULT 0"),
              ("pages_total", "INTEGER")],
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, cols in _MIGRATIONS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.execute("CREATE INDEX IF NOT EXISTS files_inode ON files(inode)")


def init_db(path: Path | str) -> sqlite3.Connection:
    conn = connect(path)
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('seq', '0')")
    conn.commit()
    return conn


def reset_index(conn: sqlite3.Connection) -> None:
    """Vacía el contenido indexado (no los usuarios ni los tokens). Los lectores lo detectan por `epoch`."""
    conn.executescript("""
        DROP TABLE IF EXISTS segments_fts;
        DROP TABLE IF EXISTS segments;
        DROP TABLE IF EXISTS files;
        DROP TABLE IF EXISTS security_descriptors;
        DROP TABLE IF EXISTS tombstones;
        DELETE FROM meta WHERE key IN ('model', 'last_full_scan');
    """)
    conn.executescript(SCHEMA)
    _migrate(conn)
    set_meta(conn, "epoch", str(time.time()))
    conn.commit()
    conn.execute("VACUUM")


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, value))


def next_seq(conn: sqlite3.Connection) -> int:
    """Número de secuencia global: sube con cada cambio del índice."""
    row = conn.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'seq' RETURNING value").fetchone()
    return int(row[0])


def prune_tombstones(conn: sqlite3.Connection, max_age_s: float = 3 * 86400) -> None:
    conn.execute("DELETE FROM tombstones WHERE ts < ?", (time.time() - max_age_s,))
