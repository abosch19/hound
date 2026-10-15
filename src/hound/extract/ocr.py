"""OCR con Tesseract (proceso externo, local). Nada sale del servidor."""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
from functools import lru_cache

from ..config import OcrConfig

_WIN_DEFAULTS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
]


@lru_cache(maxsize=8)
def _find(configured: str) -> str | None:
    if configured:
        return configured if os.path.isfile(configured) else None
    found = shutil.which("tesseract")
    if found:
        return found
    if sys.platform == "win32":
        for cand in _WIN_DEFAULTS:
            if os.path.isfile(cand):
                return cand
    return None


def find_tesseract(cfg: OcrConfig) -> str | None:
    return _find(cfg.tesseract)


def _creationflags() -> int:
    if sys.platform == "win32":
        return subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    return 0


def ocr_image(img, cfg: OcrConfig, timeout: int = 180) -> str:
    """OCR de una imagen PIL. Devuelve "" si Tesseract no está disponible."""
    exe = find_tesseract(cfg)
    if not exe:
        return ""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    cmd = [exe, "stdin", "stdout", "-l", cfg.languages, "--psm", "3"]
    if cfg.tessdata:
        cmd[1:1] = ["--tessdata-dir", cfg.tessdata]
    env = dict(os.environ, OMP_THREAD_LIMIT="1")
    proc = subprocess.run(cmd, input=buf.getvalue(), capture_output=True, timeout=timeout,
                          env=env, creationflags=_creationflags())
    if proc.returncode != 0:
        msg = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError("Tesseract falló: " + (msg[-1] if msg else f"código {proc.returncode}"))
    return clean_ocr(proc.stdout.decode("utf-8", "replace"))


_WORD = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñÀÈÒÏÇàèòïç0-9]{2,}")


def clean_ocr(text: str) -> str:
    """Quita el ruido típico del OCR sobre fotos y planos: líneas sin palabras reconocibles."""
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            if lines and lines[-1]:
                lines.append("")
            continue
        words = _WORD.findall(line)
        letters = sum(len(w) for w in words)
        if words and letters >= 3 and letters / max(1, len(line.replace(" ", ""))) >= 0.5:
            lines.append(line)
    out = "\n".join(lines).strip()
    return out if sum(c.isalpha() for c in out) >= 15 else ""
