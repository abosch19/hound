"""AutoCAD: textos, atributos y capas de ficheros DXF y DWG.

DXF se lee directamente con ezdxf. DWG es un formato cerrado: para leer su contenido
hace falta ODA File Converter (gratuito, de la Open Design Alliance), que lo convierte
a DXF en una carpeta temporal. Sin él, los DWG se indexan solo por nombre y ruta.
"""

from __future__ import annotations

import glob
import os
import sys
from functools import lru_cache

from ..config import CadConfig
from . import Block, ExtractionError, Extracted, ExtractOptions

MAX_TEXTS = 20000


@lru_cache(maxsize=4)
def _find_oda(configured: str) -> str | None:
    if configured:
        return configured if os.path.isfile(configured) else None
    if sys.platform == "win32":
        found = sorted(glob.glob(r"C:\Program Files\ODA\ODAFileConverter*\ODAFileConverter.exe"))
        return found[-1] if found else None
    return None


def oda_available(cfg: CadConfig) -> bool:
    return _find_oda(cfg.oda_converter) is not None


def _load(path: str, cfg: CadConfig):
    import ezdxf
    from ezdxf import recover

    if path.lower().endswith(".dwg"):
        exe = _find_oda(cfg.oda_converter)
        if not exe:
            return None
        from ezdxf.addons import odafc

        ezdxf.options.set("odafc-addon", "win_exec_path", exe)
        try:
            return odafc.readfile(path)
        except Exception as exc:  # noqa: BLE001
            raise ExtractionError(f"DWG ilegible con ODA File Converter: {exc}") from exc
    try:
        doc, _auditor = recover.readfile(path)
        return doc
    except (OSError, ezdxf.DXFStructureError) as exc:
        raise ExtractionError(f"DXF ilegible: {exc}") from exc


def _entity_texts(entity) -> list[str]:
    t = entity.dxftype()
    try:
        if t == "TEXT" or t == "ATTDEF":
            return [entity.dxf.text]
        if t == "MTEXT":
            return [entity.plain_text()]
        if t == "INSERT":
            return [f"{a.dxf.tag}: {a.dxf.text}" for a in entity.attribs if a.dxf.text.strip()]
        if t == "DIMENSION":
            txt = entity.dxf.get("text", "")
            return [txt] if txt and txt != "<>" else []
        if t in ("MULTILEADER", "MLEADER"):
            ctx = entity.context
            return [ctx.mtext.default_content] if ctx.mtext else []
    except Exception:  # noqa: BLE001 — una entidad corrupta no debe parar el resto
        return []
    return []


def _collect(space, limit: int) -> list[str]:
    seen: dict[str, None] = {}
    for e in space:
        for txt in _entity_texts(e):
            txt = " ".join(txt.split())
            if txt and len(txt) > 1:
                seen.setdefault(txt, None)
        if len(seen) >= limit:
            break
    return list(seen)


def extract_cad(path: str, opts: ExtractOptions) -> Extracted:
    doc = _load(path, opts.cad)
    out = Extracted()
    if doc is None:
        out.notes.append("DWG sin ODA File Converter: indexado solo por nombre")
        return out
    budget = MAX_TEXTS
    for layout in doc.layouts:
        texts = _collect(layout, budget)
        budget -= len(texts)
        if texts:
            name = "espacio modelo" if layout.name.lower() == "model" else f"presentación {layout.name}"
            out.blocks.append(Block("\n".join(texts), group=name))
    # Textos dentro de bloques (cajetines, rótulos), una vez por bloque.
    for block in doc.blocks:
        if budget <= 0:
            break
        if block.is_any_layout or block.name.startswith("*"):
            continue
        texts = _collect(block, budget)
        budget -= len(texts)
        if texts:
            out.blocks.append(Block("\n".join(texts), group=f"bloque {block.name}"))
    layers = [layer.dxf.name for layer in doc.layers if layer.dxf.name not in ("0", "Defpoints")]
    if layers:
        out.blocks.append(Block("Capas: " + ", ".join(layers), group="capas"))
    return out
