"""Embedding providers for vector memory (PR-04).

Goal: replace the original TF-IDF word-hash with a clean `EmbeddingProvider`
abstraction. Three implementations are shipped:

1. `HashingEmbeddingProvider` — the original simple_text_hash. Always available,
   no external deps, used as the **default fallback** for offline mode.

2. `SentenceTransformerProvider` — production-grade semantic embeddings using
   `sentence-transformers` MiniLM (all-MiniLM-L6-v2, 384-dim). Loaded lazily;
   raises `ImportError` if the package is not installed.

3. `TfidfEmbeddingProvider` — sklearn TfidfVectorizer based, trained on the
   corpus as documents are added. Best for small/static corpora.

The factory `get_default_provider()` returns SentenceTransformer if available,
else Hashing fallback. Callers can override via `EMBEDDING_PROVIDER` config:
"auto" | "sentence-transformers" | "tfidf" | "hashing".

All providers expose the same interface:

    provider.encode(text: str) -> list[float]
    provider.dim -> int  # property
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import List, Protocol, runtime_checkable

import numpy as np

logger = logging.getLogger(__name__)


# ── Protocol ───────────────────────────────────────────────────────


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Pluggable embedding backend.

    Any class with `encode(text: str) -> list[float]` and a `dim` property
    satisfies this protocol — useful for tests with lightweight fakes.
    """

    def encode(self, text: str) -> List[float]: ...

    @property
    def dim(self) -> int: ...


# ── Hashing (default fallback) ─────────────────────────────────────


