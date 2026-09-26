"""Prebuilt sparse/dense Skill retrieval and reciprocal-rank fusion."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

from .registry import SkillPackage, SkillRegistry


def tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]", text.casefold())


@dataclass(frozen=True)
class SkillCandidate:
    skill_id: str
    name: str
    description: str
    source: str
    content_hash: str
    score: float
    retriever: str = "bm25"

    def as_dict(self) -> dict:
        keys = ("skill_id", "name", "description", "source", "content_hash", "retriever")
        return {**{key: getattr(self, key) for key in keys}, "score": round(self.score, 6)}


class SkillRetriever(Protocol):
    index_version: str
    last_status: dict

    def search(self, query: str, limit: int = 5) -> list[SkillCandidate]: ...


class BM25SkillIndex:
    """BM25 whose document statistics are constructed once per registry version."""

    def __init__(self, registry: SkillRegistry):
        self.registry = registry
        self.last_status: dict = {}
        self._registry_version = ""
        self._packages: list[SkillPackage] = []
        self._frequencies: list[Counter[str]] = []
        self._document_frequency: Counter[str] = Counter()
        self._lengths: list[int] = []
        self._average_length = 1.0
        self.index_version = ""
        self.build()

    def build(self) -> None:
        started = time.perf_counter()
        self._packages = self.registry.packages()
        documents = [tokenize(" ".join((p.name, p.description, p.body if self.registry.index_body else ""))) for p in self._packages]
        self._frequencies = [Counter(document) for document in documents]
        self._document_frequency = Counter(term for frequency in self._frequencies for term in frequency)
        self._lengths = [len(document) for document in documents]
        self._average_length = sum(self._lengths) / len(self._lengths) if self._lengths else 1.0
        config = f"bm25-v2:body={self.registry.index_body}:{self.registry.index_version}"
        self.index_version = hashlib.sha256(config.encode()).hexdigest()
        self._registry_version = self.registry.index_version
        self.last_status = {"backend": "bm25", "index_version": self.index_version, "build_ms": (time.perf_counter() - started) * 1000, "cache": "prebuilt"}

    def _ensure_current(self) -> None:
        if self._registry_version != self.registry.index_version:
            self.build()

    def search(self, query: str, limit: int = 5) -> list[SkillCandidate]:
        self._ensure_current()
        started = time.perf_counter()
        terms = tokenize(query)
        scored: list[SkillCandidate] = []
        count = len(self._packages)
        for package, frequency, length in zip(self._packages, self._frequencies, self._lengths):
            score = 0.0
            for term in terms:
                tf = frequency.get(term, 0)
                if not tf:
                    continue
                df = self._document_frequency[term]
                inverse = math.log(1 + (count - df + 0.5) / (df + 0.5))
                score += inverse * tf * 2.5 / (tf + 1.5 * (0.25 + 0.75 * length / self._average_length))
            if score:
                scored.append(SkillCandidate(**package.metadata(), score=score))
        result = sorted(scored, key=lambda item: (-item.score, item.skill_id))[: max(0, limit)]
        self.last_status = {**self.last_status, "query_ms": (time.perf_counter() - started) * 1000, "degraded": False}
        return result


class EmbeddingBackend(Protocol):
    @property
    def identity(self) -> str: ...
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class OpenAIEmbeddingBackend:
    """Configurable real embedding backend; instantiated only when requested."""

    def __init__(self, model: str = "text-embedding-3-small", client=None, **client_options):
        if client is None:
            from openai import OpenAI
            client = OpenAI(**client_options)
        self.client, self.model = client, model

    @property
    def identity(self) -> str:
        return f"openai:{self.model}"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        response = self.client.embeddings.create(model=self.model, input=list(texts))
        return [list(item.embedding) for item in response.data]


class EmbeddingCache:
    def __init__(self, path: str | Path = ".cortex/skill-embeddings.json"):
        self.path = Path(path)
        try:
            self.values = json.loads(self.path.read_text(encoding="utf-8")) if self.path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            self.values = {}

    def key(self, package: SkillPackage, backend: EmbeddingBackend, config: str) -> str:
        return f"{package.content_hash}:{backend.identity}:{config}"

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".embeddings-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(self.values, stream, separators=(",", ":"))
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


class DenseSkillIndex:
    def __init__(self, registry: SkillRegistry, backend: EmbeddingBackend, cache: EmbeddingCache | None = None):
        self.registry, self.backend = registry, backend
        self.cache = cache or EmbeddingCache()
        self.last_status: dict = {}
        self.index_version = ""
        self._registry_version = ""
        self._packages: list[SkillPackage] = []
        self._vectors: list[list[float]] = []
        self.build()

    @property
    def config(self) -> str:
        return f"dense-v1:body={self.registry.index_body}"

    def build(self) -> None:
        started = time.perf_counter()
        self._packages = self.registry.packages()
        vectors: list[list[float] | None] = []
        misses: list[int] = []
        for index, package in enumerate(self._packages):
            cached = self.cache.values.get(self.cache.key(package, self.backend, self.config))
            vectors.append(cached)
            if cached is None:
                misses.append(index)
        try:
            if misses:
                texts = [self._text(self._packages[index]) for index in misses]
                embedded = self.backend.embed(texts)
                if len(embedded) != len(misses):
                    raise ValueError("embedding backend returned the wrong vector count")
                for index, vector in zip(misses, embedded):
                    vectors[index] = vector
                    self.cache.values[self.cache.key(self._packages[index], self.backend, self.config)] = vector
                self.cache.save()
            self._vectors = [vector or [] for vector in vectors]
            degraded, reason = False, None
        except Exception as exc:
            self._vectors = []
            degraded, reason = True, f"{type(exc).__name__}: {exc}"
        identity = f"{self.config}:{self.backend.identity}:{self.registry.index_version}"
        self.index_version = hashlib.sha256(identity.encode()).hexdigest()
        self._registry_version = self.registry.index_version
        self.last_status = {"backend": "dense", "model": self.backend.identity, "index_version": self.index_version, "build_ms": (time.perf_counter() - started) * 1000, "cache_hits": len(self._packages) - len(misses), "cache_misses": len(misses), "degraded": degraded, "reason": reason}

    def _text(self, package: SkillPackage) -> str:
        return " ".join((package.name, package.description, package.body if self.registry.index_body else ""))

    def search(self, query: str, limit: int = 5) -> list[SkillCandidate]:
        if self._registry_version != self.registry.index_version:
            self.build()
        started = time.perf_counter()
        if not self._vectors:
            return []
        try:
            query_vector = self.backend.embed([query])[0]
            scores = [(self._cosine(query_vector, vector), package) for package, vector in zip(self._packages, self._vectors)]
            result = [SkillCandidate(**package.metadata(), score=score, retriever="dense") for score, package in sorted(scores, key=lambda item: (-item[0], item[1].skill_id)) if score > 0][:limit]
            self.last_status = {**self.last_status, "query_ms": (time.perf_counter() - started) * 1000, "degraded": False, "reason": None}
            return result
        except Exception as exc:
            self.last_status = {**self.last_status, "query_ms": (time.perf_counter() - started) * 1000, "degraded": True, "reason": f"{type(exc).__name__}: {exc}"}
            return []

    @staticmethod
    def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
        if len(left) != len(right) or not left:
            return 0.0
        denominator = math.sqrt(sum(x * x for x in left)) * math.sqrt(sum(x * x for x in right))
        return sum(x * y for x, y in zip(left, right)) / denominator if denominator else 0.0


class HybridSkillIndex:
    def __init__(self, sparse: SkillRetriever, dense: SkillRetriever, rrf_k: int = 60):
        self.sparse, self.dense, self.rrf_k = sparse, dense, rrf_k
        self.index_version = hashlib.sha256(f"hybrid:{sparse.index_version}:{dense.index_version}:{rrf_k}".encode()).hexdigest()
        self.last_status: dict = {}

    def search(self, query: str, limit: int = 5) -> list[SkillCandidate]:
        sparse = self.sparse.search(query, max(limit * 2, 10))
        dense = self.dense.search(query, max(limit * 2, 10))
        scores: dict[str, float] = {}
        candidates = {item.skill_id: item for item in [*sparse, *dense]}
        for ranking in (sparse, dense):
            for rank, item in enumerate(ranking, 1):
                scores[item.skill_id] = scores.get(item.skill_id, 0) + 1 / (self.rrf_k + rank)
        result = [SkillCandidate(**{**candidates[key].as_dict(), "score": score, "retriever": "hybrid"}) for key, score in scores.items()]
        self.last_status = {"backend": "hybrid", "index_version": self.index_version, "degraded": bool(self.dense.last_status.get("degraded")), "reason": self.dense.last_status.get("reason"), "sparse": self.sparse.last_status, "dense": self.dense.last_status}
        return sorted(result, key=lambda item: (-item.score, item.skill_id))[:limit]


def select_skills(names: list[str], requested: list[str]) -> list[str]:
    available = set(names)
    return [name for name in requested if name in available]
