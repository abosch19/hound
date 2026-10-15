"""Troceado: agrupa bloques consecutivos de la misma ubicación en fragmentos de tamaño similar.

Cada fragmento conserva su ubicación ("página 3", "hoja Ventas, filas 2–41",
"párrafos 10–14") para poder citar dónde está la coincidencia.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import groupby

from .extract import Block

PLURAL = {"párrafo": "párrafos", "fila": "filas", "línea": "líneas"}


@dataclass
class Chunk:
    text: str
    location: str


def has_words(text: str, min_ratio: float = 0.2) -> bool:
    """True si el texto tiene suficientes letras para que su vector signifique algo."""
    t = "".join(text.split())
    return bool(t) and sum(c.isalpha() for c in t) / len(t) >= min_ratio


def _location(group: str, unit: str, first: int | None, last: int | None) -> str:
    parts = [group] if group else []
    if unit and first is not None:
        if last is None or last == first:
            parts.append(f"{unit} {first}")
        else:
            parts.append(f"{PLURAL.get(unit, unit)} {first}–{last}")
    return ", ".join(parts) or "texto"


def split_long(text: str, max_chars: int, overlap: int) -> list[str]:
    """Parte un texto largo por frases o espacios, con solape entre trozos."""
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []
    pieces = []
    start = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        if end < len(text):
            window = text[start + max_chars // 2:end]
            cut = max(window.rfind(". "), window.rfind("\n"), window.rfind("; "))
            if cut < 0:
                cut = window.rfind(" ")
            if cut >= 0:
                end = start + max_chars // 2 + cut + 1
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        if end >= len(text):
            break
        nxt = max(end - overlap, start + 1)
        sp = text.find(" ", nxt)
        start = sp + 1 if 0 <= sp < end else nxt
    return pieces


_WS = re.compile(r"[ \t\xa0]+")


def _clean(text: str) -> str:
    text = _WS.sub(" ", text.replace("\x00", ""))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def chunk_blocks(blocks: list[Block], max_chars: int = 1400, overlap: int = 200) -> list[Chunk]:
    chunks: list[Chunk] = []
    for group, items in groupby(blocks, key=lambda b: b.group):
        items = list(items)
        context = next((b.context for b in items if b.context), "")
        budget = max(200, max_chars - len(context)) if context else max_chars
        buf: list[str] = []
        size = 0
        unit = ""
        first = last = None

        def emit():
            nonlocal buf, size, first, last
            if buf:
                body = "\n".join(buf)
                text = f"{context}\n{body}" if context and not body.startswith(context) else body
                chunks.append(Chunk(text, _location(group, unit, first, last)))
            buf, size, first, last = [], 0, None, None

        for b in items:
            text = _clean(b.text)
            if not text:
                continue
            unit = b.unit or unit
            if len(text) > budget:
                emit()
                for piece in split_long(text, budget, overlap):
                    chunks.append(Chunk(f"{context}\n{piece}" if context else piece,
                                        _location(group, b.unit, b.n, b.n)))
                continue
            if size + len(text) + 1 > budget:
                emit()
            buf.append(text)
            size += len(text) + 1
            if b.n is not None:
                first = b.n if first is None else first
                last = b.n
        emit()
    return chunks
