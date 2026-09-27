#!/usr/bin/env python3
"""Offline evaluation of Cortex retrievers on generic or SkillRet v1.1 data."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import tempfile
import time
from pathlib import Path
import sys
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cortex.skills import BM25SkillIndex, DenseSkillIndex, HybridSkillIndex, OpenAIEmbeddingBackend, SkillRegistry


Qrels = dict[str, dict[str, float]]
SKILLRET_DATASET_ID = "ThakiCloud/SKILLRET"


def _records(path: Path) -> list[dict]:
    """Read a JSON array/object or newline-delimited JSON file."""
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, list):
        return value
    for key in ("skills", "corpus", "queries", "qrels"):
        if isinstance(value.get(key), list):
            return value[key]
    raise ValueError(f"expected a JSON array in {path}")


def _field(record: dict, names: tuple[str, ...], kind: str) -> str:
    for name in names:
        if record.get(name) is not None:
            return str(record[name])
    raise ValueError(f"{kind} has none of the required fields: {', '.join(names)}")


def _find(root: Path, candidates: tuple[str, ...], kind: str) -> Path:
    for relative in candidates:
        path = root / relative
        if path.is_file():
            return path
    raise FileNotFoundError(f"SkillRet {kind} not found below {root}; tried: {', '.join(candidates)}")


def _load_skillret_qrels(path: Path) -> Qrels:
    qrels: Qrels = {}
    if path.suffix in {".tsv", ".txt"}:
        rows = list(csv.reader(path.read_text(encoding="utf-8-sig").splitlines(), delimiter="\t"))
        if not rows:
            return qrels
        header = [cell.lower().replace("_", "-") for cell in rows[0]]
        has_header = any("query" in cell for cell in header)
        qi = next((i for i, cell in enumerate(header) if cell in {"query-id", "queryid", "qid"}), 0)
        si = next((i for i, cell in enumerate(header) if cell in {"skill-id", "corpus-id", "doc-id", "docid"}), 1)
        ri = next((i for i, cell in enumerate(header) if cell in {"relevance", "score", "rel"}), 2)
        for row in rows[1 if has_header else 0:]:
            if len(row) > max(qi, si):
                qrels.setdefault(str(row[qi]), {})[str(row[si])] = float(row[ri]) if len(row) > ri else 1.0
        return qrels
    value = json.loads(path.read_text(encoding="utf-8")) if path.suffix == ".json" else _records(path)
    if isinstance(value, dict) and "qrels" in value:
        value = value["qrels"]
    if isinstance(value, dict):
        for query_id, labels in value.items():
            if isinstance(labels, dict):
                qrels[str(query_id)] = {str(skill_id): float(score) for skill_id, score in labels.items()}
            else:
                qrels[str(query_id)] = {str(skill_id): 1.0 for skill_id in labels}
        return qrels
    for row in value:
        query_id = _field(row, ("query_id", "query-id", "qid"), "qrel")
        skill_id = _field(row, ("skill_id", "skill-id", "corpus_id", "corpus-id", "doc_id"), "qrel")
        qrels.setdefault(query_id, {})[skill_id] = float(row.get("relevance", row.get("score", 1)))
    return qrels


def _resolve_skillret_root(path: Path, revision: str | None) -> tuple[Path, str]:
    """Resolve the ``skillret`` alias through the standard Hugging Face cache."""
    if path.as_posix().casefold() not in {"skillret", "thakicloud/skillret"}:
        return path, revision or "skillret-v1.1"
    if not revision:
        raise ValueError("--revision is required when --dataset skillret is used; pin the immutable Hugging Face revision")
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise RuntimeError("--dataset skillret requires huggingface_hub; install the repository requirements") from exc
    root = snapshot_download(
        repo_id=SKILLRET_DATASET_ID,
        repo_type="dataset",
        revision=revision,
        allow_patterns=("data/skills/test.jsonl", "data/queries/test.jsonl", "data/qrels/test.jsonl"),
    )
    return Path(root), revision


def load_dataset(path: Path, adapter: str = "generic", revision: str | None = None) -> tuple[str, list[dict], list[dict], Qrels]:
    """Load normalized corpus, queries and graded qrels.

    SkillRet accepts a dataset root and deliberately selects only test queries/qrels.
    The alternate generic single-file format remains useful for smoke tests.
    """
    if adapter == "generic":
        data = json.loads(path.read_text(encoding="utf-8"))
        if not data.get("revision"):
            raise ValueError("dataset revision is required")
        qrels = {str(qid): {str(sid): 1.0 for sid in ids} for qid, ids in data["qrels"].items()}
        return str(data["revision"]), data["corpus"], data["queries"], qrels

    path, resolved_revision = _resolve_skillret_root(path, revision)
    if not path.is_dir():
        raise ValueError("the SkillRet adapter requires the SkillRet v1.1 dataset root directory")
    corpus_path = _find(path, ("data/skills/test.jsonl", "skills/test.jsonl", "test/skills.jsonl", "corpus/test.jsonl", "corpus.jsonl", "skills.jsonl", "corpus.json", "skills.json"), "test corpus")
    query_path = _find(path, ("data/queries/test.jsonl", "queries/test.jsonl", "test/queries.jsonl", "test_queries.jsonl", "queries.test.jsonl", "queries/test.json"), "test queries")
    qrels_path = _find(path, ("data/qrels/test.jsonl", "data/qrels/test.tsv", "qrels/test.tsv", "test/qrels.tsv", "test_qrels.tsv", "qrels.test.tsv", "qrels/test.jsonl", "test/qrels.jsonl", "qrels/test.json"), "test qrels")
    corpus = []
    for item in _records(corpus_path):
        skill_id = _field(item, ("skill_id", "_id", "id"), "skill")
        corpus.append({
            "id": skill_id,
            "name": str(item.get("name", item.get("title", skill_id))),
            "description": str(item.get("description", item.get("summary", item.get("text", "")))),
            "body": str(item.get("body", item.get("skill_md", item.get("content", item.get("text", ""))))),
        })
    queries = []
    for item in _records(query_path):
        queries.append({"id": _field(item, ("query_id", "_id", "id"), "query"), "query": _field(item, ("query", "text", "instruction"), "query")})
    qrels = _load_skillret_qrels(qrels_path)
    query_ids, skill_ids = {q["id"] for q in queries}, {s["id"] for s in corpus}
    if not query_ids.intersection(qrels):
        raise ValueError("SkillRet test qrels do not match any test query IDs")
    if not skill_ids.intersection({sid for labels in qrels.values() for sid in labels}):
        raise ValueError("SkillRet qrels do not match any corpus skill IDs")
    revision_file = path / "revision.txt"
    dataset_revision = revision_file.read_text(encoding="utf-8").strip() if revision_file.is_file() and revision is None else resolved_revision
    return dataset_revision, corpus, queries, qrels


def materialize(corpus: list[dict], destination: Path) -> dict[str, str]:
    """Materialize benchmark rows as Skills and return local directory -> corpus ID."""
    mapping = {}
    for number, document in enumerate(corpus):
        local_id = f"skill-{number:06d}"
        mapping[local_id] = str(document["id"])
        root = destination / local_id
        root.mkdir(parents=True)
        name = json.dumps(str(document.get("name", document["id"])), ensure_ascii=False)
        description = json.dumps(str(document.get("description", document.get("text", ""))).replace("\n", " "), ensure_ascii=False)
        body = str(document.get("body", document.get("text", "")))
        (root / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\n{body}\n", encoding="utf-8")
    return mapping


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


def calculate_metrics(ranked_ids: list[str], relevance: dict[str, float]) -> dict[str, float]:
    """Calculate one query's metrics; non-positive qrels are not relevant."""
    relevant = {skill_id for skill_id, score in relevance.items() if score > 0}
    metrics: dict[str, float] = {}
    for k in (1, 5):
        metrics[f"hit@{k}"] = float(bool(relevant.intersection(ranked_ids[:k])))
    for k in (5, 10):
        found = relevant.intersection(ranked_ids[:k])
        metrics[f"recall@{k}"] = len(found) / len(relevant) if relevant else 0.0
        metrics[f"completeness@{k}"] = float(bool(relevant) and relevant.issubset(ranked_ids[:k]))
        dcg = sum((2 ** relevance.get(sid, 0) - 1) / math.log2(rank + 1) for rank, sid in enumerate(ranked_ids[:k], 1))
        ideal = sorted((score for score in relevance.values() if score > 0), reverse=True)[:k]
        idcg = sum((2 ** score - 1) / math.log2(rank + 1) for rank, score in enumerate(ideal, 1))
        metrics[f"ndcg@{k}"] = dcg / idcg if idcg else 0.0
    relevant_ranks = [rank for rank, sid in enumerate(ranked_ids[:10], 1) if sid in relevant]
    metrics["mrr@10"] = 1 / relevant_ranks[0] if relevant_ranks else 0.0
    precisions = [sum(1 for sid in ranked_ids[:rank] if sid in relevant) / rank for rank in relevant_ranks]
    metrics["map@10"] = sum(precisions) / min(len(relevant), 10) if relevant else 0.0
    return metrics


