"""Estado de la indexación, para `hound status` y la herramienta MCP `index_status`.

El indexador corre en otro proceso: deja su fase en la tabla meta ("indexer_state") y el resto
se calcula con el propio índice. Solo son recuentos: no se devuelve ninguna ruta, así que
cualquier usuario con token puede verlo sin saber nada de ficheros que no puede abrir.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import time
from typing import Any

from . import db

STALE_S = 900  # sin latido en este tiempo, el indexador se da por parado

PHASES = {
    "starting": "Arrancando el indexador (cargando el modelo)",
    "scanning": "Recorriendo la carpeta compartida en busca de cambios",
    "indexing": "Extrayendo texto e indexando ficheros",
    "paused": "En pausa durante el horario laboral (busy_mode = pause); sigue por la noche",
    "idle": "Al día: vigilando cambios",
    "stopped": "El indexador no está en marcha",
}


def set_state(conn: sqlite3.Connection, phase: str, **extra: Any) -> None:
    """Lo llama el indexador. No hace commit: va en la misma transacción que su trabajo."""
    now = time.time()
    prev = db.get_meta(conn, "indexer_state")
    since = now
    if prev:
        try:
            p = json.loads(prev)
            if p.get("phase") == phase:
                since = p.get("since", now)
        except ValueError:
            pass
    db.set_meta(conn, "indexer_state", json.dumps({"phase": phase, "since": since, **extra}))
    db.set_meta(conn, "indexer_heartbeat", str(now))


def _iso(ts: float | str | None) -> str | None:
    if not ts:
        return None
    return dt.datetime.fromtimestamp(float(ts)).isoformat(timespec="seconds")


def index_status(conn: sqlite3.Connection, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    now = time.time()
    counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, count(*) n FROM files GROUP BY status")}
    total = sum(counts.values())
    indexed, pending, errors = counts.get("indexed", 0), counts.get("pending", 0), counts.get("error", 0)
    hb = db.get_meta(conn, "indexer_heartbeat")
    try:
        state = json.loads(db.get_meta(conn, "indexer_state") or "{}")
    except ValueError:
        state = {}
    alive = bool(hb) and now - float(hb) < STALE_S
    phase = state.get("phase", "starting") if alive else "stopped"

    # Ritmo de los últimos 15 minutos, para estimar cuánto falta.
    recent, oldest = conn.execute("SELECT count(*), min(indexed_at) FROM files WHERE indexed_at > ? "
                                  "AND status != 'pending'", (now - 900,)).fetchone()
    per_hour = recent * 3600 / max(60.0, now - oldest) if recent else 0.0
    eta_min = round(pending / per_hour * 60) if per_hour and pending else None

    out: dict[str, Any] = {
        "phase": phase,
        "description": PHASES.get(phase, phase),
        "phase_since": _iso(state.get("since")) if alive else None,
        "progress_percent": round(100 * (indexed + errors) / total, 1) if total else None,
        "files": {"total": total, "indexed": indexed, "pending": pending, "errors": errors},
        "files_per_hour": round(per_hour),
        "eta_minutes": eta_min,
        "last_full_scan": _iso(db.get_meta(conn, "last_full_scan")),
        "last_heartbeat": _iso(hb),
        "by_type": {r["kind"]: r["n"] for r in conn.execute(
            "SELECT kind, count(*) n FROM files GROUP BY kind ORDER BY n DESC")},
    }
    if alive and phase == "scanning" and state.get("seen") is not None:
        out["scan_files_seen"] = state["seen"]
    if extra:
        out.update(extra)
    return out


def format_status(st: dict[str, Any]) -> str:
    f = st["files"]
    lines = [f"Indexador: {st['description']}" + (f" (desde {st['phase_since']})" if st.get("phase_since") else "")]
    if st.get("scan_files_seen") is not None:
        lines.append(f"  Escaneo en curso: {st['scan_files_seen']} ficheros vistos")
    pct = f"{st['progress_percent']}%" if st["progress_percent"] is not None else "-"
    lines.append(f"Progreso: {pct} · {f['indexed']} indexados, {f['pending']} pendientes, {f['errors']} con error "
                 f"(de {f['total']})")
    if st["files_per_hour"]:
        eta = f", faltan ~{st['eta_minutes']} min" if st["eta_minutes"] is not None else ""
        lines.append(f"Ritmo: {st['files_per_hour']} ficheros/hora{eta}")
    lines.append(f"Último escaneo completo: {st['last_full_scan'] or '-'}")
    lines.append(f"Último latido del indexador: {st['last_heartbeat'] or '-'}")
    return "\n".join(lines)
