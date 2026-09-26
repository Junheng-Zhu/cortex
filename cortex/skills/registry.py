"""Safe discovery and persistent, content-addressed Skill package snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

import yaml


class SkillError(RuntimeError):
    pass


class SkillConflictError(SkillError):
    pass


class SkillVersionMissingError(SkillError):
    pass


class SkillSnapshotCorruptError(SkillError):
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
    manifest: dict[str, str] = field(default_factory=dict)
    disable_model_invocation: bool = False

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


def _parse_document(text: str, path: Path) -> tuple[str, str, str, bool]:
    if not text.startswith("---"):
        raise SkillError(f"{path}: SKILL.md must start with YAML front matter")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise SkillError(f"{path}: invalid front matter opening")
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if end is None:
        raise SkillError(f"{path}: unterminated front matter")
    try:
        values = yaml.safe_load("".join(lines[1:end])) or {}
    except yaml.YAMLError as exc:
        raise SkillError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(values, dict):
        raise SkillError(f"{path}: front matter must be a mapping")
    unknown = set(values) - {"name", "description", "disable-model-invocation"}
    if unknown:
        raise SkillError(f"{path}: unsupported fields: {', '.join(sorted(map(str, unknown)))}")
    name, description = values.get("name"), values.get("description")
    if not isinstance(name, str) or not name.strip():
        raise SkillError(f"{path}: name must be a non-empty string")
    if not isinstance(description, str) or not description.strip():
        raise SkillError(f"{path}: description must be a non-empty string")
    disabled = values.get("disable-model-invocation", False)
    if not isinstance(disabled, bool):
        raise SkillError(f"{path}: disable-model-invocation must be boolean")
    return name.strip(), description.strip(), "".join(lines[end + 1 :]).strip(), disabled


def _file_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


@dataclass
class SkillRegistry:
    """Discovers documents and atomically publishes immutable package snapshots."""

    roots: list[Path] = field(default_factory=list)
    index_body: bool = False
    snapshot_root: Path = field(default_factory=lambda: Path.cwd() / ".cortex" / "skill-snapshots")
    source_namespaces: dict[str, str] = field(default_factory=dict)
    max_file_bytes: int = 1_000_000
    max_package_bytes: int = 5_000_000
    _skills: dict[str, SkillPackage] = field(default_factory=dict, init=False)
    _versions: dict[tuple[str, str], SkillPackage] = field(default_factory=dict, init=False)
    diagnostics: list[SkillDiagnostic] = field(default_factory=list, init=False)
    generation: int = field(default=0, init=False)
    index_version: str = field(default="", init=False)

    def scan(self) -> list[SkillPackage]:
        candidates: list[SkillPackage] = []
        self.diagnostics.clear()
        self.snapshot_root.mkdir(parents=True, exist_ok=True)
        for root_value in self.roots:
            root = Path(root_value).expanduser().resolve()
            if not root.is_dir():
                self.diagnostics.append(SkillDiagnostic("missing_root", f"Skill root is not a directory: {root}", (str(root),)))
                continue
            namespace = self.source_namespaces.get(str(root), root.name)
            for document in sorted(root.glob("**/SKILL.md")):
                try:
                    candidates.append(self._snapshot_package(root, namespace, document))
                except (OSError, UnicodeError, SkillError) as exc:
                    self.diagnostics.append(SkillDiagnostic("invalid_package", str(exc), (str(document.parent),)))

        by_id: dict[str, list[SkillPackage]] = {}
        by_name: dict[str, list[SkillPackage]] = {}
        for package in candidates:
            by_id.setdefault(package.skill_id, []).append(package)
            by_name.setdefault(package.name.casefold(), []).append(package)
        conflicted = {skill_id for skill_id, items in by_id.items() if len(items) > 1}
        for skill_id in sorted(conflicted):
            items = by_id[skill_id]
            self.diagnostics.append(SkillDiagnostic("duplicate_id", f"duplicate Skill ID: {skill_id}", tuple(p.source for p in items)))
        for name, items in by_name.items():
            if len(items) > 1:
                self.diagnostics.append(SkillDiagnostic("duplicate_name", f"skill name {name!r} has {len(items)} sources; use skill_id", tuple(p.source for p in items)))
        accepted = [item for item in candidates if item.skill_id not in conflicted]
        self._skills = {item.skill_id: item for item in accepted}
        self._versions.update({(item.skill_id, item.content_hash): item for item in accepted})
        self.generation += 1
        signature = "\n".join(f"{p.skill_id}@{p.content_hash}" for p in sorted(accepted, key=lambda p: p.skill_id))
        self.index_version = hashlib.sha256(signature.encode()).hexdigest()
        return accepted

    def _snapshot_package(self, root: Path, namespace: str, document: Path) -> SkillPackage:
        package_dir = document.parent.resolve()
        if document.is_symlink() or not document.is_file() or package_dir != document.parent.resolve():
            raise SkillError(f"{document}: SKILL.md must be a regular in-root file")
        package_dir.relative_to(root)
        relative_package = document.parent.relative_to(root).as_posix()
        # Percent encoding preserves path identity instead of lossy slugging.
        skill_id = f"{quote(namespace, safe='._-')}:{quote(relative_package, safe='/._-')}"
        source = f"{namespace}::{relative_package}"
        paths = [document]
        references = package_dir / "references"
        if references.exists():
            if references.is_symlink() or not references.is_dir():
                raise SkillError(f"{references}: references must be a real directory")
            paths.extend(sorted(references.rglob("*")))
        files: dict[str, bytes] = {}
        total = 0
        for path in paths:
            if path.is_dir():
                continue
            if path.is_symlink() or not path.is_file():
                raise SkillError(f"{path}: resources must be regular files without symlinks")
            resolved = path.resolve()
            resolved.relative_to(package_dir)
            relative = resolved.relative_to(package_dir).as_posix()
            content = resolved.read_bytes()
            if len(content) > self.max_file_bytes:
                raise SkillError(f"{path}: file exceeds {self.max_file_bytes} bytes")
            total += len(content)
            if total > self.max_package_bytes:
                raise SkillError(f"{package_dir}: package exceeds {self.max_package_bytes} bytes")
            files[relative] = content
        text = files["SKILL.md"].decode("utf-8")
        name, description, body, disable_model_invocation = _parse_document(text, document)
        manifest = {path: _file_hash(content) for path, content in sorted(files.items())}
        canonical = json.dumps({"skill_id": skill_id, "files": manifest}, sort_keys=True, separators=(",", ":"))
        version = hashlib.sha256(canonical.encode()).hexdigest()
        snapshot = self.snapshot_root / version
        self._publish_snapshot(snapshot, skill_id, name, description, source, manifest, files, disable_model_invocation)
        # Return only the verified snapshot representation, never a mixture of
        # live source metadata/body and snapshot resources.
        return self._load_snapshot(skill_id, version)

    def _publish_snapshot(self, destination: Path, skill_id: str, name: str, description: str, source: str, manifest: dict[str, str], files: dict[str, bytes], disable_model_invocation: bool) -> None:
        if destination.exists():
            return
        temporary = Path(tempfile.mkdtemp(prefix=".skill-", dir=self.snapshot_root))
        try:
            file_root = temporary / "files"
            for relative, content in files.items():
                target = file_root / PurePosixPath(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            payload: dict[str, Any] = {"schema": 2, "skill_id": skill_id, "name": name, "description": description, "source": source, "content_hash": destination.name, "files": manifest, "disable_model_invocation": disable_model_invocation}
            (temporary / "manifest.json").write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            try:
                os.replace(temporary, destination)
            except FileExistsError:
                pass
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def get(self, identifier: str, content_hash: str | None = None) -> SkillPackage:
        if content_hash is not None:
            key = (identifier, content_hash)
            if key not in self._versions:
                self._versions[key] = self._load_snapshot(identifier, content_hash)
            return self._versions[key]
        if identifier in self._skills:
            return self._skills[identifier]
        matches = [p for p in self._skills.values() if p.name.casefold() == identifier.casefold()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise SkillConflictError(f"ambiguous skill name {identifier!r}; use skill_id")
        raise SkillError(f"skill not found: {identifier}")

    def _load_snapshot(self, skill_id: str, version: str) -> SkillPackage:
        root = self.snapshot_root / version
        metadata_path = root / "manifest.json"
        if not metadata_path.is_file():
            raise SkillVersionMissingError(f"skill version unavailable: {skill_id}@{version}")
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if metadata.get("skill_id") != skill_id or metadata.get("content_hash") != version:
                raise SkillSnapshotCorruptError(f"snapshot identity mismatch: {skill_id}@{version}")
            manifest = metadata["files"]
            for relative, expected in manifest.items():
                path = root / "files" / PurePosixPath(relative)
                if not path.is_file() or path.is_symlink() or _file_hash(path.read_bytes()) != expected:
                    raise SkillSnapshotCorruptError(f"snapshot file corrupt: {skill_id}@{version}/{relative}")
            text = (root / "files" / "SKILL.md").read_text(encoding="utf-8")
            name, description, body, disabled = _parse_document(text, root / "files" / "SKILL.md")
        except (KeyError, OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SkillSnapshotCorruptError(f"invalid snapshot: {skill_id}@{version}") from exc
        return SkillPackage(skill_id, name, description, body, metadata["source"], root / "files", version, manifest, disabled)

    def read_resource(self, skill_id: str, version: str, relative: str, max_chars: int) -> str:
        package = self.get(skill_id, version)
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or relative not in package.manifest or relative == "SKILL.md":
            raise SkillError(f"resource is not in snapshot manifest: {relative}")
        target = package.root / pure
        if not target.is_file() or target.is_symlink():
            raise SkillSnapshotCorruptError(f"snapshot resource is not a regular file: {skill_id}@{version}/{relative}")
        raw = target.read_bytes()
        if _file_hash(raw) != package.manifest[relative]:
            raise SkillSnapshotCorruptError(f"snapshot resource corrupt: {skill_id}@{version}/{relative}")
        content = raw.decode("utf-8")
        if len(content) > max_chars:
            raise SkillError(f"resource exceeds {max_chars} character budget")
        return content

    def names(self) -> list[str]:
        return list(self._skills)

    def packages(self) -> list[SkillPackage]:
        return list(self._skills.values())