def evaluate(retriever, queries: list[dict], qrels: Qrels, id_map: dict[str, str], retriever_name: str = "retriever") -> dict:
    per_query, latencies = [], []
    build_status = dict(retriever.last_status)
    for query in queries:
        text = str(query.get("query", query.get("text", "")))
        started = time.perf_counter()
        ranked = retriever.search(text, 10)
        latencies.append((time.perf_counter() - started) * 1000)
        ranked_ids = [id_map.get(item.skill_id, item.skill_id) for item in ranked]
        relevance = qrels.get(str(query["id"]), {})
        per_query.append({
            "query_id": str(query["id"]), "query": text,
            "relevant_skill_ids": sorted(skill_id for skill_id, score in relevance.items() if score > 0),
            "retrieved": [{"skill_id": skill_id, "rank": rank, "score": item.score, "retriever": retriever_name} for rank, (skill_id, item) in enumerate(zip(ranked_ids, ranked), 1)],
            "metrics": calculate_metrics(ranked_ids, relevance),
        })
    names = tuple(per_query[0]["metrics"]) if per_query else ("hit@1", "hit@5", "recall@5", "recall@10", "mrr@10", "completeness@5", "completeness@10", "ndcg@5", "ndcg@10", "map@10")
    aggregate = {name: statistics.mean(item["metrics"][name] for item in per_query) if per_query else 0.0 for name in names}
    index_status = dict(retriever.last_status)
    for key in ("build_ms", "cold_start_ms"):
        if key in build_status:
            index_status[key] = build_status[key]
    return {**aggregate, "query_p50_ms": percentile(latencies, .5), "query_p95_ms": percentile(latencies, .95), "index": index_status, "queries": per_query}


