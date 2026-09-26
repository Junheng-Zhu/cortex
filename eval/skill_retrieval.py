#!/usr/bin/env python3
"""Offline Skill retrieval evaluation for versioned corpus/query/qrels data."""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from pathlib import Path
from urllib.parse import unquote
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cortex.skills import BM25SkillIndex, DenseSkillIndex, HybridSkillIndex, OpenAIEmbeddingBackend, SkillRegistry


def load_dataset(path: Path, adapter: str = "generic") -> tuple[str, list[dict], list[dict], dict[str, set[str]]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    revision = data.get("revision")
    if not revision:
        raise ValueError("dataset revision is required")
    if adapter == "skillret":
        corpus = data.get("skills", data.get("corpus", []))
        queries = data.get("queries", [])
        qrels = {str(query["id"]): set(map(str, query.get("relevant_skill_ids", []))) for query in queries}
    else:
        corpus, queries = data["corpus"], data["queries"]
        qrels = {str(key): set(map(str, value)) for key, value in data["qrels"].items()}
    return str(revision), corpus, queries, qrels


def materialize(corpus: list[dict], destination: Path) -> None:
    for document in corpus:
        root = destination / str(document["id"])
        root.mkdir(parents=True)
        description = str(document.get("description", document.get("text", ""))).replace("\n", " ")
        body = str(document.get("body", document.get("text", "")))
        (root / "SKILL.md").write_text(f"---\nname: {document.get('name', document['id'])}\ndescription: {description}\n---\n{body}\n", encoding="utf-8")


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


def evaluate(retriever, queries: list[dict], qrels: dict[str, set[str]], id_map: dict[str, str]) -> dict:
    recalls, reciprocal, coverage, latencies = [], [], [], []
    for query in queries:
        started = time.perf_counter()
        ranked = retriever.search(str(query.get("query", query.get("text", ""))), 10)
        latencies.append((time.perf_counter() - started) * 1000)
        ranked_ids = [id_map.get(item.skill_id, item.skill_id) for item in ranked]
        relevant = qrels.get(str(query["id"]), set())
        recalls.append(bool(relevant.intersection(ranked_ids[:5])))
        ranks = [ranked_ids.index(item) + 1 for item in relevant if item in ranked_ids[:10]]
        reciprocal.append(1 / min(ranks) if ranks else 0)
        coverage.append(bool(relevant) and relevant.issubset(set(ranked_ids[:5])))
    return {"recall@5": statistics.mean(recalls) if recalls else 0, "mrr@10": statistics.mean(reciprocal) if reciprocal else 0, "multi_skill_complete@5": statistics.mean(coverage) if coverage else 0, "query_p50_ms": percentile(latencies, .5), "query_p95_ms": percentile(latencies, .95), "index": retriever.last_status}


def run(path: Path, adapter: str = "generic", dense_backend=None) -> dict:
    revision, corpus, queries, qrels = load_dataset(path, adapter)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "skills"; materialize(corpus, root)
        indexes, maps = {}, {}
        for body in (False, True):
            registry = SkillRegistry([root], index_body=body, snapshot_root=Path(directory) / "snapshots")
            started = time.perf_counter(); registry.scan(); sparse = BM25SkillIndex(registry)
            sparse.last_status["cold_start_ms"] = (time.perf_counter() - started) * 1000
            key = "bm25_body" if body else "bm25_metadata"
            indexes[key] = sparse
            maps[key] = {package.skill_id: unquote(package.skill_id.split(":", 1)[1]) for package in registry.packages()}
            if body and dense_backend is not None:
                started = time.perf_counter(); dense = DenseSkillIndex(registry, dense_backend)
                dense.last_status["cold_start_ms"] = (time.perf_counter() - started) * 1000
                indexes["dense"] = dense; maps["dense"] = maps[key]
                indexes["hybrid"] = HybridSkillIndex(sparse, dense); maps["hybrid"] = maps[key]
        results = {name: evaluate(index, queries, qrels, maps[name]) for name, index in indexes.items()}
        for missing in ({"dense", "hybrid"} - set(results)):
            results[missing] = {"available": False, "reason": "embedding backend not configured"}
        return {"dataset": str(path), "revision": revision, "query_count": len(queries), "results": results, "note": "The fixed test set is evaluation-only and must not be used for tuning."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--adapter", choices=("generic", "skillret"), default="generic")
    parser.add_argument("--dense-backend", choices=("none", "openai"), default="none")
    parser.add_argument("--embedding-model", default="text-embedding-3-small")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    backend = OpenAIEmbeddingBackend(model=args.embedding_model) if args.dense_backend == "openai" else None
    report = run(args.dataset, args.adapter, backend)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
