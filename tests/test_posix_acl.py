"""Permisos de Linux (modo "posix"): bits de modo, ACL POSIX y permiso de paso en carpetas."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

from hound.security import (ACL_GROUP, ACL_GROUP_OBJ, ACL_MASK, ACL_OTHER, ACL_USER, ACL_USER_OBJ, R, X,
                            PosixSecurity, parse_posix_acl, posix_allows)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Solo en Linux/macOS")

OWNER, ALICE, BOB = 1000, 1001, 1002
STAFF, RRHH = 100, 200


def entry(mode, uid=OWNER, gid=STAFF, acl=None):
    e = {"u": uid, "g": gid, "m": mode}
    if acl:
        e["acl"] = acl
    return e


def test_bits_de_modo():
    f = entry(0o640)
    assert posix_allows(OWNER, set(), f, R)
    assert posix_allows(ALICE, {STAFF}, f, R)
    assert not posix_allows(BOB, {RRHH}, f, R)
    assert posix_allows(BOB, set(), entry(0o644), R)
    assert posix_allows(0, set(), entry(0o000), R)  # root lo lee todo


def test_propietario_sin_permiso_no_cae_en_otros():
    assert not posix_allows(OWNER, {STAFF}, entry(0o044), R)


def test_acl_usuario_grupo_y_mascara():
    acl = [[ACL_USER_OBJ, 6, -1], [ACL_USER, 4, ALICE], [ACL_GROUP_OBJ, 0, -1], [ACL_GROUP, 4, RRHH],
           [ACL_MASK, 4, -1], [ACL_OTHER, 0, -1]]
    f = entry(0o640, acl=acl)
    assert posix_allows(ALICE, set(), f, R)
    assert posix_allows(BOB, {RRHH}, f, R)
    assert not posix_allows(BOB, {STAFF}, f, R)          # grupo propietario sin permiso: no va a "otros"
    assert not posix_allows(BOB, {999}, f, R)
    masked = [*acl[:4], [ACL_MASK, 0, -1], acl[5]]
    assert not posix_allows(ALICE, set(), entry(0o600, acl=masked), R)  # la máscara recorta al usuario


def test_parse_xattr():
    raw = (2).to_bytes(4, "little")
    for tag, perm, ident in [(ACL_USER_OBJ, 6, 0xFFFFFFFF), (ACL_USER, 4, ALICE), (ACL_GROUP_OBJ, 4, 0xFFFFFFFF),
                             (ACL_MASK, 4, 0xFFFFFFFF), (ACL_OTHER, 0, 0xFFFFFFFF)]:
        raw += tag.to_bytes(2, "little") + perm.to_bytes(2, "little") + ident.to_bytes(4, "little")
    assert parse_posix_acl(raw) == [[1, 6, -1], [2, 4, ALICE], [4, 4, -1], [16, 4, -1], [32, 0, -1]]
    assert parse_posix_acl(b"basura") is None


def test_descriptor_con_carpetas(tmp_path, monkeypatch):
    root = tmp_path / "share"
    (root / "RRHH").mkdir(parents=True)
    f = root / "RRHH" / "nomina.txt"
    f.write_text("x")
    os.chmod(f, 0o644)
    os.chmod(root / "RRHH", 0o750)
    sec = PosixSecurity(str(root))
    sd = sec.read_sd(str(f))
    data = json.loads(sd)
    assert [d["m"] for d in data["dirs"]] == [os.stat(root).st_mode & 0o777, 0o750]
    assert data["file"]["m"] == 0o644 and data["file"]["w"] == R
    st = os.stat(f)
    stranger = 4242424
    monkeypatch.setattr(sec, "_user_groups", lambda uid: {st.st_gid} if uid == 1 else set())
    # el fichero es legible por todos, pero la carpeta RRHH no deja pasar a "otros"
    assert not sec.check(f"unix:{stranger}", sd)
    assert sec.check("unix:1", sd)                          # del grupo de la carpeta: r-x
    assert sec.check(f"unix:{st.st_uid}", sd)
    assert not sec.check("S-1-5-21-1", sd)                  # un SID de Windows nunca vale aquí
    root_sd = json.loads(sec.read_sd(str(root)))
    assert root_sd["dirs"] == [] and root_sd["file"]["w"] == R | X


def test_lookup_cuenta_actual():
    import pwd

    me = pwd.getpwuid(os.getuid())
    sec = PosixSecurity()
    assert sec.lookup_account(me.pw_name) == (me.pw_name, f"unix:{me.pw_uid}")
    assert sec.lookup_account("DOMINIO\\" + me.pw_name)[1] == f"unix:{me.pw_uid}"
    with pytest.raises(Exception):
        sec.lookup_account("no_existe_houndtest")
    assert me.pw_gid in sec._user_groups(me.pw_uid)


@pytest.mark.skipif(not (sys.platform.startswith("linux") and shutil.which("setfacl")),
                    reason="Necesita Linux con setfacl")
def test_acl_real_con_setfacl(tmp_path):
    f = tmp_path / "doc.txt"
    f.write_text("x")
    os.chmod(f, 0o600)
    r = subprocess.run(["setfacl", "-m", "u:4242:r", str(f)], capture_output=True)
    if r.returncode:
        pytest.skip(f"El sistema de ficheros no admite ACL: {r.stderr.decode().strip()}")
    sec = PosixSecurity(str(tmp_path))
    data = json.loads(sec.read_sd(str(f)))
    assert [ACL_USER, 4, 4242] in data["file"]["acl"]
    assert posix_allows(4242, set(), data["file"], R)
    assert not posix_allows(4343, set(), data["file"], R)
