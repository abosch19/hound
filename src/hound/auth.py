"""Usuarios y tokens personales del MCP.

Cada token pertenece a una cuenta de Windows (local o del dominio) y guarda su SID: las
búsquedas hechas con ese token se filtran con los permisos NTFS de esa cuenta.
En la base de datos solo se guarda el hash SHA-256 del token.
"""

from __future__ import annotations

import hashlib
import queue
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from . import db
from .search import User
from .security import SecurityProvider

TOKEN_PREFIX = "hound_"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_token(conn: sqlite3.Connection, security: SecurityProvider, account: str,
                 label: str = "") -> tuple[str, str]:
    """Crea (o reutiliza) el usuario y le emite un token nuevo. Devuelve (nombre, token)."""
    name, sid = security.lookup_account(account)
    row = conn.execute("SELECT id, sid FROM users WHERE name = ?", (name,)).fetchone()
    if row is None:
        user_id = conn.execute("INSERT INTO users(name, sid, created_at) VALUES (?, ?, ?)",
                               (name, sid, time.time())).lastrowid
    else:
        user_id = row["id"]
        if row["sid"] != sid:  # la cuenta se recreó con el mismo nombre
            conn.execute("UPDATE users SET sid = ? WHERE id = ?", (sid, user_id))
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    conn.execute("INSERT INTO tokens(user_id, hash, label, created_at) VALUES (?, ?, ?, ?)",
                 (user_id, _hash(token), label, time.time()))
    conn.commit()
    return name, token


def list_tokens(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT t.id, u.name, t.label, t.created_at, t.last_used, t.revoked_at "
        "FROM tokens t JOIN users u ON u.id = t.user_id ORDER BY u.name, t.id").fetchall()


def revoke(conn: sqlite3.Connection, target: str) -> int:
    """Revoca un token por id, o todos los de un usuario por nombre."""
    now = time.time()
    if target.isdigit():
        cur = conn.execute("UPDATE tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (now, int(target)))
    else:
        cur = conn.execute(
            "UPDATE tokens SET revoked_at = ? WHERE revoked_at IS NULL AND user_id IN "
            "(SELECT id FROM users WHERE name = ? OR name LIKE ?)", (now, target, "%\\" + target))
    conn.commit()
    return cur.rowcount


class TokenStore:
    """Resuelve tokens en el servidor y escribe el registro de consultas en segundo plano."""

    CACHE_S = 30

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._local = threading.local()
        self._cache: dict[str, tuple[User | None, int | None, float]] = {}
        self._writes: queue.Queue = queue.Queue(maxsize=10000)
        threading.Thread(target=self._writer, name="hound-log", daemon=True).start()

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._local.conn = db.connect(self.db_path, readonly=True)
        return c

    def resolve(self, token: str | None) -> User | None:
        if not token or not token.startswith(TOKEN_PREFIX):
            return None
        h = _hash(token)
        now = time.monotonic()
        hit = self._cache.get(h)
        if hit and now - hit[2] < self.CACHE_S:  # una revocación tarda como mucho 30 s en aplicarse
            return hit[0]
        row = self._conn().execute(
            "SELECT t.id, u.name, u.sid FROM tokens t JOIN users u ON u.id = t.user_id "
            "WHERE t.hash = ? AND t.revoked_at IS NULL", (h,)).fetchone()
        user = User(row["name"], row["sid"]) if row else None
        self._cache[h] = (user, row["id"] if row else None, now)
        if row:
            self._enqueue("UPDATE tokens SET last_used = ? WHERE id = ?", (time.time(), row["id"]))
        return user

    def log_query(self, user: User, tool: str, args: str, results: int | None, ms: int) -> None:
        self._enqueue("INSERT INTO query_log(ts, user_name, tool, args, results, ms) VALUES (?, ?, ?, ?, ?, ?)",
                      (time.time(), user.name, tool, args, results, ms))

    def _enqueue(self, sql: str, params: tuple) -> None:
        try:
            self._writes.put_nowait((sql, params))
        except queue.Full:
            pass

    def flush(self, timeout: float = 5.0) -> None:
        end = time.monotonic() + timeout
        while not self._writes.empty() and time.monotonic() < end:
            time.sleep(0.05)
        time.sleep(0.1)

    def _writer(self) -> None:
        conn = None
        while True:
            sql, params = self._writes.get()
            try:
                if conn is None:
                    conn = db.connect(self.db_path)
                conn.execute(sql, params)
                conn.commit()
            except Exception:  # noqa: BLE001 — el registro nunca debe tumbar el servidor
                conn = None
