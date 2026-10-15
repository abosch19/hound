"""Embeddings locales en CPU con ONNX Runtime (por defecto EmbeddingGemma-300M, cuantizado int8).

El modelo se descarga una sola vez (`hound model download`) y después funciona sin conexión.
"""

from __future__ import annotations

import os
import re
import zlib
from pathlib import Path
from typing import Protocol

import numpy as np

from .config import ModelConfig


class ModelMissing(RuntimeError):
    pass


class Embedder(Protocol):
    dim: int
    name: str

    def embed_documents(self, items: list[tuple[str, str]]) -> np.ndarray: ...

    def embed_query(self, query: str) -> np.ndarray: ...


def _normalize(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return (v / n).astype(np.float32)


def model_dir(cfg: ModelConfig, models_path: Path) -> Path:
    return models_path / cfg.repo.replace("/", "--")


def download_model(cfg: ModelConfig, models_path: Path) -> Path:
    from huggingface_hub import snapshot_download

    target = model_dir(cfg, models_path)
    snapshot_download(
        cfg.repo,
        local_dir=str(target),
        allow_patterns=[cfg.file, cfg.file + "_data", "tokenizer.json", "tokenizer_config.json",
                        "config.json", "special_tokens_map.json"],
    )
    return target


class OnnxEmbedder:
    def __init__(self, cfg: ModelConfig, models_path: Path, threads: int | None = None):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        base = model_dir(cfg, models_path)
        model_file = base / cfg.file
        tok_file = base / "tokenizer.json"
        if not model_file.exists() or not tok_file.exists():
            raise ModelMissing(f"No se encuentra el modelo en {base}. Ejecuta: hound model download")
        self.cfg = cfg
        self.dim = cfg.dim
        self.name = f"{cfg.repo}:{cfg.file}:{cfg.dim}"
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads or cfg.threads or max(1, (os.cpu_count() or 2) // 2)
        so.inter_op_num_threads = 1
        so.log_severity_level = 3
        self.session = ort.InferenceSession(str(model_file), so, providers=["CPUExecutionProvider"])
        self.input_names = {i.name for i in self.session.get_inputs()}
        self.output_names = [o.name for o in self.session.get_outputs()]
        self.tok = Tokenizer.from_file(str(tok_file))
        self.tok.enable_truncation(cfg.max_tokens)
        pad_id = self.tok.token_to_id("<pad>")
        self.tok.enable_padding(pad_id=pad_id if pad_id is not None else 0, pad_token="<pad>")

    def _run(self, texts: list[str]) -> np.ndarray:
        enc = self.tok.encode_batch(texts)
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
        feeds = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.input_names:
            feeds["token_type_ids"] = np.zeros_like(ids)
        outs = self.session.run(None, {k: v for k, v in feeds.items() if k in self.input_names})
        if "sentence_embedding" in self.output_names:
            vec = outs[self.output_names.index("sentence_embedding")]
        else:  # media de los tokens
            hidden = outs[0]
            m = mask[..., None].astype(np.float32)
            vec = (hidden * m).sum(1) / np.maximum(m.sum(1), 1)
        return _normalize(np.asarray(vec, dtype=np.float32)[:, : self.dim])

    def embed_documents(self, items: list[tuple[str, str]]) -> np.ndarray:
        texts = [self.cfg.document_prefix.format(title=(title or "none").replace("|", " ")) + text
                 for title, text in items]
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        bs = max(1, self.cfg.batch_size)
        for s in range(0, len(order), bs):
            idx = order[s:s + bs]
            out[idx] = self._run([texts[i] for i in idx])
        return out

    def embed_query(self, query: str) -> np.ndarray:
        return self._run([self.cfg.query_prefix + query])[0]


class HashEmbedder:
    """Embedder determinista por bolsa de palabras. Solo para tests y pruebas sin modelo."""

    name = "hash"

    def __init__(self, dim: int = 256):
        self.dim = dim

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        for w in re.findall(r"\w{3,}", text.lower()):
            v[zlib.crc32(w[:6].encode()) % self.dim] += 1.0
        return v

    def embed_documents(self, items: list[tuple[str, str]]) -> np.ndarray:
        if not items:
            return np.zeros((0, self.dim), dtype=np.float32)
        return _normalize(np.stack([self._vec(f"{t} {x}") for t, x in items]))

    def embed_query(self, query: str) -> np.ndarray:
        return _normalize(self._vec(query)[None, :])[0]


def load_embedder(cfg: ModelConfig, models_path: Path, threads: int | None = None) -> Embedder:
    if cfg.repo == "hash":
        return HashEmbedder(cfg.dim)
    return OnnxEmbedder(cfg, models_path, threads)
