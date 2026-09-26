"""Discovery and versioned storage for document-only skill packages."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path


class SkillError(RuntimeError):
    pass


class SkillVersionMissingError(SkillError):
    pass


@dataclass(frozen=True)
class SkillPackage:
    skill_id: str
    name: str
    description: str
    body: str
    source: str
    root: Path
    content_hash: str

    def metadata(self) -> dict[str, str]:
        return {
            "skill_id": self.skill_id,
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class SkillDiagnostic:
    code: str
    message: str
    paths: tuple[str, ...] = ()


def _slug(value: str) -> str:
    value = re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip("-")
    return value or "skill"


def _parse_document(text: str, path: Path) -> tuple[str, str, str]:
    """Parse deliberately small YAML front matter without executing anything."""
    if not text.startswith("---\n"):
        raise SkillError(f"{path}: SKILL.md must start with YAML front matter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise SkillError(f"{path}: unterminated front matter")
    values: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise SkillError(f"{path}: invalid front matter line: {line}")
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip().strip('"\'')
    name, description = values.get("name", ""), values.get("description", "")
    if not name or not description:
        raise SkillError(f"{path}: name and description are required")
    return name, description, text[end + 5 :].strip()


@dataclass
class SkillRegistry:
    """Immutable-document registry; scanning never imports or runs package code."""

    roots: list[Path] = field(default_factory=list)
    index_body: bool = False
    _skills: dict[str, SkillPackage] = field(default_factory=dict, init=False)
    _versions: dict[tuple[str, str], SkillPackage] = field(default_factory=dict, init=False)
    diagnostics: list[SkillDiagnostic] = field(default_factory=list, init=False)

    def scan(self) -> list[SkillPackage]:
        found: list[SkillPackage] = []
        self.diagnostics.clear()
        for root_value in self.roots:
            root = Path(root_value).expanduser().resolve()
            if not root.exists():
                continue
            for document in sorted(root.glob("**/SKILL.md")):
                text = document.read_text(encoding="utf-8")
                name, description, body = _parse_document(text, document)
                relative = document.parent.relative_to(root).as_posix()
                source = f"{root.as_posix()}::{relative}"
                skill_id = f"{_slug(root.name)}:{_slug(relative)}"
                digest = hashlib.sha256(text.encode()).hexdigest()
                found.append(SkillPackage(skill_id, name, description, body, source, document.parent.resolve(), digest))
        by_name: dict[str, list[SkillPackage]] = {}
        for package in found:
            by_name.setdefault(package.name.casefold(), []).append(package)
        for name, packages in by_name.items():
            if len(packages) > 1:
                self.diagnostics.append(SkillDiagnostic("duplicate_name", f"skill name {name!r} has {len(packages)} sources; use skill_id", tuple(p.source for p in packages)))
        self._skills = {package.skill_id: package for package in found}
        for package in found:
            self._versions[(package.skill_id, package.content_hash)] = package
        return found

    def get(self, identifier: str, content_hash: str | None = None) -> SkillPackage:
        if content_hash is not None:
            package = self._versions.get((identifier, content_hash))
            if package is None:
                raise SkillVersionMissingError(f"skill version unavailable: {identifier}@{content_hash}")
            return package
        if identifier in self._skills:
            return self._skills[identifier]
        matches = [p for p in self._skills.values() if p.name.casefold() == identifier.casefold()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise SkillError(f"ambiguous skill name {identifier!r}; use skill_id")
        raise SkillError(f"skill not found: {identifier}")

    def names(self) -> list[str]:
        return list(self._skills)

    def packages(self) -> list[SkillPackage]:
        return list(self._skills.values())
