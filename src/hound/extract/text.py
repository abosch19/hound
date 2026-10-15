"""Texto plano, CSV y RTF."""

from __future__ import annotations

import csv
import io
import re

from . import Block, Extracted

MAX_TEXT_BYTES = 30 * 1024 * 1024


def decode(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", "replace")  # lo habitual en ficheros antiguos de Windows en español


def read_text(path: str) -> str:
    with open(path, "rb") as fh:
        return decode(fh.read(MAX_TEXT_BYTES))


def paragraphs(text: str, unit: str = "párrafo") -> list[Block]:
    out = []
    for i, para in enumerate(re.split(r"\n\s*\n", text.replace("\r\n", "\n")), start=1):
        if para.strip():
            out.append(Block(para.strip(), unit=unit, n=i))
    return out


def extract_text(path: str) -> Extracted:
    return Extracted(blocks=paragraphs(read_text(path)))


def extract_csv(path: str) -> Extracted:
    text = read_text(path)
    sample = text[:20000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t|")
    except csv.Error:
        dialect = csv.excel
    out = Extracted()
    header = ""
    for i, row in enumerate(csv.reader(io.StringIO(text), dialect), start=1):
        line = " | ".join(c.strip() for c in row if c.strip())
        if not line:
            continue
        if not header:
            header = line
        out.blocks.append(Block(line, unit="fila", n=i, context=header if line != header else ""))
    return out


_RTF_DESTINATIONS = re.compile(r"\{\\\*[^{}]*\}|\{\\(?:fonttbl|colortbl|stylesheet|info|pict)[^{}]*(?:\{[^{}]*\}[^{}]*)*\}")
_RTF_HEX = re.compile(r"\\'([0-9a-fA-F]{2})")
_RTF_UNI = re.compile(r"\\u(-?\d+)\??")
_RTF_CTRL = re.compile(r"\\[a-zA-Z]+-?\d* ?")


def rtf_to_text(rtf: str) -> str:
    text = _RTF_DESTINATIONS.sub("", rtf)
    text = re.sub(r"\\(?:par|line)(?![a-z])", "\n", text)
    text = re.sub(r"\\tab(?![a-z])", "\t", text)
    text = _RTF_UNI.sub(lambda m: chr(int(m.group(1)) % 65536), text)
    text = _RTF_HEX.sub(lambda m: bytes([int(m.group(1), 16)]).decode("cp1252", "replace"), text)
    text = _RTF_CTRL.sub("", text)
    text = text.replace("\\{", "{").replace("\\}", "}").replace("\\\\", "\\")
    return re.sub(r"[{}]", "", text)


def extract_rtf(path: str) -> Extracted:
    return Extracted(blocks=paragraphs(rtf_to_text(read_text(path))))
