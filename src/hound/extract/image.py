"""Imágenes: OCR del texto que contengan (documentos fotografiados, escaneos, planos) y los datos
EXIF de las fotos (fecha en que se tomó, cámara, descripción), para buscar "fotos de marzo de 2024"."""

from __future__ import annotations

from . import Block, ExtractionError, Extracted, ExtractOptions
from .ocr import find_tesseract, ocr_image

MAX_SIDE = 5000
MAX_FRAMES = 50
MIN_VISUAL_PX = 64  # iconos y miniaturas no merecen vector visual


MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
          "octubre", "noviembre", "diciembre"]


def exif_text(img) -> str:
    """Fecha de la foto, cámara y descripción, en texto que se pueda buscar."""
    try:
        exif = img.getexif()
    except Exception:  # noqa: BLE001
        return ""
    if not exif:
        return ""
    sub = {}
    try:
        sub = exif.get_ifd(0x8769)  # ExifIFD: DateTimeOriginal
    except Exception:  # noqa: BLE001
        pass
    parts = []
    when = str(sub.get(36867) or exif.get(306) or "").strip()  # DateTimeOriginal, DateTime
    try:
        date_part = when.split()[0].replace(":", "-")
        y, m, d = (int(x) for x in date_part.split("-")[:3])
        parts.append(f"Foto tomada el {d} de {MONTHS[m - 1]} de {y} ({y:04d}-{m:02d}-{d:02d}"
                     + (f" {when.split()[1][:5]}" if len(when.split()) > 1 else "") + ")")
    except (ValueError, IndexError):
        pass
    camera = " ".join(str(exif.get(t) or "").strip("\x00 ") for t in (271, 272)).strip()  # Make, Model
    if camera:
        parts.append(f"Cámara: {camera}")
    for tag, label in ((270, "Descripción"), (315, "Autor"), (33432, "Copyright")):
        v = str(exif.get(tag) or "").strip("\x00 ")
        if v:
            parts.append(f"{label}: {v}")
    return ". ".join(parts)


def extract_image(path: str, opts: ExtractOptions) -> Extracted:
    out = Extracted()
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = 400_000_000
    try:
        img = Image.open(path)
    except Exception as exc:  # noqa: BLE001
        raise ExtractionError(f"Imagen ilegible: {exc}") from exc
    with img:
        if opts.visual and min(img.size) >= MIN_VISUAL_PX:
            from ..embed import image_pixels

            out.pixels = image_pixels(ImageOps.exif_transpose(img), opts.image_tokens)
        if opts.visual_only:
            return out
        meta = exif_text(img)
        if meta:
            out.blocks.append(Block(meta, group="datos de la foto"))
        if not (opts.ocr.enabled and opts.ocr.images and find_tesseract(opts.ocr)):
            return out
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
