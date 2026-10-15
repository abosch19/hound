"""Servidor MCP de extremo a extremo: HTTP real, cliente MCP real, tokens y permisos."""

from __future__ import annotations

import json
import socket
import threading
import time

import anyio
import httpx
import pytest
from conftest import FakeSecurity

from hound import db
from hound.auth import TokenStore, create_token, revoke
from hound.embed import HashEmbedder
from hound.indexer import Indexer
from hound.search import SearchIndex
from hound.server import build_app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(cfg, corpus):
    import uvicorn

    cfg.ocr.enabled = False
    sec = FakeSecurity(str(corpus))
    sec.add_user("alice", "S-ALICE", {"G-RRHH"})
    sec.add_user("bob", "S-BOB")
    sec.set_rule("RRHH", {"G-RRHH"})
    emb = HashEmbedder(256)
    ix = Indexer(cfg, emb, sec)
    ix.scan()
    while ix.process_pending():
        pass
    ix.close()
    conn = db.init_db(cfg.db_path)
    _, alice = create_token(conn, sec, "alice")
    _, bob = create_token(conn, sec, "bob")
    store = TokenStore(cfg.db_path)
    app = build_app(SearchIndex(cfg, emb, sec), store)
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", {"alice": alice, "bob": bob}, conn, store
    srv.should_exit = True
    t.join(5)


def _call(url: str, token: str, tool: str, args: dict):
    import httpx2
    from mcp.client.client import Client
    from mcp.client.streamable_http import streamable_http_client

    async def go():
        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30) as http:
            async with Client(streamable_http_client(url + "/mcp", http_client=http)) as client:
                tools = await client.list_tools()
                res = await client.call_tool(tool, args)
                return [t.name for t in tools.tools], res

    return anyio.run(go)


def _payload(res) -> dict:
    if res.structured_content:
        return res.structured_content
    return json.loads(res.content[0].text)


def test_sin_token_401(server):
    url, _tokens, _conn, _store = server
    r = httpx.post(url + "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401
    r = httpx.post(url + "/mcp", json={}, headers={"Authorization": "Bearer hound_falso"})
    assert r.status_code == 401
    assert httpx.get(url + "/health").json() == {"status": "ok"}


def test_herramientas_y_busqueda(server):
    url, tokens, _conn, store = server
    names, res = _call(url, tokens["alice"], "search_files", {"query": "contrato proveedor de limpieza"})
    assert set(names) >= {"search_files", "read_document", "list_folder", "recent_files"}
    first = _payload(res)["results"][0]
    assert first["name"] == "contrato_limpieza_2024.pdf"
    assert first["path"].startswith("\\\\SERVIDOR\\Compartido\\")
    assert first["file_url"].startswith("file://SERVIDOR/Compartido/") and first["file_url"].endswith(
        "/contrato_limpieza_2024.pdf")
    assert first["file_url"].startswith(first["folder_url"]) and first["folder_url"].endswith("/")
    _names, res = _call(url, tokens["alice"], "read_document", {"path": first["path"], "start": 1})
    assert _payload(res)["segments"][0]["location"] == "página 2"
    store.flush()
    logged = store._conn().execute("SELECT user_name, tool, args FROM query_log ORDER BY id").fetchall()
    assert logged[0]["user_name"] == "EMPRESA\\alice" and "limpieza" in logged[0]["args"]


def test_permisos_por_token(server):
    url, tokens, _conn, _store = server
    _n, res = _call(url, tokens["alice"], "search_files", {"query": "nómina salario Juan", "limit": 50})
    assert "nomina" in json.dumps(_payload(res)).lower()
    _n, res = _call(url, tokens["bob"], "search_files", {"query": "nómina salario Juan", "limit": 50})
    assert "nomina" not in json.dumps(_payload(res)).lower()
    _n, res = _call(url, tokens["bob"], "read_document", {"path": "RRHH/Nominas/nomina_marzo_juan.pdf"})
    assert res.is_error and "no tienes permiso" in res.content[0].text
    _n, res = _call(url, tokens["bob"], "list_folder", {"path": ""})
    assert "RRHH" not in json.dumps(_payload(res))


def test_token_revocado(server):
    url, tokens, conn, store = server
    revoke(conn, "bob")
    store._cache.clear()
    r = httpx.post(url + "/mcp", json={}, headers={"Authorization": f"Bearer {tokens['bob']}"})
    assert r.status_code == 401


def test_index_status(server):
    url, tokens, _conn, _store = server
    names, res = _call(url, tokens["bob"], "index_status", {})
    assert "index_status" in names
    st = _payload(res)
    assert st["files"]["pending"] == 0 and st["files"]["total"] == st["files"]["indexed"] + st["files"]["errors"]
    assert st["progress_percent"] == 100.0
    assert st["search_index"] == "listo"
    # bob no ve RRHH: ve menos ficheros de los que hay, y no se devuelve ninguna ruta
    assert 0 < st["files_visible_to_you"] < st["files"]["total"]
    assert "rrhh" not in json.dumps(st).lower()


def test_mcp_responde_mientras_carga_el_indice(cfg, corpus):
    """El servidor arranca antes que el índice: index_status funciona y la búsqueda avisa."""
    import uvicorn

    sec = FakeSecurity(str(corpus))
    sec.add_user("bob", "S-BOB")
    conn = db.init_db(cfg.db_path)
    _, bob = create_token(conn, sec, "bob")
    app = build_app(lambda: None, TokenStore(cfg.db_path), lambda: {"indexer_process": {"running": True}})
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    while not srv.started:
        time.sleep(0.05)
    try:
        url = f"http://127.0.0.1:{port}"
        _n, res = _call(url, bob, "index_status", {})
        st = _payload(res)
        assert st["search_index"] == "cargando" and st["phase"] == "stopped"
        assert st["indexer_process"] == {"running": True}
        _n, res = _call(url, bob, "search_files", {"query": "algo"})
        assert res.is_error and "cargando" in res.content[0].text
    finally:
        srv.should_exit = True
        t.join(5)