def run(path: Path, adapter: str = "generic", dense_backend=None, revision: str | None = None) -> dict:
    dataset_revision, corpus, queries, qrels = load_dataset(path, adapter, revision)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "skills"
        local_ids = materialize(corpus, root)
        indexes, maps = {}, {}
        for body in (False, True):
            started = time.perf_counter()
            registry = SkillRegistry([root], index_body=body, snapshot_root=Path(directory) / f"snapshots-{body}")
            registry.scan()
            sparse = BM25SkillIndex(registry)
            sparse.last_status["cold_start_ms"] = (time.perf_counter() - started) * 1000
            key = "bm25_body" if body else "bm25_metadata"
            indexes[key] = sparse
            maps[key] = {package.skill_id: local_ids[unquote(package.skill_id.split(":", 1)[1])] for package in registry.packages()}
            if body and dense_backend is not None:
                started = time.perf_counter()
                dense = DenseSkillIndex(registry, dense_backend)
                dense.last_status["cold_start_ms"] = (time.perf_counter() - started) * 1000
                indexes["dense"], maps["dense"] = dense, maps[key]
                started = time.perf_counter()
                hybrid = HybridSkillIndex(sparse, dense)
                hybrid.last_status.update({"build_ms": (time.perf_counter() - started) * 1000, "cold_start_ms": dense.last_status["cold_start_ms"]})
                indexes["hybrid"], maps["hybrid"] = hybrid, maps[key]
        results = {name: evaluate(index, queries, qrels, maps[name], name) for name, index in indexes.items()}
        for missing in ({"dense", "hybrid"} - set(results)):
            results[missing] = {"available": False, "degraded": False, "reason": "embedding backend not configured"}
        return {"dataset": str(path), "adapter": adapter, "split": "test" if adapter == "skillret" else None, "revision": dataset_revision, "corpus_count": len(corpus), "query_count": len(queries), "results": results, "note": "The fixed test split is evaluation-only and must not be used for tuning."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--adapter", choices=("generic", "skillret"), default="generic")
    parser.add_argument("--revision", help="immutable Hugging Face dataset revision (required with --dataset skillret)")
    parser.add_argument("--dense-backend", choices=("none", "openai"), default="none")
    parser.add_argument("--embedding-model", default="text-embedding-3-small")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    backend = OpenAIEmbeddingBackend(model=args.embedding_model) if args.dense_backend == "openai" else None
    report = run(args.dataset, args.adapter, backend, args.revision)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
