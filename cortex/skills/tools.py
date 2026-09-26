"""Read-only tools for progressive disclosure of skill documents."""

from __future__ import annotations

from pydantic import BaseModel, Field

from cortex.tools.base import Tool
from cortex.tools.permission import Permission
from .registry import SkillRegistry
from .selector import SkillRetriever


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

    def __init__(self, index: SkillRetriever): self.index = index
    def execute(self, value: SearchInput):
        candidates = self.index.search(value.query, value.limit)
        return {"kind": "skill_search", "query": value.query, "candidates": [c.as_dict() for c in candidates], "index_version": self.index.index_version, "retrieval": self.index.last_status}


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
        from pathlib import PurePosixPath
        relative = PurePosixPath(value.path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"resource path escapes package: {value.path}")
        if value.content_hash is None:
            current = self.registry.get(value.skill_id)
            if value.path not in current.manifest:
                raise ValueError(f"resource path escapes or is absent from package manifest: {value.path}")
            raise ValueError("content_hash must be bound by the runtime")
        content = self.registry.read_resource(value.skill_id, value.content_hash, value.path, self.max_chars)
        return {"kind": "skill_resource", "skill_id": value.skill_id, "content_hash": value.content_hash, "path": value.path, "content": content, "size_chars": len(content)}
