import json
import math

import pytest

from eval.skill_retrieval import calculate_metrics, load_dataset, run


def test_multi_skill_metrics_distinguish_hit_recall_and_completeness():
    metrics = calculate_metrics(["a", "x", "z"], {"a": 1, "b": 1})

    assert metrics["hit@1"] == metrics["hit@5"] == 1
    assert metrics["recall@5"] == metrics["recall@10"] == 0.5
    assert metrics["completeness@5"] == metrics["completeness@10"] == 0
    assert metrics["mrr@10"] == 1
    assert metrics["ndcg@5"] == pytest.approx(1 / (1 + 1 / math.log2(3)))
    assert metrics["map@10"] == 0.5


def test_skillret_test_split_schema_and_benchmark(tmp_path):
    (tmp_path / "queries").mkdir()
    (tmp_path / "qrels").mkdir()
    skills = [
        {"skill_id": "python/debug", "name": "Python Debug", "description": "diagnose traceback", "body": "make a reproduction"},
        {"skill_id": "release", "name": "Release", "description": "release rollback", "body": "run tests"},
    ]
    (tmp_path / "skills.jsonl").write_text("\n".join(json.dumps(row) for row in skills), encoding="utf-8")
    (tmp_path / "queries" / "test.jsonl").write_text(
        "\n".join((json.dumps({"query_id": "q1", "query": "diagnose traceback"}), json.dumps({"query_id": "q2", "query": "release then debug traceback"}))),
        encoding="utf-8",
    )
    (tmp_path / "qrels" / "test.tsv").write_text(
        "query_id\tskill_id\trelevance\nq1\tpython/debug\t2\nq2\tpython/debug\t1\nq2\trelease\t1\n",
        encoding="utf-8",
    )
    # A train qrel must never be selected by the adapter.
    (tmp_path / "qrels" / "train.tsv").write_text("query_id\tskill_id\trelevance\nq1\trelease\t1\n", encoding="utf-8")

    revision, corpus, queries, qrels = load_dataset(tmp_path, "skillret")
    assert revision == "skillret-v1.1"
    assert corpus[0]["id"] == "python/debug"
    assert queries[0] == {"id": "q1", "query": "diagnose traceback"}
    assert qrels["q1"] == {"python/debug": 2.0}

    report = run(tmp_path, "skillret")
    result = report["results"]["bm25_metadata"]
    assert report["split"] == "test" and result["hit@1"] > 0
    assert result["queries"][1]["relevant_skill_ids"] == ["python/debug", "release"]
    assert result["queries"][0]["retrieved"][0].keys() == {"skill_id", "rank", "score", "retriever"}
    assert report["results"]["dense"]["available"] is False


def test_generic_fixture_remains_supported():
    from pathlib import Path

    fixture = Path(__file__).parents[1] / "eval" / "skill_retrieval_fixture.json"
    report = run(fixture)
    assert report["results"]["bm25_metadata"]["hit@5"] == 1
    assert report["results"]["bm25_metadata"]["recall@5"] == pytest.approx(5 / 6)
