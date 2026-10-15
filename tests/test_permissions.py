"""Filtrado por permisos: un usuario nunca ve ni el nombre ni fragmentos de lo que no puede abrir."""

from __future__ import annotations

import json

import pytest
from conftest import FakeSecurity

from hound.embed import HashEmbedder
from hound.indexer import Indexer
from hound.search import AccessDenied, SearchIndex, User

ALICE = User("EMPRESA\\alice", "S-ALICE")      # administración: ve RRHH
BOB = User("EMPRESA\\bob", "S-BOB")            # obra: no ve RRHH ni Contabilidad
CARLA = User("EMPRESA\\carla", "S-CARLA")      # en el grupo de RRHH pero con "Denegar" explícito
NOBODY = User("EMPRESA\\nadie", "S-NADIE")     # sin cuenta conocida: solo Everyone


@pytest.fixture
def env(cfg, corpus):
    cfg.ocr.enabled = False
    sec = FakeSecurity(str(corpus))
    sec.add_user("alice", "S-ALICE", {"G-ADMIN", "G-RRHH"})
    sec.add_user("bob", "S-BOB", {"G-OBRA"})
    sec.add_user("carla", "S-CARLA", {"G-RRHH"})
    sec.set_rule("", {"G-ADMIN", "G-OBRA", "G-RRHH"})
    sec.set_rule("RRHH", {"G-RRHH"}, deny={"S-CARLA"})
    sec.set_rule("Contabilidad", {"G-ADMIN"})
    emb = HashEmbedder(256)
    ix = Indexer(cfg, emb, sec)
    ix.scan()
    while ix.process_pending():
        pass
    index = SearchIndex(cfg, emb, sec)
    yield cfg, sec, ix, index
    ix.close()


def _dump(results) -> str:
    return json.dumps(results, ensure_ascii=False).lower()


def test_autorizado_ve_nominas(env):
    _cfg, _sec, _ix, index = env
    res = index.search(ALICE, "nómina de marzo salario")
    assert any("nomina_marzo_juan" in r["name"] for r in res)


def test_sin_permiso_no_ve_nombre_ni_fragmento(env):
    _cfg, _sec, _ix, index = env
    for user in (BOB, CARLA, NOBODY):
        for q in ("nómina de marzo salario", "nomina_marzo_juan", "Juan Garcia 2.100", "RRHH Nominas"):
            out = _dump(index.search(user, q, limit=50))
            assert "nomina" not in out and "rrhh" not in out and "2.100" not in out, (user, q)


def test_denegar_explicito_gana_al_grupo(env):
    _cfg, _sec, _ix, index = env
    assert not index.search(CARLA, "salario Juan Garcia", folder="RRHH")
    with pytest.raises(AccessDenied):
        index.read_document(CARLA, "RRHH/Nominas/nomina_marzo_juan.pdf")


def test_filtro_por_carpeta_no_salta_permisos(env):
    _cfg, _sec, _ix, index = env
    assert index.search(BOB, "factura", folder="Contabilidad") == []
    assert index.search(BOB, "factura", folder=r"\\SERVIDOR\Compartido\Contabilidad") == []


def test_read_document_denegado_igual_que_inexistente(env):
    _cfg, _sec, _ix, index = env
    with pytest.raises(AccessDenied) as e1:
        index.read_document(BOB, r"\\SERVIDOR\Compartido\Contabilidad\Facturas\factura_2024_03_perez.pdf")
    with pytest.raises(AccessDenied) as e2:
        index.read_document(BOB, r"\\SERVIDOR\Compartido\Contabilidad\no_existe.pdf")
    assert type(e1.value) is type(e2.value)


def test_list_folder_y_recientes_ocultan(env):
    _cfg, _sec, _ix, index = env
    root = index.list_folder(BOB, "")
    names = {f["name"] for f in root["folders"]}
    assert "RRHH" not in names and "Contabilidad" not in names
    assert "Obras" in names
    assert index.list_folder(BOB, "RRHH") == {**index.list_folder(BOB, "RRHH"), "folders": [], "files": []}
    assert all("RRHH" not in f["path"] and "Contabilidad" not in f["path"] for f in index.recent_files(BOB, 200))
    assert any("RRHH" in f["path"] for f in index.recent_files(ALICE, 200))


