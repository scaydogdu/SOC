"""Hybrid retrieval over the current versions of translation notes."""

import hashlib
import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import import_module
from math import fsum, hypot, isfinite, log1p
from typing import Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

BGE_M3_MODEL = "BAAI/bge-m3"
BGE_M3_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
_WORD = re.compile(r"[^\W_]+")


class Embedder(Protocol):
    def embed(self, text: str) -> Sequence[float]: ...


class _Encoder(Protocol):
    def encode(
        self, text: str, *, normalize_embeddings: bool, show_progress_bar: bool
    ) -> Sequence[float]: ...


class BGEM3Embedder:
    def __init__(self, *, device: str = "cpu", local_files_only: bool = True) -> None:
        library = import_module("sentence_transformers")
        checkpoint = BGE_M3_MODEL
        revision: str | None = BGE_M3_REVISION
        if local_files_only:
            hub = import_module("huggingface_hub")
            checkpoint = hub.snapshot_download(
                repo_id=BGE_M3_MODEL, revision=BGE_M3_REVISION, local_files_only=True
            )
            revision = None
        self._encoder = cast(
            _Encoder,
            library.SentenceTransformer(
                checkpoint,
                revision=revision,
                device=device,
                local_files_only=local_files_only,
                trust_remote_code=False,
                model_kwargs={"weights_only": True, "use_safetensors": False},
            ),
        )

    def embed(self, text: str) -> Sequence[float]:
        return tuple(
            float(value)
            for value in self._encoder.encode(
                text, normalize_embeddings=True, show_progress_bar=False
            )
        )


def _words(text: str) -> list[str]:
    return _WORD.findall(
        unicodedata.normalize("NFC", text).replace("İ", "i").casefold()
    )


class HashEmbedder:
    """Deterministic small embedder for offline fixtures."""

    def embed(self, text: str) -> Sequence[float]:
        vector = [0.0] * 64
        for word in _words(text):
            digest = hashlib.blake2b(word.encode("utf-8"), digest_size=2).digest()
            vector[digest[0] % len(vector)] += 1 if digest[1] & 1 else -1
        return vector


class RetrievalConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    top_k: int = Field(default=5, ge=0)
    dense_weight: float = Field(default=0.5, ge=0, le=1)


@dataclass(frozen=True)
class _IndexedNote:
    terms: Counter[str]
    vector: tuple[float, ...]


class HybridIndex:
    def __init__(self, embedder: Embedder, config: RetrievalConfig) -> None:
        self._embedder = embedder
        self._config = config
        self._notes: dict[str, _IndexedNote] = {}
        self._dimension: int | None = None

    def _unit_vector(self, text: str) -> tuple[float, ...]:
        vector = tuple(self._embedder.embed(text))
        magnitude = hypot(*vector)
        if not vector or not isfinite(magnitude):
            raise ValueError("embedder must return a nonempty finite vector")
        if self._dimension is not None and len(vector) != self._dimension:
            raise ValueError("embedder changed vector dimensions")
        return tuple(value / magnitude for value in vector) if magnitude else vector

    def upsert(self, key: str, text: str) -> None:
        vector = self._unit_vector(text)
        self._notes[key] = _IndexedNote(Counter(_words(text)), vector)
        self._dimension = len(vector)

    def search(self, text: str) -> tuple[str, ...]:
        if not self._notes or self._config.top_k == 0:
            return ()
        query = self._unit_vector(text)
        sparse = self._bm25(text)
        max_sparse = max(sparse.values()) or 1.0
        weight = self._config.dense_weight
        scores = {}
        for key, note in self._notes.items():
            cosine = fsum(
                left * right for left, right in zip(query, note.vector, strict=True)
            )
            dense = min(1.0, max(0.0, cosine))
            scores[key] = weight * dense + (1 - weight) * sparse[key] / max_sparse
        return tuple(
            sorted(scores, key=lambda key: (-scores[key], key))[: self._config.top_k]
        )

    def _bm25(self, text: str) -> dict[str, float]:
        terms = sorted(set(_words(text)))
        count = len(self._notes)
        lengths = {key: note.terms.total() for key, note in self._notes.items()}
        average_length = sum(lengths.values()) / count or 1.0
        frequencies = {
            term: sum(term in note.terms for note in self._notes.values())
            for term in terms
        }
        idf = {
            term: log1p((count - freq + 0.5) / (freq + 0.5))
            for term, freq in frequencies.items()
        }
        k1, b = 1.2, 0.75
        scores = {}
        for key, note in self._notes.items():
            length_factor = k1 * (1 - b + b * lengths[key] / average_length)
            scores[key] = fsum(
                idf[term] * freq * (k1 + 1) / (freq + length_factor)
                for term in terms
                if (freq := note.terms[term])
            )
        return scores
