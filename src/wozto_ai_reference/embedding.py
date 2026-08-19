"""Deterministic local embeddings and an in-memory hybrid search adapter."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable, Sequence

from .adapters import is_authorized
from .domain import Document, Principal, RetrievalHit
from .ports import EmbeddingKind, EmbeddingProvider

_TERM_PATTERN = re.compile(r"[\wçğıöşü]+", re.IGNORECASE)


def terms(value: str) -> tuple[str, ...]:
    return tuple(term.casefold() for term in _TERM_PATTERN.findall(value))


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise ValueError("embedding dimensions must match")
    score = sum(a * b for a, b in zip(left, right, strict=True))
    return max(0.0, min(1.0, score))


class HashEmbeddingProvider:
    """Offline token-hash embedding for plumbing tests, never a quality claim."""

    def __init__(self, *, dimensions: int = 64) -> None:
        if dimensions < 8 or dimensions > 4096:
            raise ValueError("dimensions must be between 8 and 4096")
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def model_id(self) -> str:
        """Gomme-uzayi kimligi. Hash gommesi ANLAMSAL DEGILDIR; kimlikte bunu acikca
        soyluyoruz ki kalicilastirilmis vektorlere bakan biri yaniltilmasin."""
        return f"hash-blake2b-nonsemantic/{self._dimensions}"

    async def embed(
        self,
        texts: Sequence[str],
        *,
        kind: EmbeddingKind = "passage",
    ) -> Sequence[Sequence[float]]:
        # `kind` BILINCLI olarak yok sayilir: token-hash gommesi SIMETRIKTIR, sorgu ile
        # belge arasinda fark gozetmez. Imza yine de tasinir ki asimetrik adaptorlerle
        # (e5 vb.) ayni port arkasinda degistirilebilir kalsin.
        del kind
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> tuple[float, ...]:
        vector = [0.0] * self._dimensions
        for token in terms(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % self._dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[bucket] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            return tuple(vector)
        return tuple(value / norm for value in vector)


class InMemoryHybridSearchProvider:
    """Deterministic vector+lexical adapter for evaluation without infrastructure."""

    def __init__(
        self,
        documents: Iterable[Document],
        *,
        embeddings: EmbeddingProvider,
        vector_weight: float = 0.7,
    ) -> None:
        if not 0.0 <= vector_weight <= 1.0:
            raise ValueError("vector_weight must be between 0 and 1")
        self._documents = tuple(documents)
        self._embeddings = embeddings
        self._vector_weight = vector_weight

    async def search(self, *, principal: Principal, query: str, limit: int) -> Sequence[RetrievalHit]:
        authorized = [document for document in self._documents if is_authorized(principal, document)]
        if not authorized or not terms(query):
            return []
        # ⚠️ Sorgu ve belgeler AYRI cagrilarda gomulur. Eskiden tek listede
        # birlestiriliyordu; asimetrik bir model takildiginda (e5: "query: " /
        # "passage: ") bu SESSIZCE yanlis prefix uygulardi.
        query_vector = (await self._embeddings.embed([query], kind="query"))[0]
        document_vectors = await self._embeddings.embed(
            [document.content for document in authorized], kind="passage"
        )
        query_terms = set(terms(query))
        hits: list[RetrievalHit] = []
        for document, vector in zip(authorized, document_vectors, strict=True):
            document_terms = set(terms(f"{document.title} {document.section} {document.content}"))
            lexical = len(query_terms.intersection(document_terms)) / len(query_terms)
            vector_score = cosine_similarity(query_vector, vector)
            score = self._vector_weight * vector_score + (1.0 - self._vector_weight) * lexical
            if score > 0:
                hits.append(RetrievalHit(document=document, score=score))
        hits.sort(key=lambda hit: (-hit.score, hit.document.document_id, hit.document.version))
        return hits[:limit]
