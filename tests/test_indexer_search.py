"""Indexado incremental, solo lectura, ranking híbrido y filtros."""

from __future__ import annotations

import hashlib
import os
import time

import numpy as np
import pytest

from hound.embed import HashEmbedder
from hound.indexer import Indexer, name_chunk, parse_schedule
from hound.search import SearchIndex, User, excerpt, fts_query
from hound.security import NoSecurity

ANY = User("EMPRESA\\alice", "")


def _snapshot(root):
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(dirpath, f)
            st = os.stat(p)
            out[p] = (st.st_size, st.st_mtime_ns, hashlib.md5(open(p, "rb").read()).hexdigest())
    return out


def _index_all(ix):
    ix.scan()
    while ix.process_pending():
        pass


@pytest.fixture
def setup(cfg):
    cfg.permissions.mode = "none"
    cfg.ocr.enabled = False
    emb = HashEmbedder(256)
    ix = Indexer(cfg, emb, NoSecurity())
    _index_all(ix)
    index = SearchIndex(cfg, emb, NoSecurity())
    yield cfg, ix, index
    ix.close()


def test_indexado_es_solo_lectura(cfg, corpus):
    cfg.permissions.mode = "none"
    before = _snapshot(corpus)
    ix = Indexer(cfg, HashEmbedder(256), NoSecurity())
    _index_all(ix)
    ix.close()
    assert _snapshot(corpus) == before


def test_estados_y_errores_sin_parar(setup):
    _cfg, ix, _index = setup
    rows = {r["path"]: r for r in ix.conn.execute("SELECT path, status, error FROM files")}
    assert rows["roto.pdf"]["status"] == "error" and "PDF" in rows["roto.pdf"]["error"]
    assert rows["notas.txt"]["status"] == "indexed"
    assert "Thumbs.db" not in rows and "~$borrador.docx" not in rows
    # El DWG sin ODA se indexa por nombre, con aviso.
    dwg = rows["Obras/Reforma oficina/alzado_fachada.dwg"]
    assert dwg["status"] == "indexed" and "ODA" in dwg["error"]


def test_busqueda_basica_y_ubicacion(setup):
    _cfg, _ix, index = setup
    res = index.search(ANY, "proveedor de limpieza clausula")
    assert res[0]["name"] == "contrato_limpieza_2024.pdf"
    assert res[0]["location"] == "página 2"
    assert res[0]["path"] == r"\\SERVIDOR\Compartido\Proveedores\Limpieza\contrato_limpieza_2024.pdf"
    assert "**" in res[0]["excerpt"]


def test_fichero_roto_se_encuentra_por_nombre(setup):
    _cfg, _ix, index = setup
    assert any(r["name"] == "roto.pdf" for r in index.search(ANY, "roto"))


def test_dwg_por_nombre(setup):
    _cfg, _ix, index = setup
    assert index.search(ANY, "alzado fachada", file_type="cad")[0]["name"] == "alzado_fachada.dwg"


def test_filtros(setup):
    _cfg, ix, index = setup
    assert all(r["type"] == "excel" for r in index.search(ANY, "limpieza", file_type="excel"))
    assert all("Obras" in r["path"] for r in index.search(ANY, "reforma", folder="obras"))
    with pytest.raises(ValueError):
        index.search(ANY, "x", file_type="desconocido")
    old = time.time() - 400 * 86400
    p = os.path.join(ix.root, "notas.txt")
    os.utime(p, (old, old))
    ix.scan()
    while ix.process_pending():
        pass
    index.refresh()
    since = time.strftime("%Y-%m-%d", time.localtime(time.time() - 30 * 86400))
    assert all(r["name"] != "notas.txt" for r in index.search(ANY, "caldera técnico", date_from=since))
    assert any(r["name"] == "notas.txt" for r in index.search(ANY, "caldera técnico", date_to=since))


def test_incremental_nuevo_modificado_borrado_renombrado(setup):
    cfg, ix, index = setup
    root = ix.root
    p = os.path.join(root, "nuevo.txt")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("Inventario de sillas ergonómicas del almacén")
    _index_all(ix)
    index.refresh()
    assert index.search(ANY, "sillas ergonómicas")[0]["name"] == "nuevo.txt"

    with open(p, "w", encoding="utf-8") as fh:
        fh.write("Lista de proveedores de café para la máquina")
    os.utime(p, (time.time() + 5, time.time() + 5))
    _index_all(ix)
    index.refresh()
    top = index.search(ANY, "proveedores de café máquina")[0]
    assert top["name"] == "nuevo.txt" and "café" in top["excerpt"]
    assert not any("sillas" in r["excerpt"] for r in index.search(ANY, "sillas ergonómicas almacén"))

    os.rename(p, os.path.join(root, "Obras", "cafe.txt"))
    _index_all(ix)
    index.refresh()
    names = [r["path"] for r in index.search(ANY, "proveedores de café máquina")]
    assert names[0].endswith(r"Obras\cafe.txt") and not any(n.endswith("nuevo.txt") for n in names)

    os.remove(os.path.join(root, "Obras", "cafe.txt"))
    _index_all(ix)
    index.refresh()
    assert not any(r["name"] == "cafe.txt" for r in index.search(ANY, "proveedores de café máquina"))