class HashingEmbeddingProvider:
    """Deterministic SHA-256 word-hash embedding.

    Same algorithm as the original `simple_text_hash`: tokenize, hash each word
    into `dim` buckets, L2-normalize. Always available, no dependencies.

    Properties:
    - Deterministic across processes (SHA-256, not Python's randomized hash())
    - Lightweight (no model download)
    - Quality: bag-of-words — "并发" won't match "async/await"
    - Use case: offline fallback, tests, small corpora
    """

    def __init__(self, dim: int = 128):
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def encode(self, text: str) -> List[float]:
        """Return a unit-norm embedding for `text`."""
        words = re.findall(r"[\w]+", text.lower())
        if not words:
            return [0.0] * self._dim

        buckets = np.zeros(self._dim, dtype=np.float32)
        for word in words:
            h1 = self._word_bucket(word, self._dim)
            h2 = self._word_bucket(word[::-1], self._dim)
            buckets[h1] += 1.0
            buckets[h2] += 0.5

        norm = float(np.linalg.norm(buckets))
        if norm > 0:
            buckets = buckets / norm
        return buckets.tolist()

    @staticmethod
    def _word_bucket(word: str, dim: int) -> int:
        """SHA-256 → uniform bucket index. Stable across runs."""
        digest = hashlib.sha256(word.encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big") % dim


# ── Sentence-Transformers (production) ─────────────────────────────


class SentenceTransformerProvider:
    """Real semantic embeddings via sentence-transformers.

    Uses `all-MiniLM-L6-v2` by default (384-dim, ~90MB, fast on CPU).
    Semantically related phrases (e.g. "并发" ↔ "async/await") end up close
    in vector space — unlike the hashing provider.

    Raises:
        ImportError: if `sentence-transformers` is not installed.
        RuntimeError: if model download/loading fails (e.g. no network).
    """

    DEFAULT_MODEL = "all-MiniLM-L6-v2"

    def __init__(self, model_name: str = DEFAULT_MODEL):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise ImportError(
                "sentence-transformers is not installed. "
                "Install it via `pip install sentence-transformers` "
                "or use HashingEmbeddingProvider as a fallback."
            ) from e

        try:
            self._model = SentenceTransformer(model_name)
        except Exception as e:
            raise RuntimeError(
                f"Failed to load sentence-transformers model {model_name!r}: {e}"
            ) from e

        self._dim = int(self._model.get_sentence_embedding_dimension())

    @property
    def dim(self) -> int:
        return self._dim

    def encode(self, text: str) -> List[float]:
        """Embed `text` into a 384-dim vector (or whatever the model outputs)."""
        vec = self._model.encode(text, convert_to_numpy=True)
        return vec.astype(np.float32).tolist()


# ── TF-IDF (alternative) ───────────────────────────────────────────


class TfidfEmbeddingProvider:
    """TF-IDF embedding, fitted incrementally on the corpus.

    Distinct from `HashingEmbeddingProvider` in that it learns a vocabulary
    from the documents it sees, and uses inverse-document-frequency weighting.
    Better quality on a fixed corpus, but requires refitting when vocabulary
    drifts significantly.

    For dynamic corpora, prefer SentenceTransformer. This provider is most
    useful for: a) unit tests, b) static document collections where you can
    fit once and query many times.
    """

    def __init__(self, max_features: int = 384):
        self._max_features = max_features
        self._vectorizer = None
        self._dim = max_features
        self._fitted_corpus: List[str] = []

    @property
    def dim(self) -> int:
        return self._dim

    def _ensure_fitted(self):
        if self._vectorizer is not None:
            return
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
        except ImportError as e:
            raise ImportError(
                "scikit-learn is required for TfidfEmbeddingProvider. "
                "Install it via `pip install scikit-learn`."
            ) from e
        self._vectorizer = TfidfVectorizer(max_features=self._max_features)

    def encode(self, text: str) -> List[float]:
        """Encode text. Lazily fits on first call with a single document.

        For a meaningful fit, callers should call `fit(corpus)` first.
        """
        self._ensure_fitted()
        if not self._fitted_corpus:
            # Cold-start: fit on this single doc. Quality is poor but consistent.
            self._vectorizer.fit([text])
            self._fitted_corpus.append(text)
        vec = self._vectorizer.transform([text]).toarray()[0]
        # Pad/truncate to dim for stable dimensionality
        if vec.size < self._dim:
            padded = np.zeros(self._dim, dtype=np.float32)
            padded[: vec.size] = vec
            vec = padded
        else:
            vec = vec[: self._dim]
        return vec.astype(np.float32).tolist()

    def fit(self, corpus: List[str]) -> None:
        """Pre-fit the vectorizer on a known corpus."""
        self._ensure_fitted()
        if not corpus:
            return
        self._vectorizer.fit(corpus)
        self._fitted_corpus = list(corpus)


# ── Factory ────────────────────────────────────────────────────────


def get_default_provider(name: str = "auto", **kwargs) -> EmbeddingProvider:
    """Resolve an embedding provider by name.

    Args:
        name: "auto" | "sentence-transformers" | "tfidf" | "hashing"
        **kwargs: forwarded to the provider constructor (e.g. `dim=384`).

    Returns:
        A configured `EmbeddingProvider`.

    Falls back gracefully:
    - "auto" → SentenceTransformer (if installed) else Hashing
    - "sentence-transformers" → raises ImportError if missing (caller decides)
    - "tfidf" → TfidfEmbeddingProvider
    - "hashing" → HashingEmbeddingProvider

    P12-4: when "auto" is selected and sentence-transformers is not installed,
    emit an explicit info log so users know they're getting a degraded
    semantic-search experience (hash-based, not real embeddings).
    """
    name = (name or "auto").lower()
    if name == "auto":
        if _sentence_transformers_available():
            try:
                return SentenceTransformerProvider(
                    model_name=kwargs.get("model_name", SentenceTransformerProvider.DEFAULT_MODEL)
                )
            except Exception as e:
                logger.warning("sentence-transformers load failed (%s); falling back to hashing", e)
        else:
            # P12-4: explicit downgrade notice — no longer silent. Helps
            # users understand why their "并发" search won't find "async/await".
            logger.info(
                "sentence-transformers is not installed; using HashingEmbeddingProvider fallback. "
                "Install with `pip install -e .[semantic]` (~90MB model download) "
                "for true semantic search."
            )
        return HashingEmbeddingProvider(dim=kwargs.get("dim", 128))

    if name in ("sentence-transformers", "st", "minilm"):
        return SentenceTransformerProvider(
            model_name=kwargs.get("model_name", SentenceTransformerProvider.DEFAULT_MODEL)
        )
    if name in ("tfidf", "tf-idf"):
        return TfidfEmbeddingProvider(max_features=kwargs.get("dim", 384))
    if name in ("hashing", "hash"):
        return HashingEmbeddingProvider(dim=kwargs.get("dim", 128))

    raise ValueError(
        f"Unknown embedding provider: {name!r}. "
        f"Expected one of: auto, sentence-transformers, tfidf, hashing."
    )


def _sentence_transformers_available() -> bool:
    """True if sentence-transformers is importable."""
    try:
        import sentence_transformers  # noqa: F401

        return True
    except ImportError:
        return False


# ── Config hook ────────────────────────────────────────────────────


def get_provider_from_env() -> EmbeddingProvider:
    """Build a provider from `EMBEDDING_PROVIDER` env var (defaults to 'auto')."""
    name = os.getenv("EMBEDDING_PROVIDER", "auto")
    dim = int(os.getenv("EMBEDDING_DIM", "128"))
    model_name = os.getenv("EMBEDDING_MODEL", SentenceTransformerProvider.DEFAULT_MODEL)
    return get_default_provider(name=name, dim=dim, model_name=model_name)
