import json
from pathlib import Path

import pytest

from cortex.context.manager import ContextManager
from cortex.llm.capabilities import ContextMode
from cortex.llm.protocol import LLMResponse
from cortex.runtime.checkpoint import Checkpoint
from cortex.runtime.loop import AgentLoop
from cortex.runtime.state import AgentState
from cortex.runtime.action import Action
from cortex.skills import BM25SkillIndex, DenseSkillIndex, EmbeddingCache, HybridSkillIndex, SkillRegistry, SkillSnapshotCorruptError
from eval.skill_retrieval import run
from cortex.tools.executor import ToolExecutor
from cortex.tools.registry import ToolRegistry
from cortex.tools.executor import ToolResult


def write_skill(root: Path, folder="skill", *, name="Skill", description="one line", body="BODY", resource="RESOURCE", disabled=False):
    package = root / folder; (package / "references").mkdir(parents=True)
    flag = "\ndisable-model-invocation: true" if disabled else ""
    (package / "SKILL.md").write_text(f"---\nname: {name}\ndescription: >-\n  {description}{flag}\n---\n{body}", encoding="utf-8")
    (package / "references" / "data.txt").write_text(resource, encoding="utf-8")
    return package


def registry(tmp_path, root=None, **kwargs):
    value = SkillRegistry([root or tmp_path / "skills"], snapshot_root=tmp_path / "snapshots", **kwargs); value.scan(); return value


def test_full_package_snapshot_changes_and_survives_source_edit(tmp_path):
    source = tmp_path / "skills"; package = write_skill(source)
    first = registry(tmp_path, source).packages()[0]
    (package / "references" / "data.txt").write_text("NEW", encoding="utf-8")
    second_registry = registry(tmp_path, source); second = second_registry.packages()[0]
    assert first.content_hash != second.content_hash
    assert second_registry.read_resource(first.skill_id, first.content_hash, "references/data.txt", 100) == "RESOURCE"
    assert second_registry.read_resource(second.skill_id, second.content_hash, "references/data.txt", 100) == "NEW"
    restarted = SkillRegistry([], snapshot_root=tmp_path / "snapshots")
    assert restarted.get(first.skill_id, first.content_hash).body == "BODY"


def test_snapshot_corruption_is_explicit(tmp_path):
    write_skill(tmp_path / "skills"); item = registry(tmp_path).packages()[0]
    (item.root / "SKILL.md").write_text("corrupt", encoding="utf-8")
    restarted = SkillRegistry([], snapshot_root=tmp_path / "snapshots")
    with pytest.raises(SkillSnapshotCorruptError): restarted.get(item.skill_id, item.content_hash)


def test_bad_package_and_id_name_conflicts_are_diagnostics(tmp_path):
    root1, root2 = tmp_path / "one" / "shared", tmp_path / "two" / "shared"
    write_skill(root1, name="Same"); write_skill(root2, name="Same")
    bad = root1 / "bad"; bad.mkdir(); (bad / "SKILL.md").write_text("---\nname: [bad\n---\nx")
    value = SkillRegistry([root1, root2], snapshot_root=tmp_path / "snapshots"); assert value.scan() == []
    assert {item.code for item in value.diagnostics} >= {"invalid_package", "duplicate_id", "duplicate_name"}


def test_prebuilt_bm25_rebuilds_only_for_registry_version(tmp_path):
    root = tmp_path / "skills"; package = write_skill(root, description="alpha")
    value = registry(tmp_path, root); index = BM25SkillIndex(value)
    frequencies = index._frequencies
    assert index.search("alpha") and index._frequencies is frequencies
    (package / "SKILL.md").write_text("---\nname: Skill\ndescription: beta\n---\nBODY")
    value.scan(); assert index.search("beta") and index._frequencies is not frequencies


class FakeEmbeddings:
    identity = "fake:v1"
    def __init__(self, fail=False): self.calls, self.fail = 0, fail
    def embed(self, texts):
        self.calls += 1
        if self.fail: raise RuntimeError("offline")
        return [[float("alpha" in text), float("beta" in text), 1.0] for text in texts]


def test_dense_cache_hybrid_and_explicit_degradation(tmp_path):
    root = tmp_path / "skills"; write_skill(root, "a", name="A", description="alpha"); write_skill(root, "b", name="B", description="beta")
    value = registry(tmp_path, root); backend = FakeEmbeddings(); cache = EmbeddingCache(tmp_path / "cache.json")
    dense = DenseSkillIndex(value, backend, cache); assert dense.search("alpha")[0].name == "A"
    cached = DenseSkillIndex(value, backend, EmbeddingCache(tmp_path / "cache.json")); assert cached.last_status["cache_hits"] == 2
    failing = DenseSkillIndex(value, FakeEmbeddings(True), EmbeddingCache(tmp_path / "other.json")); assert failing.last_status["degraded"]
    hybrid = HybridSkillIndex(BM25SkillIndex(value), failing); assert hybrid.search("alpha")[0].name == "A" and hybrid.last_status["degraded"]


