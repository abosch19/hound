"""PDF con pdfium: texto por página y OCR de las páginas escaneadas o con el texto estropeado.

Los PDF largos se leen por tramos (`page_start`/`page_limit`): el indexador guarda cada tramo
en cuanto lo tiene, así que un escaneo de cientos de páginas ya se puede buscar desde el
principio y una parada o un tiempo agotado no pierden lo ya leído.
"""

from __future__ import annotations

import unicodedata

from . import Block, ExtractionError, Extracted, ExtractOptions
from .ocr import find_tesseract, ocr_image


def garbled(text: str) -> bool:
    """Capa de texto inservible: fuentes sin mapa de caracteres (símbolos, "(cid:12)", uso privado)."""
    t = "".join(text.split())
    if len(t) < 40:
        return False
    if t.count("(cid:") >= 3:
        return True
    bad = sum(1 for c in t if c == "\ufffd" or unicodedata.category(c) in ("Co", "Cc", "Cn"))
    letters = sum(1 for c in t if c.isalpha())
    return bad / len(t) > 0.1 or letters / len(t) < 0.3 and not any(c.isdigit() for c in t[:200])


def extract_pdf(path: str, opts: ExtractOptions) -> Extracted:
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(path)
    except pdfium.PdfiumError as exc:
        if "password" in str(exc).lower():
            raise ExtractionError("PDF protegido con contraseña") from exc
        raise ExtractionError(f"PDF ilegible: {exc}") from exc

    out = Extracted()
    try:
        total = len(pdf)
        start = max(0, opts.page_start)
        end = total
        # Un tramo que dejaría muy poco para el siguiente se lee entero.
        if opts.page_limit and total - start > opts.page_limit * 5 // 3:
            end = start + opts.page_limit
        out.pages_total, out.pages_done = total, end
        if start == 0:
            out.title = pdf.get_metadata_dict(skip_empty=True).get("Title", "")
        can_ocr = opts.ocr.enabled and find_tesseract(opts.ocr) is not None
        for i in range(start, end):
            page = pdf[i]
            try:
                textpage = page.get_textpage()
                text = textpage.get_text_bounded().replace("\r\n", "\n").replace("\r", "\n")
                textpage.close()
                bad = garbled(text)
                if (len(text.strip()) < opts.ocr.pdf_min_chars or bad) and can_ocr and i < opts.ocr.max_pages:
                    img = page.render(scale=opts.ocr.dpi / 72, grayscale=True).to_pil()
                    ocr_text = ocr_image(img, opts.ocr)
                    if bad or len(ocr_text) > len(text.strip()):
                        text = ocr_text
                elif bad:
                    text = ""  # mejor nada que basura en el índice
            finally:
                page.close()
            if text.strip():
                out.blocks.append(Block(text, group=f"página {i + 1}"))
        if end == total and total > opts.ocr.max_pages and can_ocr:
            out.notes.append(f"OCR solo en las primeras {opts.ocr.max_pages} páginas")
    finally:
        pdf.close()
    return out