def test_cambio_de_extractor_reprocesa(setup):
    _cfg, ix, _index = setup
    ix.caps = {"oda"}
    st = ix.scan()
    assert st.changed >= 2  # el DXF y el DWG se vuelven a procesar con ODA


def test_carpeta_ilegible_no_borra_su_contenido(setup, monkeypatch):
    _cfg, ix, _index = setup
    real = os.scandir

    def fake(path):
        if str(path).endswith("Contabilidad"):
            raise PermissionError("acceso denegado")
        return real(path)

    monkeypatch.setattr(os, "scandir", fake)
    st = ix.scan()
    assert st.deleted == 0 and st.errors


def test_rrf_combina_vectores_y_palabras(setup):
    _cfg, _ix, index = setup
    # "778899" solo aparece como palabra clave en el adjunto del correo.
    res = index.search(ANY, "póliza 778899")
    assert res[0]["name"] == "seguro.eml"
    assert "adjunto poliza.txt" in res[0]["location"]


def test_read_document_por_tramos(setup):
    _cfg, _ix, index = setup
    doc = index.read_document(ANY, "Proveedores/Limpieza/contrato_limpieza_2024.pdf", 0, 1)
    assert doc["total_segments"] == 2 and len(doc["segments"]) == 1
    assert doc["segments"][0]["location"] == "página 1" and doc["next_start"] == 1
    rest = index.read_document(ANY, doc["path"], doc["next_start"])
    assert rest["segments"][0]["location"] == "página 2" and rest["next_start"] is None


def test_recarga_en_memoria(setup):
    _cfg, ix, index = setup
    before = sum(int(b.alive.sum()) for b in index.blocks)
    os.remove(os.path.join(ix.root, "notas.txt"))
    _index_all(ix)
    index.refresh()
    after = sum(int(b.alive.sum()) for b in index.blocks)
    assert after < before


def test_utilidades():
    assert fts_query('el "contrato" de limpieza') == '"contrato"* OR "limpieza"*'
    assert fts_query("de la") == ""
    ex = excerpt("Texto previo. " * 40 + "El contrato de limpieza firmado.", ["contrato"])
    assert "**contrato**" in ex and ex.startswith("…")
    assert excerpt("Camión rápido", ["camion"]) == "**Camión** rápido"
    assert name_chunk("Obras/Cliente_X/presupuesto-cocina.v2.pdf").text == \
        "presupuesto cocina v2\nCarpeta: Obras / Cliente_X"


def test_horario_laboral(cfg):
    import datetime as dt

    assert parse_schedule(cfg, dt.datetime(2026, 10, 14, 10, 0))      # miércoles
    assert not parse_schedule(cfg, dt.datetime(2026, 10, 14, 21, 0))
    assert not parse_schedule(cfg, dt.datetime(2026, 10, 17, 10, 0))  # sábado


def test_float16_en_memoria(cfg):
    cfg.permissions.mode = "none"
    cfg.ocr.enabled = False
    cfg.search.memory_dtype = "float16"
    emb = HashEmbedder(256)
    ix = Indexer(cfg, emb, NoSecurity())
    _index_all(ix)
    ix.close()
    index = SearchIndex(cfg, emb, NoSecurity())
    assert index.blocks[0].vecs.dtype == np.float16
    assert index.search(ANY, "proveedor de limpieza")[0]["name"] == "contrato_limpieza_2024.pdf"


def test_escala_un_millon_de_fragmentos_en_menos_de_un_segundo(cfg):
    """Rendimiento de la parte vectorial con 1M de vectores de 768 dimensiones."""
    if os.environ.get("HOUND_BENCH") != "1":
        pytest.skip("HOUND_BENCH=1 para ejecutarlo (usa ~3 GB de RAM)")
    from hound.search import _VecBlock

    cfg.permissions.mode = "none"
    index = SearchIndex.__new__(SearchIndex)
    rng = np.random.default_rng(0)
    n, d = 1_000_000, 768
    index.blocks = []
    for s in range(0, n, 32768):
        m = min(32768, n - s)
        v = rng.standard_normal((m, d), dtype=np.float32)
        v /= np.linalg.norm(v, axis=1, keepdims=True)
        index.blocks.append(_VecBlock(v, np.arange(s, s + m), rng.integers(0, 100_000, m)))
    fmask = rng.random(100_000) < 0.7
    q = rng.standard_normal(d).astype(np.float32)
    q /= np.linalg.norm(q)
    t0 = time.perf_counter()
    res = index._vector_candidates(q, fmask, 300)
    elapsed = time.perf_counter() - t0
    print(f"1M x 768: {elapsed * 1000:.0f} ms")
    assert len(res) == 300 and elapsed < 1.0
