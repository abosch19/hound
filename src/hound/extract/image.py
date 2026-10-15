"""Imágenes: OCR del texto que contengan (documentos fotografiados, escaneos, planos)."""

from __future__ import annotations

from . import Block, ExtractionError, Extracted, ExtractOptions
from .ocr import find_tesseract, ocr_image

MAX_SIDE = 5000
MAX_FRAMES = 50


def extract_image(path: str, opts: ExtractOptions) -> Extracted:
    out = Extracted()
    if not (opts.ocr.enabled and opts.ocr.images and find_tesseract(opts.ocr)):
        return out
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = 400_000_000
    try:
        img = Image.open(path)
    except Exception as exc:  # noqa: BLE001
        raise ExtractionError(f"Imagen ilegible: {exc}") from exc
    with img:
        frames = getattr(img, "n_frames", 1) if path.lower().endswith((".tif", ".tiff")) else 1
        for i in range(min(frames, MAX_FRAMES)):
            img.seek(i)
            if min(img.size) < opts.ocr.min_image_px:
                continue
            frame = ImageOps.exif_transpose(img).convert("L")
            if max(frame.size) > MAX_SIDE:
                frame.thumbnail((MAX_SIDE, MAX_SIDE))
            text = ocr_image(frame, opts.ocr)
            if text:
                out.blocks.append(Block(text, group=f"página {i + 1}" if frames > 1 else "imagen"))
    return out
