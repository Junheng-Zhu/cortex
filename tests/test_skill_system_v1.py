from pathlib import Path

import pytest

from cortex.context.manager import ContextManager
from cortex.llm.capabilities import ContextMode
from cortex.llm.protocol import LLMResponse, ToolCall
from cortex.runtime.checkpoint import Checkpoint
from cortex.runtime.loop import AgentLoop
from cortex.runtime.state import AgentState
from cortex.skills import BM25SkillIndex, SkillRegistry, SkillVersionMissingError
from cortex.skills.tools import SkillLoadTool, SkillReadResourceTool, SkillSearchTool
from cortex.tools.executor import ToolExecutor
from cortex.tools.permission import Permission
from cortex.tools.registry import ToolRegistry


def package(root: Path, folder: str, name: str, description: str, body: str = "CORE") -> Path:
    target = root / folder
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\n{body}\n", encoding="utf-8")
    return target


def runtime(root: Path, llm, **kwargs):
    skills = SkillRegistry([root]); skills.scan(); index = BM25SkillIndex(skills)
    tools = ToolRegistry()
    tools.register(SkillSearchTool(index)); tools.register(SkillLoadTool(skills)); tools.register(SkillReadResourceTool(skills))
    executor = ToolExecutor(set(Permission), tools)
    return AgentLoop(llm, executor, skill_registry=skills, skill_index=index, **kwargs)


class AutoLLM:
    capabilities = type("Capabilities", (), {"context_mode": ContextMode.CLIENT_MANAGED})()

    def __init__(self, select=True): self.requests, self.select = [], select
    def respond(self, **request):
        self.requests.append(request)
        if len(self.requests) == 1 and self.select:
            candidate = next(item for item in request["input"] if item.get("name") == "cortex_skill_candidates")
            skill_id = candidate["content"].split("- ", 1)[1].split(": ", 1)[0]
            return LLMResponse(tool_calls=[ToolCall("load-1", "skill_load", {"skill_id": skill_id, "content_hash": None})])
        return LLMResponse(content="done")


def test_explicit_load_before_first_request(tmp_path):
    package(tmp_path, "debug", "Debug", "traceback diagnosis", "EXACT CORE")
    llm = AutoLLM(select=False)
    agent = runtime(tmp_path, llm, explicit_skills=["Debug"])
    assert agent.run("unrelated") == "done"
    assert "EXACT CORE" in str(llm.requests[0]["input"])


def test_automatic_selection_and_no_selection_protocol(tmp_path):
    package(tmp_path, "debug", "Debug", "traceback python failure", "SECRET CORE")
    llm = AutoLLM()
    agent = runtime(tmp_path, llm)
    assert agent.run("python traceback") == "done"
    assert "SECRET CORE" not in str(llm.requests[0]["input"])
    assert "SECRET CORE" in str(llm.requests[1]["input"])
    assert list(agent.last_state.skill_versions) == [agent.skill_registry.names()[0]]

    no_skill = AutoLLM(select=False)
    assert runtime(tmp_path, no_skill).run("python traceback") == "done"
    assert len(no_skill.requests) == 1


def test_bm25_similar_candidates_and_body_index_option(tmp_path):
    package(tmp_path, "a", "Cooking", "make pasta", "zebraword")
    package(tmp_path, "b", "Python", "debug traceback")
    metadata = SkillRegistry([tmp_path]); metadata.scan()
    assert BM25SkillIndex(metadata).search("traceback")[0].name == "Python"
    assert BM25SkillIndex(metadata).search("zebraword") == []
    body = SkillRegistry([tmp_path], index_body=True); body.scan()
    assert BM25SkillIndex(body).search("zebraword")[0].name == "Cooking"


def test_duplicate_load_does_not_double_count(tmp_path):
    package(tmp_path, "a", "A", "alpha")
    agent = runtime(tmp_path, AutoLLM(select=False))
    skill_id = agent.skill_registry.names()[0]
    data = SkillLoadTool(agent.skill_registry).execute(type("V", (), {"skill_id": skill_id, "content_hash": None})())
    state = AgentState()
    agent._apply_skill_result(state, "skill_load", data)
    agent._apply_skill_result(state, "skill_load", data)
    selections = [e for e in agent.recorder.events if e.event_type == "skill_selection"]
    assert selections[-1].data["duplicate"] is True
    assert selections[-1].data["loaded_chars"] == 0


def test_supplemental_search_budget_exhaustion(tmp_path):
    package(tmp_path, "a", "A", "alpha")
    agent = runtime(tmp_path, AutoLLM(select=False))
    state = AgentState(skill_searches=1, skill_search_limit=1)
    from cortex.runtime.action import Action
    state.pending_actions = [Action(call_id="x", tool_name="skill_search", arguments={"query": "alpha", "limit": 5})]
    agent.act(state)
    assert state.pending_actions[0].status == "FAILED"
    assert "budget exhausted" in str(state.last_tool_result)


def test_resource_path_and_symlink_escape(tmp_path):
    root = tmp_path / "root"; target = package(root, "a", "A", "alpha")
    (tmp_path / "secret").write_text("no")
    (target / "escape").symlink_to(tmp_path / "secret")
    skills = SkillRegistry([root]); skills.scan(); skill_id = skills.names()[0]
    tool = SkillReadResourceTool(skills)
    for path in ("../secret", "escape"):
        with pytest.raises(Exception, match="escapes"):
            tool.execute(type("V", (), {"skill_id": skill_id, "content_hash": None, "path": path})())


def test_compaction_reinserts_whole_core_and_checkpoint_version(tmp_path):
    package(tmp_path, "a", "A", "alpha", "INDIVISIBLE CORE")
    skills = SkillRegistry([tmp_path]); skills.scan(); item = skills.packages()[0]
    state = AgentState(context_history=[{"role": "user", "content": "x" * 500}], skill_versions={item.skill_id: item.content_hash}, skill_bodies={item.skill_id: item.body}, skill_disclosed={item.skill_id})
    context, _ = ContextManager(max_chars=1000).build_context(state, ContextMode.CLIENT_MANAGED)
    assert "INDIVISIBLE CORE" in str(context)
    checkpoint = Checkpoint.capture(state)
    restored = checkpoint.restore(new_run_id="r", max_steps=3)
    assert restored.skill_versions == state.skill_versions
    missing = SkillRegistry([tmp_path]); missing.scan(); missing._versions.clear()
    with pytest.raises(SkillVersionMissingError):
        missing.get(item.skill_id, item.content_hash)
