"""Extracción de texto por tipo de fichero.

Cada extractor devuelve una lista de `Block`: un trozo de texto con su ubicación
(página, hoja y fila, diapositiva, párrafo…). El troceado posterior (chunking.py)
agrupa bloques consecutivos de la misma ubicación en fragmentos de tamaño similar.

Los extractores solo abren los ficheros en modo lectura.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from ..config import CadConfig, OcrConfig


@dataclass
class Block:
    text: str
    group: str = ""
    """Ubicación gruesa: "página 3", "hoja Ventas", "diapositiva 2", "adjunto factura.pdf, página 1"…"""
    unit: str = ""
    """Unidad fina dentro del grupo ("párrafo", "fila", "línea"); vacía si no aplica."""
    n: int | None = None
    context: str = ""
    """Texto que se antepone a cada fragmento del grupo (p. ej. la fila de cabecera de una hoja)."""


@dataclass
class Extracted:
    blocks: list[Block] = field(default_factory=list)
    title: str = ""
    notes: list[str] = field(default_factory=list)
    pages_total: int | None = None
    pages_done: int | None = None
    """PDF por tramos: páginas leídas hasta ahora (si es menor que pages_total, queda por leer)."""


@dataclass
class ExtractOptions:
    ocr: OcrConfig = field(default_factory=OcrConfig)
    cad: CadConfig = field(default_factory=CadConfig)
    attachments: bool = True
    page_start: int = 0
    """PDF: primera página a leer (0 = desde el principio)."""
    page_limit: int = 0
    """PDF: cuántas páginas leer en esta pasada (0 = todas)."""


KINDS = {
    "pdf": {".pdf"},
    "word": {".docx", ".docm", ".odt", ".doc", ".rtf"},
    "excel": {".xlsx", ".xlsm", ".xls", ".ods", ".csv"},
    "powerpoint": {".pptx", ".pptm", ".odp", ".ppt"},
    "email": {".msg", ".eml"},
    "text": {".txt", ".md", ".log", ".html", ".htm"},
    "image": {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp", ".gif", ".heic"},
    "cad": {".dxf", ".dwg"},
}
EXT_KIND = {ext: kind for kind, exts in KINDS.items() for ext in exts}

# Sube el número cuando cambie un extractor para que se reprocesen esos ficheros. Ojo: subir "pdf" o
# "image" repite el OCR de todos ellos. Las mejoras de PDF por tramos, texto estropeado y EXIF no lo
# suben a propósito: se aplican a lo nuevo y a lo que cambie ("hound reindex --path" para forzarlo).
EXTRACTOR_VERSION = {
    "pdf": 1, "word": 1, "excel": 1, "powerpoint": 1, "email": 1,
    "text": 1, "image": 1, "cad": 1, "other": 1,
}


def kind_of(ext: str) -> str:
    return EXT_KIND.get(ext.lower(), "other")


class ExtractionError(Exception):
    pass


def capabilities(opts: ExtractOptions) -> set[str]:
    from .cad import oda_available
    from .ocr import find_tesseract

    caps = set()
    if opts.ocr.enabled and find_tesseract(opts.ocr):
        caps.add("ocr")
    if oda_available(opts.cad):
        caps.add("oda")
    return caps


def extractor_tag(kind: str, caps: set[str]) -> str:
    """Identifica con qué versión y capacidades se procesó un fichero.

    Si cambia (p. ej. se instala Tesseract u ODA File Converter), el fichero se reprocesa.
    """
    used = []
    if kind in ("pdf", "image", "email") and "ocr" in caps:
        used.append("ocr")
    if kind == "cad" and "oda" in caps:
        used.append("oda")
    return f"{kind}{EXTRACTOR_VERSION.get(kind, 1)}" + "".join("+" + u for u in used)


def extract(path: str, opts: ExtractOptions, depth: int = 0) -> Extracted:
    ext = os.path.splitext(path)[1].lower()
    kind = kind_of(ext)
    if kind == "pdf":
        from .pdf import extract_pdf
        return extract_pdf(path, opts)
    if ext in (".docx", ".docm"):
        from .office import extract_docx
        return extract_docx(path)
    if ext in (".xlsx", ".xlsm"):
        from .office import extract_xlsx
        return extract_xlsx(path)
    if ext == ".xls":
        from .office import extract_xls
        return extract_xls(path)
    if ext in (".pptx", ".pptm"):
        from .office import extract_pptx
        return extract_pptx(path)
    if ext in (".odt", ".ods", ".odp"):
        from .office import extract_odf
        return extract_odf(path)
    if ext == ".rtf":
        from .text import extract_rtf
        return extract_rtf(path)
    if ext == ".csv":
        from .text import extract_csv
        return extract_csv(path)
    if ext in (".html", ".htm"):
        from .text import extract_html
        return extract_html(path)
    if kind == "text":
        from .text import extract_text
        return extract_text(path)
    if ext == ".eml":
        from .email import extract_eml
        return extract_eml(path, opts, depth)
    if ext == ".msg":
        from .email import extract_msg
        return extract_msg(path, opts, depth)
    if kind == "image":
        from .image import extract_image
        return extract_image(path, opts)
    if kind == "cad":
        from .cad import extract_cad
        return extract_cad(path, opts)
    # .doc, .ppt, comprimidos, vídeo…: solo se indexan por nombre y ruta.
    return Extracted()
