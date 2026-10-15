"""Cuánto cuesta indexar cada tipo de fichero, aprendido de lo ya indexado.

Para cada tipo se guardan las últimas muestras (MB, segundos) y se ajusta
segundos ≈ fijo + por_MB × MB. Los segundos son de reloj: la extracción en paralelo se divide
entre los procesos de extracción y se suma la parte del cálculo de vectores. Con pocas
muestras se usan valores por defecto razonables (OCR de imágenes y PDF escaneados caros,
texto barato). Sirve para el tiempo estimado de `index_status` y para `hound estimate`.
"""

from __future__ import annotations

import json
import sqlite3

import numpy as np

from . import db

MAX_SAMPLES = 200
MIN_SAMPLES = 8

# (segundos fijos por fichero, segundos por MB) sin muestras propias
DEFAULTS = {
    "text": (0.05, 0.5), "word": (0.3, 1.0), "excel": (0.4, 2.0), "powerpoint": (0.4, 1.0),
    "email": (0.3, 1.0), "pdf": (1.0, 4.0), "image": (4.0, 1.0), "cad": (0.5, 2.0), "other": (0.02, 0.0),
    "_reembed": (0.0, 3.0),  # por cada 100 fragmentos
    "_visual": (2.0, 0.0),   # vector visual de una imagen ya indexada
}


class CostModel:
    def __init__(self, samples: dict[str, list[list[float]]] | None = None):
        self.samples = samples or {}
        self._fit: dict[str, tuple[float, float]] = {}

    @classmethod
    def load(cls, conn: sqlite3.Connection) -> CostModel:
        try:
            return cls(json.loads(db.get_meta(conn, "costs") or "{}"))
        except ValueError:
            return cls()

    def save(self, conn: sqlite3.Connection) -> None:
        db.set_meta(conn, "costs", json.dumps(self.samples, separators=(",", ":")))

    def add(self, kind: str, mb: float, seconds: float) -> None:
        s = self.samples.setdefault(kind, [])
        s.append([round(mb, 4), round(seconds, 4)])
        del s[:-MAX_SAMPLES]
        self._fit.pop(kind, None)

    def params(self, kind: str) -> tuple[float, float]:
        if kind in self._fit:
            return self._fit[kind]
        d = DEFAULTS.get(kind, DEFAULTS["other"])
        s = self.samples.get(kind, [])
        if len(s) < MIN_SAMPLES:
            fit = d
        else:
            a = np.array(s, dtype=np.float64)
            mb, sec = a[:, 0], a[:, 1]
            # Las muestras recientes pesan más (la carga del servidor cambia).
            w = np.sqrt(np.linspace(0.3, 1.0, len(a)))
            if np.ptp(mb) < 1e-3:
                fit = (float(np.average(sec, weights=w ** 2)), 0.0)
            else:
                X = np.stack([np.ones_like(mb), mb], axis=1) * w[:, None]
                (fixed, per_mb), *_ = np.linalg.lstsq(X, sec * w, rcond=None)
                if per_mb < 0:
                    fixed, per_mb = float(np.average(sec, weights=w ** 2)), 0.0
                elif fixed < 0:
                    fixed, per_mb = 0.0, float(np.sum(sec * mb) / max(np.sum(mb * mb), 1e-9))
                fit = (float(fixed), float(per_mb))
        self._fit[kind] = fit
        return fit

    def estimate(self, kind: str, n: int, mb: float) -> float:
        """Segundos para indexar n ficheros de ese tipo que suman mb megas."""
        fixed, per_mb = self.params(kind)
        return n * fixed + mb * per_mb

    def learned(self) -> dict[str, int]:
        return {k: len(v) for k, v in self.samples.items()}


def backlog_seconds(conn: sqlite3.Connection, model: CostModel | None = None) -> float:
    """Tiempo estimado para lo pendiente, lo que falta de los PDF a medias, los vectores por
    recalcular tras un cambio de modelo y los vectores visuales de imágenes en cola."""
    model = model or CostModel.load(conn)
    gen = int(db.get_meta(conn, "model_gen", "0"))
    segs = conn.execute("SELECT count(*) FROM segments s JOIN files f ON f.id = s.file_id "
                        "WHERE f.emb < ? AND f.status != 'pending'", (gen,)).fetchone()[0] if gen else 0
    total = model.estimate("_reembed", 0, segs / 100)
    images = conn.execute("SELECT count(*) FROM files WHERE kind = 'image' AND visual = 0 AND status = 'indexed'"
                          ).fetchone()[0]
    if images and db.get_meta(conn, "vision") == "1":
        total += model.estimate("_visual", images, 0.0)
    for r in conn.execute("SELECT kind, count(*) n, coalesce(sum(size), 0) b FROM files "
                          "WHERE status = 'pending' GROUP BY kind"):
        total += model.estimate(r["kind"], r["n"], r["b"] / 1e6)
    for r in conn.execute("SELECT kind, size, pages_done, pages_total FROM files WHERE status = 'partial'"):
        left = 1.0 - (r["pages_done"] or 0) / max(1, r["pages_total"] or 1)
        total += model.estimate(r["kind"], 1, r["size"] / 1e6) * max(0.0, left)
    return total
