"""Movidos y renombrados, PDF largos por tramos, fragmentos numéricos, códigos, horario y costes."""

from __future__ import annotations

import datetime as dt
import json
import os
import time

import pytest
from conftest import make_config, make_pdf

from hound import db
from hound.chunking import has_words
from hound.costs import CostModel, backlog_seconds
from hound.embed import HashEmbedder
from hound.extract import ExtractOptions
from hound.extract.image import extract_image
from hound.extract.pdf import extract_pdf, garbled
from hound.extract.text import extract_html
from hound.indexer import PDF_FIRST_PAGES, Indexer, LoadMeter, parse_schedule
from hound.search import SearchIndex, User, lookup_weights
from hound.security import NoSecurity
from hound.status import index_status

ANY = User("EMPRESA\\alice", "")


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "share"
    (root / "Obras" / "Cocina").mkdir(parents=True)
    (root / "Obras" / "Cocina" / "presupuesto.txt").write_text("Presupuesto de la reforma de la cocina con azulejos")
    (root / "Obras" / "plano.txt").write_text("Plano de la planta baja")
    cfg = make_config(tmp_path, root, mode="none")
    cfg.ocr.enabled = False
    emb = HashEmbedder(256)
    ix = Indexer(cfg, emb, NoSecurity())
    yield cfg, root, ix, emb
    ix.close()


def _run(ix):
    st = ix.scan()
    while ix.process_pending():
        pass
    return st


def _segs(ix):
    return {r[0] for r in ix.conn.execute("SELECT id FROM segments")}


def test_carpeta_renombrada_no_se_reindexa(env):
    cfg, root, ix, emb = env
    _run(ix)
    before = _segs(ix)
    os.rename(root / "Obras", root / "Proyectos")
    st = ix.scan()
    assert st.moved == 2 and st.new == 0 and st.deleted == 0
    assert not ix.conn.execute("SELECT 1 FROM files WHERE status = 'pending'").fetchone()
    assert _segs(ix) == before  # mismos fragmentos: no se ha vuelto a extraer ni a calcular vectores
    res = SearchIndex(cfg, emb, NoSecurity()).search(ANY, "Proyectos presupuesto cocina")
    assert res[0]["path"].endswith("Proyectos\\Cocina\\presupuesto.txt")


def test_fichero_cambiado_y_movido_si_se_reindexa(env):
    _cfg, root, ix, _emb = env
    _run(ix)
    (root / "Obras" / "plano.txt").write_text("Plano nuevo, distinto tamaño y contenido")
    os.rename(root / "Obras" / "plano.txt", root / "plano2.txt")
    st = ix.scan()
    assert st.moved == 0 and st.new == 1 and st.deleted == 1


def test_movimiento_visto_por_el_vigilante(env):
    _cfg, root, ix, _emb = env
    _run(ix)
    before = _segs(ix)
    os.rename(root / "Obras" / "Cocina", root / "Cocina")
    ix._moves.append(("Obras/Cocina", "Cocina"))
    ix._changed |= {("Obras", True), ("", False)}
    ix._first_event = ix._last_event = time.monotonic() - 5
    assert ix.changes_ready()
    assert ix._drain_changes()
    assert ix.conn.execute("SELECT path FROM files WHERE name = 'presupuesto.txt'").fetchone()[0] == \
        "Cocina/presupuesto.txt"
    assert _segs(ix) == before


def test_espera_a_que_paren_los_cambios(env):
    _cfg, _root, ix, _emb = env
    now = time.monotonic()
    ix._changed.add(("Obras", False))
    ix._first_event, ix._last_event = now - 1, now - 0.5
    assert not ix.changes_ready(now)                      # todavía llegan cambios
    assert ix.changes_ready(now + ix.QUIET_S)             # ya ha habido calma
    ix._last_event = now
    assert ix.changes_ready(now - 1 + ix.MAX_WAIT_S + 1)  # cambios sin parar: no se espera más del máximo
    ix._changed = {(f"d{i}", False) for i in range(ix.MAX_CHANGES + 1)}
    assert not ix._drain_changes()                        # demasiados: escaneo completo


def test_pdf_largo_por_tramos(env):
    cfg, root, ix, emb = env
    make_pdf(root / "memoria.pdf", [f"Pagina {i + 1} de la memoria. Capitulo sobre cimentacion {i + 1}"
                                    for i in range(120)])
    ix.scan()
    ix.process_pending()  # primera pasada: lo pendiente, con solo las primeras páginas del PDF
    row = ix.conn.execute("SELECT status, pages_done, pages_total FROM files WHERE name = 'memoria.pdf'").fetchone()
    assert tuple(row) == ("partial", PDF_FIRST_PAGES, 120)
    index = SearchIndex(cfg, emb, NoSecurity())
    assert index.search(ANY, "memoria cimentacion")  # ya se puede buscar
    assert index_status(ix.conn)["files"]["partial_pdfs"] == 1
    while ix.process_pending():
        pass
    row = ix.conn.execute("SELECT id, status, pages_done FROM files WHERE name = 'memoria.pdf'").fetchone()
    assert (row["status"], row["pages_done"]) == ("indexed", 120)
    locs = [r[0] for r in ix.conn.execute("SELECT location FROM segments WHERE file_id = ? ORDER BY ord",
                                          (row["id"],))]
    assert "página 1" in locs and "página 120" in locs and len(locs) == len(set(locs))
    index.refresh()
    assert any(r["location"] == "página 117" for r in index.search(ANY, "cimentacion 117", limit=50))


