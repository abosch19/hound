"""Permisos NTFS reales con la API Authz de Windows.

Crea cuentas locales y carpetas con ACL de verdad, así que necesita Windows y privilegios
de administrador. Se ejecuta en la CI de GitHub (windows-latest) o a mano con:

    set HOUND_WIN_ACL_TESTS=1 && python -m pytest tests/test_windows_acl.py
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get("HOUND_WIN_ACL_TESTS") != "1",
    reason="Solo en Windows con HOUND_WIN_ACL_TESTS=1 (crea usuarios locales)",
)

USERS = ("houndtest_ana", "houndtest_beto")


def _run(*cmd):
    subprocess.run(cmd, check=True, capture_output=True)


@pytest.fixture(scope="module")
def accounts():
    for u in USERS:
        subprocess.run(["net", "user", u, "/delete"], capture_output=True)
        # 14 caracteres como máximo: con más, "net user" pide confirmación y falla sin consola.
        _run("net", "user", u, "Hx9!" + secrets.token_hex(5), "/add")
    yield USERS
    for u in USERS:
        subprocess.run(["net", "user", u, "/delete"], capture_output=True)


@pytest.fixture
def acl_share(tmp_path, accounts):
    root = tmp_path / "share"
    for d in ("Publico", "Privado", "Denegado"):
        (root / d).mkdir(parents=True)
    (root / "Publico" / "circular.txt").write_text("Circular sobre el horario de verano de la oficina")
    (root / "Privado" / "sueldos.txt").write_text("Tabla de sueldos confidencial de la empresa")
    (root / "Denegado" / "auditoria.txt").write_text("Informe de auditoria interna reservado")
    # La raíz: lectura para Usuarios (como un recurso compartido típico).
    _run("icacls", str(root), "/grant", "*S-1-5-32-545:(OI)(CI)RX")
    # Privado: sin herencia; solo ana, SYSTEM y Administradores.
    _run("icacls", str(root / "Privado"), "/inheritance:r", "/grant", "houndtest_ana:(OI)(CI)RX",
         "/grant", "*S-1-5-18:(OI)(CI)F", "/grant", "*S-1-5-32-544:(OI)(CI)F")
    # Denegado: hereda Usuarios pero deniega a ana explícitamente.
    _run("icacls", str(root / "Denegado"), "/deny", "houndtest_ana:(OI)(CI)R")
    return root


def test_authz_con_acl_reales(tmp_path, acl_share):
    from hound.config import Config
    from hound.embed import HashEmbedder
    from hound.indexer import Indexer
    from hound.search import AccessDenied, SearchIndex, User
    from hound.security import WindowsSecurity

    sec = WindowsSecurity()
    cfg = Config()
    cfg.share.path = str(acl_share)
    cfg.data_dir = str(tmp_path / "data")
    cfg.model.repo, cfg.model.dim = "hash", 256
    cfg.index.extract_workers = 1
    cfg.ocr.enabled = False
    emb = HashEmbedder(256)
    ix = Indexer(cfg, emb, sec)
    ix.scan()
    while ix.process_pending():
        pass
    ix.close()
    index = SearchIndex(cfg, emb, sec)

    ana = User(*sec.lookup_account("houndtest_ana"))
    beto = User(*sec.lookup_account("houndtest_beto"))
    assert ana.name.lower().endswith("\\houndtest_ana") and ana.sid.startswith("S-1-5-21-")

    def names(user, q):
        return {r["name"] for r in index.search(user, q, limit=50)}

    assert "circular.txt" in names(ana, "horario de verano")
    assert "circular.txt" in names(beto, "horario de verano")
    assert "sueldos.txt" in names(ana, "sueldos confidencial")
    assert "sueldos.txt" not in names(beto, "sueldos confidencial")
    assert "auditoria.txt" not in names(ana, "auditoria interna")
    assert "auditoria.txt" in names(beto, "auditoria interna")
    with pytest.raises(AccessDenied):
        index.read_document(beto, "Privado/sueldos.txt")
    with pytest.raises(AccessDenied):
        index.read_document(ana, "Denegado/auditoria.txt")

    # Quitar el permiso en el disco se refleja tras el reescaneo.
    _run("icacls", str(acl_share / "Privado"), "/remove:g", "houndtest_ana")
    ix = Indexer(cfg, emb, sec)
    ix.scan()
    ix.close()
    index.refresh()
    index._perm_cache.clear()
    assert "sueldos.txt" not in names(ana, "sueldos confidencial")


def test_cuenta_inexistente():
    from hound.security import SecurityError, WindowsSecurity

    with pytest.raises(SecurityError):
        WindowsSecurity().lookup_account("no_existe_" + secrets.token_hex(4))
