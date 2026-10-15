"""Embeddings locales en CPU con ONNX Runtime (por defecto EmbeddingGemma 2, cuantizado int8).

El modelo se descarga una sola vez (`hound model download`) y después funciona sin conexión.
EmbeddingGemma 2 pone texto e imágenes en el mismo espacio: una consulta de texto ("la grieta
del baño") se compara directamente con el vector de una foto. El codificador de imágenes solo
lo carga el indexador; el servidor de búsqueda solo necesita el de texto.
"""

from __future__ import annotations

import math
import os
import re
import zlib
from dataclasses import dataclass
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


PATCH, POOL = 16, 3
BOI, EOI, IMAGE_TOKEN = "<|image>", "<image|>", "<|image|>"


@dataclass
class ImagePixels:
    """Imagen preparada para el codificador (se calcula en el proceso de extracción)."""
    patches: np.ndarray    # (n_patches, 16*16*3) uint8, sin relleno
    positions: np.ndarray  # (n_patches, 2) columna, fila
    tokens: int            # tokens visuales que saldrán (n_patches / 9)


def image_pixels(img, max_tokens: int = 140) -> ImagePixels:
    """Redimensiona conservando la proporción y trocea en parches de 16×16 (como Gemma 4)."""
    from PIL import Image

    img = img.convert("RGB")
    max_patches = max_tokens * POOL * POOL
    side = POOL * PATCH
    factor = math.sqrt(max_patches * PATCH * PATCH / (img.height * img.width))
    th, tw = int(factor * img.height // side) * side, int(factor * img.width // side) * side
    max_side = (max_patches // POOL ** 2) * side
    if th == 0:
        th, tw = side, min(int(img.width // img.height) * side, max_side)
    elif tw == 0:
        tw, th = side, min(int(img.height // img.width) * side, max_side)
    if (th, tw) != (img.height, img.width):
        img = img.resize((tw, th), Image.BICUBIC)
    a = np.asarray(img, dtype=np.uint8)
    ph, pw = th // PATCH, tw // PATCH
    patches = a.reshape(ph, PATCH, pw, PATCH, 3).transpose(0, 2, 1, 3, 4).reshape(ph * pw, -1)
    xs, ys = np.meshgrid(np.arange(pw), np.arange(ph), indexing="xy")
    return ImagePixels(np.ascontiguousarray(patches), np.stack([xs, ys], -1).reshape(-1, 2).astype(np.int16),
                       (ph * pw) // POOL ** 2)


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
                        "config.json", "special_tokens_map.json",
                        *([cfg.vision_file, cfg.vision_file + "_data"] if cfg.vision_file else [])],
    )
    return target


class OnnxEmbedder:
    def __init__(self, cfg: ModelConfig, models_path: Path, threads: int | None = None, vision: bool = False):
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
        # EmbeddingGemma 2: entradas para imágenes, vídeo y audio (vacías cuando solo hay texto).
        self._media = {i.name: i.shape[-1] for i in self.session.get_inputs() if i.name.endswith("_features")}
        self.vision = None
        vision_file = base / cfg.vision_file if cfg.vision_file else None
        if vision and vision_file is not None and "image_features" in self._media:
            if vision_file.exists():
                self.vision = ort.InferenceSession(str(vision_file), so, providers=["CPUExecutionProvider"])
            else:
                import logging

                logging.getLogger("hound.embed").warning(
                    "Sin búsqueda por lo que se ve en las imágenes: falta %s (hound model download)", vision_file)
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
        return self._finish(feeds, mask)

    def _finish(self, feeds: dict, mask: np.ndarray, **media: np.ndarray) -> np.ndarray:
        for name, width in self._media.items():
            feeds[name] = media.get(name, np.zeros((0, width), dtype=np.float32))
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

    def embed_images(self, images: list[ImagePixels]) -> np.ndarray:
        """Vector de "lo que se ve" en cada imagen, en el mismo espacio que las consultas de texto."""
        if self.vision is None:
            raise RuntimeError("El modelo no tiene codificador de imágenes")
        out = np.zeros((len(images), self.dim), dtype=np.float32)
        ids_of = self.tok.token_to_id
        for k, px in enumerate(images):
            # Relleno hasta el máximo de parches, como el procesador original (posición -1 = vacío).
            n = max(len(px.patches), self.cfg.image_tokens * POOL * POOL)
            pv = np.zeros((1, n, px.patches.shape[1]), dtype=np.float32)
            pp = np.full((1, n, 2), -1, dtype=np.int64)
            pv[0, :len(px.patches)] = px.patches.astype(np.float32) / 255.0
            pp[0, :len(px.patches)] = px.positions
            feats = self.vision.run(["image_features"], {"pixel_values": pv, "pixel_position_ids": pp})[0]
            feats = feats[:px.tokens]
            body = [ids_of(BOI)] + [ids_of(IMAGE_TOKEN)] * len(feats) + [ids_of(EOI)]
            base = self.tok.encode("").ids  # <bos> … <eos> del tokenizador
            ids = np.array([base[:1] + body + base[1:]], dtype=np.int64)
            mask = np.ones_like(ids)
            out[k] = self._finish({"input_ids": ids, "attention_mask": mask}, mask,
                                  image_features=feats.astype(np.float32))[0]
        return out


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

    vision = True  # imágenes de prueba: el color dominante pasa a una palabra ("rojo", "verde", "azul")

    def embed_images(self, images: list[ImagePixels]) -> np.ndarray:
        words = []
        for px in images:
            r, g, b = px.patches.reshape(-1, 3).mean(0)
            words.append(("rojo", "verde", "azul")[int(np.argmax([r, g, b]))] + " foto imagen")
        return _normalize(np.stack([self._vec(w) for w in words]))


def load_embedder(cfg: ModelConfig, models_path: Path, threads: int | None = None,
                  vision: bool = False) -> Embedder:
    if cfg.repo == "hash":
        return HashEmbedder(cfg.dim)
    return OnnxEmbedder(cfg, models_path, threads, vision=vision)
