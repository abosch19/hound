"""Rutas: el índice guarda rutas relativas a la carpeta compartida, con "/" como separador.

En Windows los nombres no distinguen mayúsculas, así que todas las comparaciones
(exclusiones, filtros por carpeta) se hacen en minúsculas.
"""

from __future__ import annotations

import fnmatch
import os

from .config import Config


def norm_rel(rel: str) -> str:
    rel = rel.replace("\\", "/").strip("/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError("La ruta no puede contener '..'")
    return "/".join(parts)


def to_rel(abs_path: str, root: str) -> str:
    return norm_rel(os.path.relpath(abs_path, root))


def to_abs(rel: str, root: str) -> str:
    rel = norm_rel(rel)
    return os.path.join(root, *rel.split("/")) if rel else root


def display_path(rel: str, cfg: Config) -> str:
    """Ruta que ve el usuario: UNC si está configurada, si no la ruta local del servidor."""
    base = cfg.share.unc or cfg.share.path
    sep = "\\" if ("\\" in base or base.endswith(":")) else "/"
    base = base.rstrip("\\/")
    return base + sep + rel.replace("/", sep) if rel else base


def parse_user_path(path: str, cfg: Config) -> str:
    """Convierte lo que escribe el usuario (UNC, ruta local o relativa) en ruta relativa.

    Lanza ValueError si la ruta apunta fuera de la carpeta compartida.
    """
    p = (path or "").strip().strip('"').replace("\\", "/")
    for base in (cfg.share.unc, cfg.share.path):
        if not base:
            continue
        b = base.replace("\\", "/").rstrip("/")
        if p.lower() == b.lower():
            return ""
        if p.lower().startswith(b.lower() + "/"):
            return norm_rel(p[len(b) + 1:])
    if p.startswith("//") or (len(p) > 1 and p[1] == ":"):
        raise ValueError("La ruta está fuera de la carpeta compartida")
    return norm_rel(p)


def is_under(rel: str, folder: str) -> bool:
    """True si rel está dentro de folder (o es folder). Sin distinguir mayúsculas."""
    if not folder:
        return True
    r, f = rel.lower(), folder.lower()
    return r == f or r.startswith(f + "/")


class Excluder:
    def __init__(self, cfg: Config):
        self.prefixes = [norm_rel(p).lower() for p in cfg.index.exclude if norm_rel(p)]
        self.patterns = [p.lower() for p in cfg.index.exclude_names]

    def name_excluded(self, name: str) -> bool:
        n = name.lower()
        return any(fnmatch.fnmatchcase(n, pat) for pat in self.patterns)

    def excluded(self, rel: str) -> bool:
        if any(is_under(rel, p) for p in self.prefixes):
            return True
        return any(self.name_excluded(part) for part in rel.split("/"))
