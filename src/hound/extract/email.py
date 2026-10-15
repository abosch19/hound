"""Correos .eml (MIME) y .msg (Outlook, formato OLE), con sus adjuntos."""

from __future__ import annotations

import datetime as dt
import email
import email.policy
import os
import re
import struct
import tempfile
from html.parser import HTMLParser

from . import Block, Extracted, ExtractOptions, kind_of

MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
ATTACHMENT_KINDS = {"pdf", "word", "excel", "powerpoint", "text", "image"}


class _HTMLText(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head") and self.skip:
            self.skip -= 1
        elif tag in ("td", "th"):
            self.parts.append(" | ")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _HTMLText()
    p.feed(html)
    text = "".join(p.parts)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _header_block(fields: list[tuple[str, str]]) -> Block:
    return Block("\n".join(f"{k}: {v}" for k, v in fields if v), group="cabecera")


def _body_blocks(body: str) -> list[Block]:
    out = []
    for i, para in enumerate(re.split(r"\n\s*\n", body), start=1):
        if para.strip():
            out.append(Block(para.strip(), group="cuerpo", unit="párrafo", n=i))
    return out


def _attachment_blocks(name: str, data: bytes, opts: ExtractOptions, depth: int, out: Extracted) -> None:
    from . import extract

    ext = os.path.splitext(name)[1].lower()
    if depth > 0 or not opts.attachments or kind_of(ext) not in ATTACHMENT_KINDS or len(data) > MAX_ATTACHMENT_BYTES:
        return
    fd, tmp = tempfile.mkstemp(suffix=ext, prefix="hound-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        sub = extract(tmp, opts, depth=depth + 1)
        for b in sub.blocks:
            group = f"adjunto {name}" + (f", {b.group}" if b.group else "")
            out.blocks.append(Block(b.text, group=group, unit=b.unit, n=b.n, context=b.context))
    except Exception as exc:  # noqa: BLE001 — un adjunto roto no invalida el correo
        out.notes.append(f"Adjunto {name}: {exc}")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def extract_eml(path: str, opts: ExtractOptions, depth: int = 0) -> Extracted:
    with open(path, "rb") as fh:
        msg = email.message_from_binary_file(fh, policy=email.policy.default)
    subject = str(msg.get("subject", "") or "")
    out = Extracted(title=subject)
    attachments = []
    for part in msg.iter_attachments():
        fname = part.get_filename()
        if fname:
            attachments.append(fname)
    out.blocks.append(_header_block([
        ("Asunto", subject), ("De", str(msg.get("from", "") or "")), ("Para", str(msg.get("to", "") or "")),
        ("CC", str(msg.get("cc", "") or "")), ("Fecha", str(msg.get("date", "") or "")),
        ("Adjuntos", ", ".join(attachments)),
    ]))
    body_part = msg.get_body(preferencelist=("plain", "html"))
    if body_part is not None:
        try:
            body = body_part.get_content()
        except (LookupError, UnicodeDecodeError):
            body = body_part.get_payload(decode=True).decode("utf-8", "replace")
        if body_part.get_content_subtype() == "html":
            body = html_to_text(body)
        out.blocks.extend(_body_blocks(body))
    for part in msg.iter_attachments():
        fname = part.get_filename()
        if fname:
            data = part.get_payload(decode=True) or b""
            _attachment_blocks(fname, data, opts, depth, out)
    return out


# --- Outlook .msg ---------------------------------------------------------------

def _filetime(value: int) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime(1601, 1, 1) + dt.timedelta(microseconds=value // 10)
    except OverflowError:
        return None


class _Msg:
    def __init__(self, ole, prefix: str = ""):
        self.ole = ole
        self.prefix = prefix

    def _stream(self, name: str) -> bytes | None:
        full = self.prefix + name
        if self.ole.exists(full):
            return self.ole.openstream(full).read()
        return None

    def string(self, propid: int) -> str:
        data = self._stream(f"__substg1.0_{propid:04X}001F")
        if data is not None:
            return data.decode("utf-16-le", "replace").rstrip("\x00")
        data = self._stream(f"__substg1.0_{propid:04X}001E")
        if data is not None:
            return data.decode("cp1252", "replace").rstrip("\x00")
        return ""

    def binary(self, propid: int) -> bytes | None:
        return self._stream(f"__substg1.0_{propid:04X}0102")

    def time(self, *propids: int) -> dt.datetime | None:
        data = self._stream("__properties_version1.0")
        if not data:
            return None
        header = 32 if not self.prefix else 8
        wanted = {(pid << 16) | 0x0040 for pid in propids}
        found = {}
        for off in range(header, len(data) - 15, 16):
            tag, _flags, value = struct.unpack_from("<IIQ", data, off)
            if tag in wanted:
                found[tag >> 16] = value
        for pid in propids:
            if pid in found:
                return _filetime(found[pid])
        return None


def extract_msg(path: str, opts: ExtractOptions, depth: int = 0) -> Extracted:
    import olefile

    from . import ExtractionError

    if not olefile.isOleFile(path):
        raise ExtractionError("No es un fichero .msg válido")
    with olefile.OleFileIO(path) as ole:
        m = _Msg(ole)
        subject = m.string(0x0037)
        sent = m.time(0x0039, 0x0E06)
        attachments: list[tuple[str, bytes | None]] = []
        for entry in ole.listdir(streams=False, storages=True):
            if len(entry) == 1 and entry[0].startswith("__attach_version1.0_"):
                a = _Msg(ole, entry[0] + "/")
                name = a.string(0x3707) or a.string(0x3704) or a.string(0x3001)
                if name:
                    attachments.append((name, a.binary(0x3701)))
        out = Extracted(title=subject)
        sender = m.string(0x0C1A)
        sender_mail = m.string(0x5D01) or m.string(0x0C1F)
        out.blocks.append(_header_block([
            ("Asunto", subject),
            ("De", f"{sender} <{sender_mail}>" if sender and sender_mail and "@" in sender_mail else sender or sender_mail),
            ("Para", m.string(0x0E04)), ("CC", m.string(0x0E03)),
            ("Fecha", sent.strftime("%d/%m/%Y %H:%M") if sent else ""),
            ("Adjuntos", ", ".join(n for n, _ in attachments)),
        ]))
        body = m.string(0x1000)
        if not body.strip():
            html = m.binary(0x1013)
            if html is None:
                html_s = m.string(0x1013)
            else:
                html_s = html.decode("utf-8", "replace") if b"charset=utf-8" in html[:2000].lower() else html.decode("cp1252", "replace")
            body = html_to_text(html_s) if html_s else ""
        out.blocks.extend(_body_blocks(body))
        for name, data in attachments:
            if data:
                _attachment_blocks(name, data, opts, depth, out)
    return out
