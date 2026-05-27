"""Embedding providers for RAG retrieval.

Two providers are shipped:

* ``HashingEmbeddingProvider`` — deterministic, zero‑dependency
  feature‑hashing vectorizer. Good baseline for short, tag‑rich texts
  (which is exactly what our L2 summaries look like).
* ``SentenceTransformerProvider`` — wraps ``sentence-transformers``
  if installed. Used when ``RAG_EMBEDDING_PROVIDER=sentence-transformers``.

Both expose the same interface so the retrieval layer can stay
agnostic. Vectors are returned as float32 ``numpy.ndarray`` already
L2‑normalized so cosine similarity reduces to a dot product.
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Iterable, List, Optional, Protocol, Sequence

import numpy as np


_TOKEN_RE = re.compile(r"[A-Za-z0-9_.:@/-]+")


def anonymize_services(text: str, services: Sequence[str]) -> str:
    """Replace concrete service names with positional placeholders.

    The mapping is built from the **order of first appearance** in *text*
    (with *services* as a hint set of known service identifiers). This
    keeps embeddings structural: a "memory pressure on the first hop"
    pattern produces a similar vector regardless of whether the first
    hop is ``user`` or ``orders``.

    Used to remove ground-truth leakage from the embedding input — the
    raw ``summary_text`` (with real names) is still kept for prompt
    assembly and human inspection.

    Substitutions are whole-word, case-insensitive. Service identifiers
    are matched literally (including ``-`` and ``_``).
    """
    if not text or not services:
        return text or ""

    # Deduplicate while preserving the original order of the hint list.
    seen: set[str] = set()
    candidates: List[str] = []
    for svc in services:
        if not svc:
            continue
        key = svc.strip()
        if not key or key.lower() in seen:
            continue
        seen.add(key.lower())
        candidates.append(key)

    # Re-order by first appearance in the text so the mapping is stable
    # across runs that mention the same services in the same role.
    def _first_pos(svc: str) -> int:
        m = re.search(rf"\b{re.escape(svc)}\b", text, flags=re.IGNORECASE)
        return m.start() if m else 10**9
    candidates.sort(key=_first_pos)

    out = text
    for idx, svc in enumerate(candidates):
        placeholder = f"<svc_{idx}>"
        out = re.sub(
            rf"\b{re.escape(svc)}\b",
            placeholder,
            out,
            flags=re.IGNORECASE,
        )
    return out


class EmbeddingProvider(Protocol):
    name: str
    dim: int

    def embed(self, text: str) -> np.ndarray: ...
    def embed_batch(self, texts: Sequence[str]) -> np.ndarray: ...


def _normalize(vec: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vec))
    if norm == 0.0:
        return vec.astype(np.float32, copy=False)
    return (vec / norm).astype(np.float32, copy=False)


def _tokenize(text: str) -> List[str]:
    if not text:
        return []
    return [t.lower() for t in _TOKEN_RE.findall(text)]


class HashingEmbeddingProvider:
    """Feature‑hashing vectorizer with signed hashing trick.

    For each token we hash to a bucket in ``[0, dim)`` and add either +1
    or -1 (sign derived from a second hash byte). The resulting vector
    is L2 normalized. This gives a deterministic, dependency‑free
    embedding suitable for the tag‑rich L2 summaries.
    """

    name = "hashing-v1"

    def __init__(self, dim: int = 256, ngram_range: tuple[int, int] = (1, 2)) -> None:
        self.dim = int(dim)
        self.ngram_range = ngram_range

    # ---- internals ----------------------------------------------------
    def _ngrams(self, tokens: List[str]) -> Iterable[str]:
        lo, hi = self.ngram_range
        n_tokens = len(tokens)
        for n in range(lo, hi + 1):
            if n_tokens < n:
                continue
            for i in range(n_tokens - n + 1):
                yield " ".join(tokens[i : i + n])

    def _hash(self, term: str) -> tuple[int, int]:
        digest = hashlib.blake2b(term.encode("utf-8"), digest_size=8).digest()
        bucket = int.from_bytes(digest[:4], "little") % self.dim
        sign = 1 if (digest[4] & 0x01) else -1
        return bucket, sign

    # ---- public API ---------------------------------------------------
    def embed(self, text: str) -> np.ndarray:
        vec = np.zeros(self.dim, dtype=np.float32)
        tokens = _tokenize(text)
        if not tokens:
            return vec
        for term in self._ngrams(tokens):
            bucket, sign = self._hash(term)
            vec[bucket] += sign
        return _normalize(vec)

    def embed_batch(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.stack([self.embed(t) for t in texts], axis=0)


class SentenceTransformerProvider:
    """Optional provider — only loaded if the package is installed."""

    def __init__(
        self,
        model_name: str = "sentence-transformers/all-MiniLM-L6-v2",
    ) -> None:
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "sentence-transformers is not installed; "
                "set RAG_EMBEDDING_PROVIDER=hashing or install the package"
            ) from exc
        self._model = SentenceTransformer(model_name)
        self.name = f"st:{model_name}"
        self.dim = int(self._model.get_sentence_embedding_dimension() or 384)

    def embed(self, text: str) -> np.ndarray:
        vec = self._model.encode(text, normalize_embeddings=True)
        return np.asarray(vec, dtype=np.float32)

    def embed_batch(self, texts: Sequence[str]) -> np.ndarray:
        arr = self._model.encode(
            list(texts), normalize_embeddings=True, convert_to_numpy=True
        )
        return np.asarray(arr, dtype=np.float32)


# ── selection ────────────────────────────────────────────────────────────
_PROVIDER_CACHE: Optional[EmbeddingProvider] = None


def get_embedding_provider() -> EmbeddingProvider:
    """Return the singleton embedding provider selected by env."""
    global _PROVIDER_CACHE
    if _PROVIDER_CACHE is not None:
        return _PROVIDER_CACHE
    choice = os.environ.get("RAG_EMBEDDING_PROVIDER", "hashing").lower()
    if choice in ("st", "sentence-transformers", "sbert"):
        _PROVIDER_CACHE = SentenceTransformerProvider(
            os.environ.get(
                "RAG_EMBEDDING_MODEL",
                "sentence-transformers/all-MiniLM-L6-v2",
            )
        )
    else:
        dim = int(os.environ.get("RAG_EMBEDDING_DIM", "256"))
        _PROVIDER_CACHE = HashingEmbeddingProvider(dim=dim)
    return _PROVIDER_CACHE


def reset_embedding_provider() -> None:
    """Reset the cached provider (mostly useful for tests)."""
    global _PROVIDER_CACHE
    _PROVIDER_CACHE = None


# ── serialization helpers ───────────────────────────────────────────────
def vector_to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def blob_to_vector(blob: bytes, dim: int) -> np.ndarray:
    arr = np.frombuffer(blob, dtype=np.float32)
    if arr.size != dim:
        raise ValueError(
            f"embedding dim mismatch: blob has {arr.size}, expected {dim}"
        )
    return arr
