"""Carga de la configuración (config.toml) con valores por defecto."""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

DEFAULT_EXTENSIONS = [
    # documentos
    ".pdf", ".docx", ".docm", ".xlsx", ".xlsm", ".xls", ".pptx", ".pptm",
    ".odt", ".ods", ".odp", ".msg", ".eml", ".txt", ".csv", ".md", ".rtf",
    # imágenes (OCR)
    ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp",
    # CAD
    ".dxf", ".dwg",
    # se indexan solo por nombre y ruta
    ".doc", ".ppt", ".zip", ".rar", ".7z", ".mp4", ".mov", ".mp3", ".wav",
]

DEFAULT_EXCLUDE_NAMES = [
    "$RECYCLE.BIN", "System Volume Information", "~snapshot", ".git",
    "node_modules", "Thumbs.db", "desktop.ini", "~$*", "*.tmp", "~*.tmp", ".~lock.*",
]


def default_data_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Hound"
    return Path.home() / ".local" / "share" / "hound"


def default_config_path() -> Path:
    env = os.environ.get("HOUND_CONFIG")
    if env:
        return Path(env)
    return default_data_dir() / "config.toml"


@dataclass
class ShareConfig:
    path: str = ""
    """Ruta local de la carpeta compartida en el servidor (p. ej. D:\\Compartido)."""
    unc: str = ""
    """Ruta UNC con la que los usuarios la ven (p. ej. \\\\SERVIDOR\\Compartido)."""
    name: str = ""
    """Nombre del recurso compartido SMB, para aplicar también sus permisos (opcional)."""


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8765
    tls_cert: str = ""
    tls_key: str = ""


@dataclass
class ScheduleConfig:
    busy_hours: str = "08:00-19:00"
    busy_days: str = "mon-fri"
    busy_mode: str = "slow"
    """slow: indexa despacio en horario laboral; pause: no indexa; normal: sin cambios."""
    busy_pause_ms: int = 500


@dataclass
class IndexConfig:
    extensions: list[str] = field(default_factory=lambda: list(DEFAULT_EXTENSIONS))
    exclude: list[str] = field(default_factory=list)
    """Carpetas (relativas a la carpeta compartida) que no se indexan ni son consultables."""
    exclude_names: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_NAMES))
    """Patrones de nombre (glob) que se ignoran en cualquier nivel."""
    max_file_mb: int = 300
    rescan_minutes: int = 360
    watch: bool = True
    extract_workers: int = 2
    file_timeout_s: int = 600
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)


@dataclass
class OcrConfig:
    enabled: bool = True
    tesseract: str = ""
    tessdata: str = ""
    languages: str = "spa+eng"
    pdf_min_chars: int = 40
    dpi: int = 300
    images: bool = True
    max_pages: int = 300
    min_image_px: int = 300


@dataclass
class CadConfig:
    oda_converter: str = ""
    """Ruta a ODAFileConverter(.exe) para leer textos de ficheros DWG (opcional)."""


@dataclass
class ModelConfig:
    repo: str = "onnx-community/embeddinggemma-300m-ONNX"
    file: str = "onnx/model_quantized.onnx"
    dim: int = 768
    max_tokens: int = 512
    query_prefix: str = "task: search result | query: "
    document_prefix: str = "title: {title} | text: "
    threads: int = 0
    batch_size: int = 16


@dataclass
class SearchConfig:
    memory_dtype: str = "float32"
    rrf_k: int = 60
    candidates: int = 300
    chunk_chars: int = 1400
    chunk_overlap: int = 200


@dataclass
class PermissionsConfig:
    mode: str = "ntfs" if sys.platform == "win32" else "posix"
    """ntfs (Windows) / posix (Linux): filtra por los permisos de cada fichero; none: todos ven todo."""
    cache_minutes: int = 10


@dataclass
class Config:
    share: ShareConfig = field(default_factory=ShareConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    index: IndexConfig = field(default_factory=IndexConfig)
    ocr: OcrConfig = field(default_factory=OcrConfig)
    cad: CadConfig = field(default_factory=CadConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    permissions: PermissionsConfig = field(default_factory=PermissionsConfig)
    data_dir: str = ""
    config_path: str = ""

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir) if self.data_dir else default_data_dir()

    @property
    def db_path(self) -> Path:
        return self.data_path / "index.sqlite"

    @property
    def models_path(self) -> Path:
        return self.data_path / "models"

    @property
    def logs_path(self) -> Path:
        return self.data_path / "logs"


def _merge(obj: Any, values: dict[str, Any], where: str) -> None:
    known = {f.name: f for f in fields(obj)}
    for key, value in values.items():
        if key not in known:
            raise ValueError(f"Opción desconocida en la configuración: {where}{key}")
        current = getattr(obj, key)
        if is_dataclass(current):
            if not isinstance(value, dict):
                raise ValueError(f"{where}{key} debe ser una sección")
            _merge(current, value, f"{where}{key}.")
        else:
            setattr(obj, key, value)


def load_config(path: str | Path | None = None) -> Config:
    cfg = Config()
    p = Path(path) if path else default_config_path()
    if p.exists():
        # utf-8-sig: el Bloc de notas y PowerShell 5 suelen guardar con BOM, que tomllib no acepta.
        _merge(cfg, tomllib.loads(p.read_text(encoding="utf-8-sig")), "")
        cfg.config_path = str(p)
    cfg.index.extensions = [e.lower() if e.startswith(".") else "." + e.lower() for e in cfg.index.extensions]
    if cfg.permissions.mode not in ("ntfs", "posix", "none"):
        raise ValueError("permissions.mode debe ser 'ntfs', 'posix' o 'none'")
    return cfg