def test_pdf_corto_de_una_vez(tmp_path):
    make_pdf(tmp_path / "a.pdf", ["uno", "dos", "tres"])
    ex = extract_pdf(str(tmp_path / "a.pdf"), ExtractOptions(page_limit=PDF_FIRST_PAGES))
    assert (ex.pages_done, ex.pages_total, len(ex.blocks)) == (3, 3, 3)


def test_texto_estropeado():
    assert garbled("(cid:12)(cid:45)(cid:3)(cid:88) " * 5)
    assert garbled("" * 20)
    assert not garbled("Presupuesto de la reforma de la cocina con azulejos y fontanería " * 2)
    assert not garbled("12.400,00 € 3.100,50 € 2024-03-01 15.000 " * 3)


def test_fragmentos_numericos_sin_vector(env):
    cfg, root, ix, emb = env
    (root / "importes.csv").write_text("\n".join(f"{i};{i * 1234.5:.2f};{i * 7}" for i in range(1, 60)))
    _run(ix)
    fid = ix.conn.execute("SELECT id FROM files WHERE name = 'importes.csv'").fetchone()[0]
    vecs = [len(r[0]) for r in ix.conn.execute("SELECT vec FROM segments WHERE file_id = ? ORDER BY ord", (fid,))]
    assert vecs[0] > 0 and all(v == 0 for v in vecs[1:])  # solo el nombre lleva vector
    res = SearchIndex(cfg, emb, NoSecurity()).search(ANY, "12345.00")
    assert res and res[0]["name"] == "importes.csv"  # se encuentra por la palabra exacta
    assert has_words("Factura de marzo 2024") and not has_words("12;1234.50;7 | 13;1604.85;91")


def test_peso_de_codigos():
    assert lookup_weights("B12345678") == (0.5, 2.0)
    assert lookup_weights("factura 2024/0153") == (0.5, 2.0)
    assert lookup_weights("contrato con el proveedor de limpieza") == (1.0, 1.0)


def test_horario_que_cruza_la_medianoche(cfg):
    s = cfg.index.schedule
    s.busy_hours, s.busy_days = "22:00-06:00", "mon-fri"
    assert parse_schedule(cfg, dt.datetime(2026, 10, 14, 23, 0))      # miércoles noche
    assert parse_schedule(cfg, dt.datetime(2026, 10, 15, 3, 0))       # madrugada del jueves
    assert not parse_schedule(cfg, dt.datetime(2026, 10, 14, 12, 0))
    assert parse_schedule(cfg, dt.datetime(2026, 10, 17, 3, 0))       # sábado de madrugada: es la noche del viernes
    assert not parse_schedule(cfg, dt.datetime(2026, 10, 18, 3, 0))   # domingo de madrugada


def test_carga_del_servidor():
    m = LoadMeter()
    time.sleep(0.2)
    assert 0.0 <= m.others_percent() <= 100.0


def test_html(tmp_path):
    p = tmp_path / "p.html"
    p.write_text("<html><head><title>Normas de obra</title><style>p{}</style></head>"
                 "<body><h1>Seguridad</h1><p>Casco obligatorio &amp; botas</p><script>x()</script></body></html>")
    ex = extract_html(str(p))
    text = "\n".join(b.text for b in ex.blocks)
    assert ex.title == "Normas de obra" and "Casco obligatorio & botas" in text and "x()" not in text


def test_exif(tmp_path):
    from PIL import Image

    img = Image.new("RGB", (50, 50))
    exif = img.getexif()
    exif[271], exif[272] = "Canon", "EOS 80D"
    exif.get_ifd(0x8769)[36867] = "2024:03:05 10:22:00"
    img.save(tmp_path / "foto.jpg", exif=exif)
    ex = extract_image(str(tmp_path / "foto.jpg"), ExtractOptions())
    text = ex.blocks[0].text
    assert "5 de marzo de 2024" in text and "2024-03-05 10:22" in text and "Canon EOS 80D" in text


def test_modelo_de_costes(tmp_path):
    m = CostModel()
    assert m.estimate("pdf", 10, 5.0) > m.estimate("text", 10, 5.0)  # valores por defecto
    for mb in (0.1, 0.5, 1, 2, 4, 8, 3, 0.2, 6, 1.5):
        m.add("pdf", mb, 2.0 + 10.0 * mb)
    fixed, per_mb = m.params("pdf")
    assert abs(fixed - 2) < 0.1 and abs(per_mb - 10) < 0.1
    conn = db.init_db(tmp_path / "i.sqlite")
    m.save(conn)
    conn.execute("INSERT INTO files(path, name, ext, kind, size, mtime, status) "
                 "VALUES ('a.pdf', 'a.pdf', '.pdf', 'pdf', 3000000, 0, 'pending')")
    assert abs(backlog_seconds(conn) - 32.0) < 0.5
    assert CostModel.load(conn).learned() == {"pdf": 10}


