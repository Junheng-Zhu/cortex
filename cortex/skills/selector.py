"""Small deterministic BM25 retriever used by Skill System V1."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .registry import SkillPackage, SkillRegistry


def _tokens(text: str) -> list[str]:
    # Word runs plus individual CJK characters keeps the dependency footprint zero.
    return re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]", text.casefold())


@dataclass(frozen=True)
class SkillCandidate:
    skill_id: str
    name: str
    description: str
    source: str
    content_hash: str
    score: float

    def as_dict(self) -> dict:
        return {**{k: getattr(self, k) for k in ("skill_id", "name", "description", "source", "content_hash")}, "score": round(self.score, 4)}


class BM25SkillIndex:
    def __init__(self, registry: SkillRegistry):
        self.registry = registry

    def search(self, query: str, limit: int = 5) -> list[SkillCandidate]:
        packages = self.registry.packages()
        docs = [_tokens(" ".join((p.name, p.description, p.body if self.registry.index_body else ""))) for p in packages]
        if not packages or not _tokens(query):
            return []
        average = sum(map(len, docs)) / len(docs) or 1
        query_tokens = _tokens(query)
        scored = []
        for package, doc in zip(packages, docs):
            score = 0.0
            for term in query_tokens:
                frequency = doc.count(term)
                if not frequency:
                    continue
                containing = sum(term in other for other in docs)
                inverse = math.log(1 + (len(docs) - containing + .5) / (containing + .5))
                score += inverse * frequency * 2.5 / (frequency + 1.5 * (.25 + .75 * len(doc) / average))
            if score:
                scored.append(SkillCandidate(**package.metadata(), score=score))
        return sorted(scored, key=lambda item: (-item.score, item.skill_id))[: max(0, limit)]


def select_skills(names: list[str], requested: list[str]) -> list[str]:
    """Compatibility helper retained for callers of the pre-V1 stub."""
    available = set(names)
    return [name for name in requested if name in available]
