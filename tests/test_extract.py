from __future__ import annotations

import pytest
from conftest import needs_tesseract

from hound.chunking import chunk_blocks, split_long
from hound.extract import Block, ExtractionError, ExtractOptions, extract, extractor_tag
from hound.extract.ocr import clean_ocr
from hound.extract.text import rtf_to_text

OPTS = ExtractOptions()


def _text(path) -> str:
    return "\n".join(b.text for b in extract(str(path), OPTS).blocks)


def test_pdf_por_paginas(corpus):
    ex = extract(str(corpus / "Proveedores/Limpieza/contrato_limpieza_2024.pdf"), OPTS)
    assert [b.group for b in ex.blocks] == ["página 1", "página 2"]
    assert "proveedor de limpieza" in ex.blocks[1].text


def test_pdf_roto_lanza_error(corpus):
    with pytest.raises(ExtractionError):
        extract(str(corpus / "roto.pdf"), OPTS)


def test_word_parrafos_y_tablas(corpus):
    ex = extract(str(corpus / "Obras/Reforma oficina/presupuesto_reforma.docx"), OPTS)
    assert ex.title == "Presupuesto reforma"
    text = "\n".join(b.text for b in ex.blocks)
    assert "Alicatado del baño" in text and "Fontanería | 3.200 €" in text
    assert all(b.unit == "párrafo" for b in ex.blocks)


def test_excel_filas_con_cabecera(corpus):
    ex = extract(str(corpus / "Contabilidad/gastos_2024.xlsx"), OPTS)
    assert ex.blocks[0].group == "hoja Gastos"
    assert ex.blocks[2].text == "Limpieza | Limpiezas Brillo | 850"
    assert ex.blocks[2].context == "Concepto | Proveedor | Importe"


def test_powerpoint(corpus):
    ex = extract(str(corpus / "plan_comercial.pptx"), OPTS)
    assert ex.blocks[0].group == "diapositiva 1" and "sector hotelero" in ex.blocks[0].text


def test_correo_con_adjunto(corpus):
    ex = extract(str(corpus / "Correo/seguro.eml"), OPTS)
    groups = [b.group for b in ex.blocks]
    assert groups[0] == "cabecera" and "cuerpo" in groups
    assert any(g.startswith("adjunto poliza.txt") for g in groups)
    assert "778899" in "\n".join(b.text for b in ex.blocks)


def test_csv_cp1252(corpus):
    assert "Hotel Mar Azul | Benidorm" in _text(corpus / "clientes.csv")


def test_dxf_textos_y_capas(corpus):
    text = _text(corpus / "Obras/Reforma oficina/plano_planta_baja.dxf")
    assert "PLANTA BAJA - COCINA" in text and "Tubería de cobre 22 mm" in text and "FONTANERIA" in text


def test_dwg_sin_oda_solo_nombre(corpus):
    ex = extract(str(corpus / "Obras/Reforma oficina/alzado_fachada.dwg"), OPTS)
    assert ex.blocks == [] and ex.notes


def test_msg_outlook(tmp_path):
    olefile = pytest.importorskip("olefile")
    del olefile
    # .msg mínimo escrito a mano no es trivial; se comprueba que un fichero no OLE se rechaza.
    p = tmp_path / "x.msg"
    p.write_bytes(b"no es ole")
    with pytest.raises(ExtractionError):
        extract(str(p), OPTS)


@needs_tesseract
def test_ocr_imagen(corpus):
    assert "Taller" in _text(corpus / "Contabilidad/Facturas/ticket_taller.png")


@needs_tesseract
def test_ocr_pdf_escaneado(corpus):
    ex = extract(str(corpus / "acta_escaneada.pdf"), OPTS)
    assert ex.blocks and "REUNION" in ex.blocks[0].text.upper()


def test_clean_ocr_descarta_ruido():
    assert clean_ocr("~~ |/ \\ ,,\n_ - . ;\n") == ""
    assert "Factura de compra" in clean_ocr("Factura de compra número 12\n~~~ |||\n")


def test_rtf():
    assert rtf_to_text(r"{\rtf1\ansi{\fonttbl{\f0 Arial;}}\pard Hola \'e1rbol\par Adi\u243?s}").split() == \
        ["Hola", "árbol", "Adiós"]


def test_extractor_tag_cambia_con_capacidades():
    assert extractor_tag("cad", set()) != extractor_tag("cad", {"oda"})
    assert extractor_tag("pdf", {"ocr"}) != extractor_tag("pdf", set())
    assert extractor_tag("word", {"ocr", "oda"}) == extractor_tag("word", set())


def test_chunking_agrupa_y_cita_ubicacion():
    blocks = [Block(f"párrafo número {i} " * 5, unit="párrafo", n=i) for i in range(1, 30)]
    chunks = chunk_blocks(blocks, max_chars=400, overlap=50)
    assert len(chunks) > 3
    assert chunks[0].location.startswith("párrafos 1–")
    assert all(len(c.text) <= 450 for c in chunks)


def test_chunking_no_mezcla_paginas():
    blocks = [Block("uno", group="página 1"), Block("dos", group="página 2")]
    assert [c.location for c in chunk_blocks(blocks)] == ["página 1", "página 2"]


def test_chunking_antepone_cabecera():
    blocks = [Block("A | B", group="hoja X", unit="fila", n=1)] + \
        [Block(f"fila {i} " * 20, group="hoja X", unit="fila", n=i, context="A | B") for i in range(2, 40)]
    chunks = chunk_blocks(blocks, max_chars=500)
    assert all(c.text.startswith("A | B") for c in chunks)


def test_split_long_solapa_y_cubre_todo():
    text = " ".join(f"palabra{i}" for i in range(2000))
    pieces = split_long(text, 500, 100)
    assert all(len(p) <= 500 for p in pieces)
    assert "palabra0" in pieces[0] and "palabra1999" in pieces[-1]
    assert pieces[0].split()[-1] in pieces[1]
