"""Permisos: lectura de ACL al indexar y comprobación de acceso por usuario al buscar.

Dos implementaciones reales:

- Windows ("ntfs"): ACL de NTFS evaluadas con la API Authz.
- Linux ("posix"): propietario, grupo, bits de modo y ACL POSIX (setfacl), como las aplica
  el kernel cuando Samba abre un fichero en nombre del usuario.

No reimplementamos la lógica de las ACL (herencia, "Denegar", grupos anidados): usamos
la API Authz de Windows, que la evalúa igual que el propio sistema de ficheros, a partir
del SID del usuario y del descriptor de seguridad guardado en el índice.

- `read_sd(path)`: descriptor de seguridad del fichero (propietario, grupo y DACL).
- `check(sid, sd)`: ¿puede ese usuario leer un fichero con ese descriptor?
- `lookup_account(nombre)`: nombre canónico y SID de una cuenta local o del dominio.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
import time
from abc import ABC, abstractmethod

if sys.platform != "win32":
    import pwd

FILE_GENERIC_READ = 0x00120089


class SecurityError(RuntimeError):
    pass


class SecurityProvider(ABC):
    enforced = True

    @abstractmethod
    def read_sd(self, path: str) -> bytes | None: ...

    @abstractmethod
    def check(self, sid: str, sd: bytes) -> bool: ...

    @abstractmethod
    def lookup_account(self, name: str) -> tuple[str, str]: ...

    def share_sd(self, share_name: str) -> bytes | None:
        return None


class NoSecurity(SecurityProvider):
    """permissions.mode = "none": todos los usuarios ven todo el índice."""

    enforced = False

    def read_sd(self, path: str) -> bytes | None:
        return None

    def check(self, sid: str, sd: bytes) -> bool:
        return True

    def lookup_account(self, name: str) -> tuple[str, str]:
        return name, "S-1-0-" + name.lower()


def default_mode() -> str:
    return "ntfs" if sys.platform == "win32" else "posix"


def get_security(mode: str, root: str = "") -> SecurityProvider:
    if mode == "none":
        return NoSecurity()
    if mode == "posix":
        if sys.platform == "win32":
            raise SecurityError("permissions.mode = 'posix' no funciona en Windows. Usa 'ntfs'.")
        return PosixSecurity(root)
    if sys.platform != "win32":
        raise SecurityError("permissions.mode = 'ntfs' solo funciona en Windows. En Linux usa 'posix'.")
    return WindowsSecurity()


# --- POSIX (Linux) ----------------------------------------------------------------------

ACL_USER_OBJ, ACL_USER, ACL_GROUP_OBJ, ACL_GROUP, ACL_MASK, ACL_OTHER = 0x01, 0x02, 0x04, 0x08, 0x10, 0x20
R, W, X = 4, 2, 1


def parse_posix_acl(raw: bytes) -> list[list[int]] | None:
    """Decodifica el xattr system.posix_acl_access: [[etiqueta, permisos, id], ...]."""
    if len(raw) < 4 or int.from_bytes(raw[:4], "little") != 2 or (len(raw) - 4) % 8:
        return None
    out = []
    for i in range(4, len(raw), 8):
        tag = int.from_bytes(raw[i:i + 2], "little")
        perm = int.from_bytes(raw[i + 2:i + 4], "little")
        ident = int.from_bytes(raw[i + 4:i + 8], "little")
        out.append([tag, perm & 7, ident if tag in (ACL_USER, ACL_GROUP) else -1])
    return out


def posix_allows(uid: int, groups: set[int], entry: dict, want: int) -> bool:
    """Algoritmo de comprobación de acceso de POSIX.1e para una entrada {u, g, m, acl}."""
    if uid == 0:
        return True
    mode = entry["m"]
    acl = entry.get("acl")
    if not acl:
        if uid == entry["u"]:
            bits = mode >> 6
        elif entry["g"] in groups:
            bits = mode >> 3
        else:
            bits = mode
        return (bits & want) == want
    mask = next((p for t, p, _ in acl if t == ACL_MASK), 7)
    if uid == entry["u"]:
        return ((mode >> 6) & want) == want
    for t, p, i in acl:
        if t == ACL_USER and i == uid:
            return (p & mask & want) == want
    matched = False
    for t, p, i in acl:
        if (t == ACL_GROUP_OBJ and entry["g"] in groups) or (t == ACL_GROUP and i in groups):
            matched = True
            if (p & mask & want) == want:
                return True
    if matched:
        return False
    other = next((p for t, p, _ in acl if t == ACL_OTHER), mode & 7)
    return (other & want) == want


class PosixSecurity(SecurityProvider):
    """Permisos de Linux. La "cuenta" de un usuario es su uid ("unix:1000"); sus grupos se
    resuelven con NSS, así que sirven igual cuentas locales que del dominio (winbind/sssd).

    El descriptor que se guarda es JSON con la entrada del fichero y la de cada carpeta desde
    la raíz de la carpeta compartida: para abrir un fichero hace falta permiso de paso (x) en
    todas ellas, igual que cuando Samba lo abre en nombre del usuario.
    """

    CONTEXT_TTL = 600
    DIR_TTL = 60

    def __init__(self, root: str = ""):
        self.root = os.path.abspath(root) if root else ""
        self._lock = threading.Lock()
        self._groups: dict[int, tuple[set[int], float]] = {}
        self._dirs: dict[str, tuple[dict, float]] = {}

    @staticmethod
    def _entry(path: str, st: os.stat_result) -> dict:
        e = {"u": st.st_uid, "g": st.st_gid, "m": st.st_mode & 0o777}
        getxattr = getattr(os, "getxattr", None)
        if getxattr is not None:
            try:
                acl = parse_posix_acl(getxattr(path, "system.posix_acl_access"))
            except OSError:
                acl = None  # sin ACL (ENODATA) o sistema de ficheros sin soporte
            if acl:
                e["acl"] = sorted(acl)
        return e

    def _dir_entry(self, path: str) -> dict:
        now = time.monotonic()
        hit = self._dirs.get(path)
        if hit and now - hit[1] < self.DIR_TTL:
            return hit[0]
        e = self._entry(path, os.stat(path))
        if len(self._dirs) > 50000:
            self._dirs.clear()
        self._dirs[path] = (e, now)
        return e

    def read_sd(self, path: str) -> bytes | None:
        path = os.path.abspath(path)
        st = os.stat(path)
        target = self._entry(path, st)
        target["w"] = R | X if stat.S_ISDIR(st.st_mode) else R
        chain = []
        if self.root and path != self.root and path.startswith(self.root.rstrip("/") + "/"):
            d = os.path.dirname(path)
            while True:
                chain.append(self._dir_entry(d))
                if d == self.root:
                    break
                d = os.path.dirname(d)
            chain.reverse()
        return json.dumps({"v": 1, "dirs": chain, "file": target}, sort_keys=True,
                          separators=(",", ":")).encode()

    def _user_groups(self, uid: int) -> set[int]:
        now = time.monotonic()
        hit = self._groups.get(uid)
        if hit and now - hit[1] < self.CONTEXT_TTL:
            return hit[0]
        try:
            pw = pwd.getpwuid(uid)
            groups = set(os.getgrouplist(pw.pw_name, pw.pw_gid))
        except (KeyError, OSError) as exc:
            raise SecurityError(f"No se pueden obtener los grupos de unix:{uid}: {exc}") from exc
        self._groups[uid] = (groups, now)
        return groups

    def check(self, sid: str, sd: bytes) -> bool:
        if not sd or not sid.startswith("unix:"):
            return False
        uid = int(sid[5:])
        data = json.loads(sd)
        with self._lock:
            groups = self._user_groups(uid)
        if not all(posix_allows(uid, groups, d, X) for d in data["dirs"]):
            return False
        f = data["file"]
        return posix_allows(uid, groups, f, f.get("w", R))

    def lookup_account(self, name: str) -> tuple[str, str]:
        candidates = [name]
        if "\\" in name:  # DOMINIO\usuario: según winbind, con o sin el dominio
            candidates.append(name.split("\\", 1)[1])
        candidates += [c.lower() for c in candidates if c != c.lower()]
        for cand in candidates:
            try:
                pw = pwd.getpwnam(cand)
            except KeyError:
                continue
            return pw.pw_name, f"unix:{pw.pw_uid}"
        raise SecurityError(f"No existe la cuenta '{name}'")


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    _authz = ctypes.WinDLL("authz", use_last_error=True)
    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _netapi = ctypes.WinDLL("netapi32", use_last_error=True)

    OWNER_SECURITY_INFORMATION = 0x1
    GROUP_SECURITY_INFORMATION = 0x2
    DACL_SECURITY_INFORMATION = 0x4
    ERROR_INSUFFICIENT_BUFFER = 122
    AUTHZ_RM_FLAG_NO_AUDIT = 0x1
    SDDL_REVISION_1 = 1

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class AUTHZ_ACCESS_REQUEST(ctypes.Structure):
        _fields_ = [
            ("DesiredAccess", wintypes.DWORD),
            ("PrincipalSelfSid", ctypes.c_void_p),
            ("ObjectTypeList", ctypes.c_void_p),
            ("ObjectTypeListLength", wintypes.DWORD),
            ("OptionalArguments", ctypes.c_void_p),
        ]

    class AUTHZ_ACCESS_REPLY(ctypes.Structure):
        _fields_ = [
            ("ResultListLength", wintypes.DWORD),
            ("GrantedAccessMask", ctypes.POINTER(wintypes.DWORD)),
            ("SaclEvaluationResults", ctypes.POINTER(wintypes.DWORD)),
            ("Error", ctypes.POINTER(wintypes.DWORD)),
        ]

    class SHARE_INFO_502(ctypes.Structure):
        _fields_ = [
            ("netname", wintypes.LPWSTR), ("type", wintypes.DWORD), ("remark", wintypes.LPWSTR),
            ("permissions", wintypes.DWORD), ("max_uses", wintypes.DWORD), ("current_uses", wintypes.DWORD),
            ("path", wintypes.LPWSTR), ("passwd", wintypes.LPWSTR), ("reserved", wintypes.DWORD),
            ("security_descriptor", ctypes.c_void_p),
        ]

    _advapi.GetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                         ctypes.POINTER(wintypes.DWORD)]
    _advapi.GetFileSecurityW.restype = wintypes.BOOL
    _advapi.LookupAccountNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p,
                                           ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR,
                                           ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
    _advapi.LookupAccountNameW.restype = wintypes.BOOL
    _advapi.LookupAccountSidW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, wintypes.LPWSTR,
                                          ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR,
                                          ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
    _advapi.LookupAccountSidW.restype = wintypes.BOOL
    _advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    _advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    _advapi.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    _advapi.ConvertStringSidToSidW.restype = wintypes.BOOL
    _advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR), ctypes.c_void_p]
    _advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    _advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    _advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    _advapi.GetSecurityDescriptorLength.argtypes = [ctypes.c_void_p]
    _advapi.GetSecurityDescriptorLength.restype = wintypes.DWORD
    _kernel.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel.LocalFree.restype = ctypes.c_void_p
    _authz.AuthzInitializeResourceManager.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
                                                      ctypes.c_void_p, wintypes.LPCWSTR,
                                                      ctypes.POINTER(ctypes.c_void_p)]
    _authz.AuthzInitializeResourceManager.restype = wintypes.BOOL
    _authz.AuthzInitializeContextFromSid.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
                                                     ctypes.c_void_p, LUID, ctypes.c_void_p,
                                                     ctypes.POINTER(ctypes.c_void_p)]
    _authz.AuthzInitializeContextFromSid.restype = wintypes.BOOL
    _authz.AuthzAccessCheck.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(AUTHZ_ACCESS_REQUEST),
                                        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                                        ctypes.POINTER(AUTHZ_ACCESS_REPLY), ctypes.c_void_p]
    _authz.AuthzAccessCheck.restype = wintypes.BOOL
    _authz.AuthzFreeContext.argtypes = [ctypes.c_void_p]
    _authz.AuthzFreeContext.restype = wintypes.BOOL
    _netapi.NetShareGetInfo.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                        ctypes.POINTER(ctypes.c_void_p)]
    _netapi.NetShareGetInfo.restype = wintypes.DWORD
    _netapi.NetApiBufferFree.argtypes = [ctypes.c_void_p]
    _netapi.NetApiBufferFree.restype = wintypes.DWORD

    def _err(what: str) -> SecurityError:
        code = ctypes.get_last_error()
        return SecurityError(f"{what}: {ctypes.FormatError(code).strip()} ({code})")

    def _long_path(path: str) -> str:
        if path.startswith("\\\\?\\") or len(path) < 240:
            return path
        if path.startswith("\\\\"):
            return "\\\\?\\UNC\\" + path[2:]
        return "\\\\?\\" + path

    def _sid_to_string(psid) -> str:
        out = wintypes.LPWSTR()
        if not _advapi.ConvertSidToStringSidW(psid, ctypes.byref(out)):
            raise _err("ConvertSidToStringSid")
        try:
            return out.value
        finally:
            _kernel.LocalFree(out)

    class WindowsSecurity(SecurityProvider):
        CONTEXT_TTL = 600

        def __init__(self):
            self._lock = threading.Lock()
            self._rm = ctypes.c_void_p()
            if not _authz.AuthzInitializeResourceManager(AUTHZ_RM_FLAG_NO_AUDIT, None, None, None, "Hound",
                                                         ctypes.byref(self._rm)):
                raise _err("AuthzInitializeResourceManager")
            self._contexts: dict[str, tuple[ctypes.c_void_p, float]] = {}

        def read_sd(self, path: str) -> bytes | None:
            info = OWNER_SECURITY_INFORMATION | GROUP_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION
            p = _long_path(path)
            needed = wintypes.DWORD(0)
            _advapi.GetFileSecurityW(p, info, None, 0, ctypes.byref(needed))
            if not needed.value:
                raise _err(f"GetFileSecurity({path})")
            buf = ctypes.create_string_buffer(needed.value)
            if not _advapi.GetFileSecurityW(p, info, buf, needed.value, ctypes.byref(needed)):
                raise _err(f"GetFileSecurity({path})")
            return buf.raw[: needed.value]

        def _context(self, sid: str):
            now = time.monotonic()
            hit = self._contexts.get(sid)
            if hit and now - hit[1] < self.CONTEXT_TTL:
                return hit[0]
            psid = ctypes.c_void_p()
            if not _advapi.ConvertStringSidToSidW(sid, ctypes.byref(psid)):
                raise _err(f"SID no válido {sid}")
            try:
                ctx = ctypes.c_void_p()
                if not _authz.AuthzInitializeContextFromSid(0, psid, self._rm, None, LUID(0, 0), None,
                                                            ctypes.byref(ctx)):
                    raise _err(f"No se pueden obtener los grupos de {sid}")
            finally:
                _kernel.LocalFree(psid)
            if hit:
                _authz.AuthzFreeContext(hit[0])
            self._contexts[sid] = (ctx, now)
            return ctx

        def check(self, sid: str, sd: bytes) -> bool:
            if not sd:
                return False
            with self._lock:
                ctx = self._context(sid)
                req = AUTHZ_ACCESS_REQUEST(FILE_GENERIC_READ, None, None, 0, None)
                granted = wintypes.DWORD(0)
                error = wintypes.DWORD(0)
                reply = AUTHZ_ACCESS_REPLY(1, ctypes.pointer(granted), None, ctypes.pointer(error))
                sd_buf = ctypes.create_string_buffer(sd, len(sd))
                if not _authz.AuthzAccessCheck(0, ctx, ctypes.byref(req), None, sd_buf, None, 0,
                                               ctypes.byref(reply), None):
                    raise _err("AuthzAccessCheck")
                return error.value == 0 and (granted.value & FILE_GENERIC_READ) == FILE_GENERIC_READ

        def lookup_account(self, name: str) -> tuple[str, str]:
            sid_size = wintypes.DWORD(0)
            dom_size = wintypes.DWORD(0)
            use = wintypes.DWORD(0)
            _advapi.LookupAccountNameW(None, name, None, ctypes.byref(sid_size), None, ctypes.byref(dom_size),
                                       ctypes.byref(use))
            if not sid_size.value:
                raise _err(f"No existe la cuenta '{name}'")
            sid_buf = ctypes.create_string_buffer(sid_size.value)
            dom_buf = ctypes.create_unicode_buffer(dom_size.value)
            if not _advapi.LookupAccountNameW(None, name, sid_buf, ctypes.byref(sid_size), dom_buf,
                                              ctypes.byref(dom_size), ctypes.byref(use)):
                raise _err(f"No existe la cuenta '{name}'")
            sid = _sid_to_string(sid_buf)
            # Nombre canónico DOMINIO\usuario tal y como lo devuelve Windows.
            n_size = wintypes.DWORD(256)
            d_size = wintypes.DWORD(256)
            n_buf = ctypes.create_unicode_buffer(256)
            d_buf = ctypes.create_unicode_buffer(256)
            if _advapi.LookupAccountSidW(None, sid_buf, n_buf, ctypes.byref(n_size), d_buf, ctypes.byref(d_size),
                                         ctypes.byref(use)):
                return f"{d_buf.value}\\{n_buf.value}", sid
            return name, sid

        def share_sd(self, share_name: str) -> bytes | None:
            buf = ctypes.c_void_p()
            rc = _netapi.NetShareGetInfo(None, share_name, 502, ctypes.byref(buf))
            if rc != 0:
                raise SecurityError(f"No se pueden leer los permisos del recurso compartido '{share_name}' "
                                    f"(código {rc})")
            try:
                info = ctypes.cast(buf, ctypes.POINTER(SHARE_INFO_502)).contents
                if not info.security_descriptor:
                    return None  # sin descriptor: Everyone tiene control total
                # El descriptor del recurso compartido no lleva propietario ni grupo, que
                # AuthzAccessCheck exige: lo reconstruimos con SYSTEM como propietario.
                sddl = wintypes.LPWSTR()
                if not _advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                        info.security_descriptor, SDDL_REVISION_1, DACL_SECURITY_INFORMATION,
                        ctypes.byref(sddl), None):
                    raise _err("ConvertSecurityDescriptorToStringSecurityDescriptor")
                try:
                    text = "O:SYG:SY" + sddl.value
                finally:
                    _kernel.LocalFree(sddl)
            finally:
                _netapi.NetApiBufferFree(buf)
            psd = ctypes.c_void_p()
            size = wintypes.DWORD(0)
            if not _advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(text, SDDL_REVISION_1,
                                                                                ctypes.byref(psd), ctypes.byref(size)):
                raise _err("ConvertStringSecurityDescriptorToSecurityDescriptor")
            try:
                return ctypes.string_at(psd, size.value)
            finally:
                _kernel.LocalFree(psd)
