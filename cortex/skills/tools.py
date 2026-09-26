"""Read-only tools for progressive disclosure of skill documents."""

from __future__ import annotations

from pathlib import Path
from pydantic import BaseModel, Field

from cortex.tools.base import Tool, ToolSandboxError
from cortex.tools.permission import Permission
from .registry import SkillRegistry
from .selector import BM25SkillIndex


class SearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=5, ge=1, le=10)


class LoadInput(BaseModel):
    skill_id: str
    content_hash: str | None = None


class ResourceInput(BaseModel):
    skill_id: str
    path: str
    content_hash: str | None = None


class SkillSearchTool(Tool):
    name = "skill_search"
    description = "Search installed Skill metadata. At most one supplemental search is allowed per task."
    input_model = SearchInput
    permission = Permission.SKILL_SEARCH
    retryable = False
    max_retries = 0
    timeout = 3

    def __init__(self, index: BM25SkillIndex): self.index = index
    def execute(self, value: SearchInput):
        return {"kind": "skill_search", "query": value.query, "candidates": [c.as_dict() for c in self.index.search(value.query, value.limit)]}


class SkillLoadTool(Tool):
    name = "skill_load"
    description = "Load the complete core body of a selected Skill. Never executes package code."
    input_model = LoadInput
    permission = Permission.SKILL_LOAD
    retryable = False
    max_retries = 0
    timeout = 3

    def __init__(self, registry: SkillRegistry): self.registry = registry
    def execute(self, value: LoadInput):
        package = self.registry.get(value.skill_id, value.content_hash)
        return {"kind": "skill_load", **package.metadata(), "body": package.body, "size_chars": len(package.body)}


class SkillReadResourceTool(Tool):
    name = "skill_read_resource"
    description = "Read a UTF-8 resource inside a loaded Skill package; paths cannot escape the package."
    input_model = ResourceInput
    permission = Permission.SKILL_READ_RESOURCE
    retryable = False
    max_retries = 0
    timeout = 3

    def __init__(self, registry: SkillRegistry, max_chars: int = 20_000):
        self.registry, self.max_chars = registry, max_chars

    def execute(self, value: ResourceInput):
        package = self.registry.get(value.skill_id, value.content_hash)
        relative = Path(value.path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ToolSandboxError("resource path escapes skill package", self.name, value.path)
        target = (package.root / relative).resolve()
        try:
            target.relative_to(package.root)
        except ValueError as exc:
            raise ToolSandboxError("resource symlink escapes skill package", self.name, value.path) from exc
        if not target.is_file():
            raise FileNotFoundError(value.path)
        content = target.read_text(encoding="utf-8")
        if len(content) > self.max_chars:
            raise ValueError(f"resource exceeds {self.max_chars} character budget")
        return {"kind": "skill_resource", "skill_id": package.skill_id, "content_hash": package.content_hash, "path": relative.as_posix(), "content": content, "size_chars": len(content)}