def test_context_disclosure_retry_incremental_and_modes():
    state = AgentState(skill_versions={"a": "1", "b": "2"}, skill_bodies={"a": "AAA", "b": "BBB"}, skill_pending_disclosures={"a@1"})
    manager = ContextManager()
    first, _ = manager.build_context(state, ContextMode.SERVER_MANAGED)
    assert "AAA" in str(first) and "BBB" not in str(first) and state.skill_pending_disclosures == {"a@1"}
    # Building/request failure does not commit disclosure.
    retry, _ = manager.build_context(state, ContextMode.SERVER_MANAGED); assert "AAA" in str(retry)
    state.skill_accepted_disclosures.add("a@1"); state.skill_pending_disclosures.remove("a@1"); state.skill_pending_disclosures.add("b@2")
    second, _ = manager.build_context(state, ContextMode.SERVER_MANAGED)
    assert "BBB" in str(second) and "AAA" not in str(second)
    client, _ = manager.build_context(state, ContextMode.CLIENT_MANAGED)
    assert str(client).count("AAA") == 1 and str(client).count("BBB") == 1


def test_failed_model_request_keeps_pending_disclosure():
    class Flaky:
        def __init__(self): self.calls = 0
        def respond(self, **kwargs):
            self.calls += 1
            if self.calls == 1: raise RuntimeError("provider down")
            return LLMResponse(response_id="response-ok", content="ok")
    loop = AgentLoop(Flaky(), ToolExecutor(set(), ToolRegistry()), context_mode=ContextMode.SERVER_MANAGED)
    state = AgentState(pending_input=[{"role": "user", "content": "x"}], skill_versions={"a": "1"}, skill_bodies={"a": "AAA"}, skill_pending_disclosures={"a@1"})
    with pytest.raises(RuntimeError): loop._request(state)
    assert state.skill_pending_disclosures == {"a@1"} and not state.skill_accepted_disclosures
    loop._request(state)
    assert state.skill_pending_disclosures == set() and state.skill_accepted_disclosures == {"a@1"}


def test_active_body_budget_is_explicit():
    state = AgentState(skill_versions={"a": "1"}, skill_bodies={"a": "12345"})
    with pytest.raises(ValueError, match="active Skill body budget"):
        ContextManager(max_active_skill_chars=4).build_context(state)


def test_resource_omission_binds_selected_version_not_latest():
    class CapturingExecutor:
        allowed_permissions = set()
        def execute(self, name, arguments):
            self.arguments = arguments
            return ToolResult(name, 1, 0, True, None, None, {"kind": "skill_resource", "skill_id": "a", "content_hash": arguments["content_hash"], "path": "references/x", "content": "old", "size_chars": 3})
    executor = CapturingExecutor(); loop = AgentLoop(object(), executor)
    state = AgentState(skill_versions={"a": "old-version"})
    state.pending_actions = [Action(call_id="call-resource", tool_name="skill_read_resource", arguments={"skill_id": "a", "path": "references/x", "content_hash": None})]
    loop.act(state)
    assert executor.arguments["content_hash"] == "old-version"


def test_skills_disabled_skips_discovery(monkeypatch, tmp_path):
    from cortex.app.bootstrap import build_agent
    def forbidden(*args, **kwargs): raise AssertionError("scan must not run")
    monkeypatch.setattr(SkillRegistry, "scan", forbidden)
    assert build_agent(object(), skills_enabled=False, skill_roots=[tmp_path]).skill_registry is None


def test_checkpoint_preserves_search_and_disclosure_state():
    state = AgentState(skill_versions={"a": "1"}, skill_searches=1, skill_search_limit=1, skill_local_queries=2, skill_pending_disclosures={"a@1"}, skill_accepted_disclosures={"b@2"}, pending_input=[{"type": "function_call_output", "call_id": "c", "output": "ok"}])
    restored = Checkpoint.capture(state).restore(new_run_id="new", max_steps=4)
    assert (restored.skill_searches, restored.skill_search_limit, restored.skill_local_queries) == (1, 1, 2)
    assert restored.pending_input[0]["call_id"] == "c" and restored.skill_pending_disclosures == {"a@1"}


def test_yaml_multiline_disabled_and_scan_symlink(tmp_path):
    root = tmp_path / "skills"; package = write_skill(root, description="multi line", disabled=True)
    outside = tmp_path / "outside"; outside.write_text("x"); (package / "references" / "escape").symlink_to(outside)
    value = registry(tmp_path, root)
    assert value.packages() == [] and value.diagnostics[0].code == "invalid_package"
    (package / "references" / "escape").unlink(); value.scan()
    assert value.packages()[0].description == "multi line" and value.packages()[0].disable_model_invocation


def test_offline_retrieval_evaluation_writes_metrics(tmp_path):
    fixture = Path(__file__).parents[1] / "eval" / "skill_retrieval_fixture.json"
    report = run(fixture)
    assert report["revision"] and report["results"]["bm25_metadata"]["recall@5"] == 1
    assert report["results"]["dense"]["available"] is False