def test_estimate_cli(env, capsys):
    from hound.cli import cmd_estimate

    cfg, _root, ix, _emb = env
    _run(ix)

    class A:
        json = True

    assert cmd_estimate(A(), cfg) == 0
    out = json.loads(capsys.readouterr().out)
    assert {r["folder"] for r in out["folders"]} == {"Obras"} and out["total"]["files"] == 2


def test_pdf_movido_mientras_se_lee_sigue_despues(env):
    _cfg, root, ix, _emb = env
    make_pdf(root / "memoria.pdf", [f"Pagina {i + 1} cimentacion" for i in range(120)])
    ix.scan()
    ix.process_pending()
    os.rename(root / "memoria.pdf", root / "Obras" / "memoria.pdf")  # antes de que el escaneo lo vea
    ix.process_pending()
    row = ix.conn.execute("SELECT status, pages_done FROM files WHERE name = 'memoria.pdf'").fetchone()
    assert tuple(row) == ("partial", PDF_FIRST_PAGES)  # no se da por terminado
    assert ix.scan().moved == 1
    while ix.process_pending():
        pass
    row = ix.conn.execute("SELECT status, pages_done, error FROM files WHERE name = 'memoria.pdf'").fetchone()
    assert tuple(row) == ("indexed", 120, None)


# --- EmbeddingGemma 2: imágenes y cambio de modelo -------------------------------------------

def _photo(path, color):
    from PIL import Image

    Image.new("RGB", (200, 150), color).save(path)


def test_fotos_por_lo_que_se_ve(env):
    cfg, root, ix, emb = env
    _photo(root / "Obras" / "IMG_0001.jpg", (220, 30, 30))   # para HashEmbedder: "rojo foto imagen"
    _photo(root / "Obras" / "IMG_0002.jpg", (30, 200, 40))   # "verde foto imagen"
    _run(ix)
    rows = ix.conn.execute("SELECT name, visual FROM files WHERE kind = 'image' ORDER BY name").fetchall()
    assert [tuple(r) for r in rows] == [("IMG_0001.jpg", 1), ("IMG_0002.jpg", 1)]
    cfg.search.image_min_similarity = 0.3
    res = SearchIndex(cfg, emb, NoSecurity()).search(ANY, "rojo")
    by_name = {r["name"]: r for r in res}
    assert by_name["IMG_0001.jpg"]["location"] == "imagen (lo que se ve)"
    assert by_name.get("IMG_0002.jpg", {}).get("location") != "imagen (lo que se ve)"
    st = index_status(ix.conn)
    assert st["images"] == {"with_visual_search": 2, "waiting": 0, "without": 0}


def test_imagenes_ya_indexadas_reciben_vector_visual(env):
    cfg, root, _ix, emb = env
    _photo(root / "foto.jpg", (30, 30, 220))

    class NoVision(HashEmbedder):
        vision = None

    old = Indexer(cfg, NoVision(256), NoSecurity())
    _run(old)
    assert old.conn.execute("SELECT visual FROM files WHERE name = 'foto.jpg'").fetchone()[0] == 0
    old.close()
    ix = Indexer(cfg, emb, NoSecurity())
    before = ix.conn.execute("SELECT count(*) FROM segments").fetchone()[0]
    while ix.process_pending():
        pass
    assert ix.conn.execute("SELECT visual, status FROM files WHERE name = 'foto.jpg'").fetchone()[:] == (1, "indexed")
    assert ix.conn.execute("SELECT count(*) FROM segments").fetchone()[0] == before + 1  # solo el visual
    ix.close()


def test_cambio_de_modelo_sin_reextraer(env, monkeypatch):
    cfg, root, ix, emb = env
    _run(ix)
    ix.close()

    class Nuevo(HashEmbedder):
        name = "hash-v2"

    import hound.indexer as indexer_mod

    calls = []
    monkeypatch.setattr(indexer_mod, "_extract_worker", lambda *a: calls.append(a))
    new = Indexer(cfg, Nuevo(256), NoSecurity())
    assert new.gen == 1
    index = SearchIndex(cfg, Nuevo(256), NoSecurity())
    assert index.search(ANY, "presupuesto cocina")  # por palabras sigue encontrando
    assert not index._vector_candidates(Nuevo(256).embed_query("cocina"), index.filter_mask(ANY), 10)
    assert index_status(new.conn)["model_upgrade_files_left"] == 2
    while new.process_pending():
        pass
    assert not calls  # no se ha vuelto a extraer nada
    assert not new.conn.execute("SELECT 1 FROM files WHERE emb < 1").fetchone()
    index.refresh()
    assert index._vector_candidates(Nuevo(256).embed_query("cocina"), index.filter_mask(ANY), 10)
    assert "model_upgrade_files_left" not in index_status(new.conn)
    new.close()