def test_rutas_fuera_de_la_carpeta(env):
    _cfg, _sec, _ix, index = env
    for bad in ("../secreto.txt", "Obras/../../etc/passwd", r"C:\Windows\win.ini", r"\\OTRO\recurso\a.pdf"):
        with pytest.raises((ValueError, AccessDenied)):
            index.read_document(ALICE, bad)


def test_cambio_de_permisos_se_aplica_al_reescanear(env):
    cfg, sec, ix, index = env
    assert index.search(BOB, "contrato limpieza", folder="Proveedores")
    sec.set_rule("Proveedores", {"G-ADMIN"})
    ix.scan()
    index.refresh()
    assert index.search(BOB, "contrato limpieza", folder="Proveedores") == []
    assert index.search(ALICE, "contrato limpieza", folder="Proveedores")


def test_permiso_concedido_se_aplica(env):
    cfg, sec, ix, index = env
    assert not index.search(BOB, "factura Perez", folder="Contabilidad")
    sec.set_rule("Contabilidad", {"G-ADMIN", "G-OBRA"})
    ix.scan()
    index.refresh()
    assert index.search(BOB, "factura Perez", folder="Contabilidad")


def test_fichero_sin_descriptor_se_deniega(env):
    cfg, sec, ix, index = env
    sec.set_rule("Obras", None)
    ix.scan()
    index.refresh()
    assert index.search(ALICE, "presupuesto reforma", folder="Obras") == []


def test_fallo_de_authz_deniega(env):
    _cfg, sec, _ix, index = env
    sec.broken_sids.add("S-BOB")
    assert index.search(BOB, "contrato limpieza") == []
    assert index.list_folder(BOB, "")["files"] == []


def test_permisos_del_recurso_compartido(env):
    cfg, sec, _ix, index = env
    cfg.share.name = "Compartido"
    sec.share = b"allow:G-ADMIN;deny:"
    index._mask_cache.clear()
    assert index.search(BOB, "presupuesto reforma") == []
    assert index.search(ALICE, "presupuesto reforma")


def test_carpeta_excluida_no_se_indexa(cfg, corpus):
    cfg.index.exclude = ["RRHH"]
    sec = FakeSecurity(str(corpus))
    sec.add_user("alice", "S-ALICE", {"G-ADMIN"})
    sec.set_rule("", {"S-EVERYONE"})
    emb = HashEmbedder(256)
    ix = Indexer(cfg, emb, sec)
    ix.scan()
    while ix.process_pending():
        pass
    ix.close()
    index = SearchIndex(cfg, emb, sec)
    assert ix.conn.execute("SELECT count(*) FROM files WHERE path LIKE 'RRHH/%'").fetchone()[0] == 0
    assert "nomina" not in _dump(index.search(ALICE, "nómina salario", limit=50))


def test_exclusion_posterior_borra_lo_indexado(env):
    cfg, sec, ix, index = env
    assert index.search(ALICE, "nómina salario", folder="RRHH")
    cfg.index.exclude = ["rrhh"]  # sin distinguir mayúsculas, como Windows
    ix2 = Indexer(cfg, ix.embedder, sec, conn=ix.conn)
    ix2.scan()
    index.refresh()
    assert index.search(ALICE, "nómina salario", folder="RRHH") == []


def test_tokens_revocados(tmp_path, cfg, corpus):
    from hound import db
    from hound.auth import TokenStore, create_token, revoke

    sec = FakeSecurity(str(corpus))
    sec.add_user("bob", "S-BOB")
    conn = db.init_db(cfg.db_path)
    name, token = create_token(conn, sec, "bob")
    store = TokenStore(cfg.db_path)
    assert store.resolve(token) == User(name, "S-BOB")
    assert store.resolve(token + "x") is None
    assert store.resolve("") is None
    assert store.resolve(None) is None
    revoke(conn, "bob")
    store._cache.clear()
    assert store.resolve(token) is None
    with pytest.raises(RuntimeError):
        create_token(conn, sec, "desconocido")
