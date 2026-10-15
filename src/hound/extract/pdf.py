"""PDF con pdfium: texto por página y OCR de las páginas escaneadas."""

from __future__ import annotations

from . import Block, ExtractionError, Extracted, ExtractOptions
from .ocr import find_tesseract, ocr_image


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
        meta = pdf.get_metadata_dict(skip_empty=True)
        out.title = meta.get("Title", "")
        can_ocr = opts.ocr.enabled and find_tesseract(opts.ocr) is not None
        ocr_pages = 0
        for i in range(len(pdf)):
            page = pdf[i]
            try:
                textpage = page.get_textpage()
                text = textpage.get_text_bounded().replace("\r\n", "\n").replace("\r", "\n")
                textpage.close()
                if len(text.strip()) < opts.ocr.pdf_min_chars and can_ocr and ocr_pages < opts.ocr.max_pages:
                    img = page.render(scale=opts.ocr.dpi / 72, grayscale=True).to_pil()
                    ocr_text = ocr_image(img, opts.ocr)
                    ocr_pages += 1
                    if len(ocr_text) > len(text.strip()):
                        text = ocr_text
            finally:
                page.close()
            if text.strip():
                out.blocks.append(Block(text, group=f"página {i + 1}"))
        if ocr_pages >= opts.ocr.max_pages:
            out.notes.append(f"OCR limitado a {opts.ocr.max_pages} páginas")
    finally:
        pdf.close()
    return out
