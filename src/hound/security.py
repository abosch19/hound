"""Permisos: lectura de ACL de NTFS al indexar y comprobación de acceso por usuario al buscar.

No reimplementamos la lógica de las ACL (herencia, "Denegar", grupos anidados): usamos
la API Authz de Windows, que la evalúa igual que el propio sistema de ficheros, a partir
del SID del usuario y del descriptor de seguridad guardado en el índice.

- `read_sd(path)`: descriptor de seguridad del fichero (propietario, grupo y DACL).
- `check(sid, sd)`: ¿puede ese usuario leer un fichero con ese descriptor?
- `lookup_account(nombre)`: nombre canónico y SID de una cuenta local o del dominio.
"""

from __future__ import annotations

import sys
import threading
import time
from abc import ABC, abstractmethod

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


def get_security(mode: str) -> SecurityProvider:
    if mode == "none":
        return NoSecurity()
    if sys.platform != "win32":
        raise SecurityError("permissions.mode = 'ntfs' solo funciona en Windows. Usa 'none' para pruebas.")
    return WindowsSecurity()


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
