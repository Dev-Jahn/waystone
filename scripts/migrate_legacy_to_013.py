#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""One-time static migration from the legacy Waystone surface to 0.13."""
from __future__ import annotations

import json
import shutil
import sys
import uuid
from pathlib import Path

import yaml

repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo_root))

from waystone.core import WorkflowError, write_text_atomic  # noqa: E402
from waystone.project import load_tasks, normalize_config, registry_path  # noqa: E402
from waystone.project.brief import parse_project_brief  # noqa: E402


LEGACY_KEYS = ("ssot", "generated_dir", "review", "delegation", "state")
SECTIONS = (
    "Purpose", "Commitments", "Prototype scope", "Long-term direction", "Non-goals",
    "Working hypotheses", "Open questions", "Revision triggers",
)
GUIDE = "dev_docs/0.13-migration-guide.md"


class MigrationError(WorkflowError):
    """A legacy project cannot be migrated without guessing."""

    code = "legacy_migration_error"

    def __init__(self, detail: str):
        super().__init__(f"{self.code}: {detail}")


def _has_yaml_comment(text: str) -> bool:
    """Detect YAML comments without treating hashes inside quoted scalars as comments."""
    for line in text.splitlines():
        single_quoted = False
        double_quoted = False
        escaped = False
        for index, character in enumerate(line):
            if escaped:
                escaped = False
                continue
            if character == "\\" and double_quoted:
                escaped = True
            elif character == "'" and not double_quoted:
                single_quoted = not single_quoted
            elif character == '"' and not single_quoted:
                double_quoted = not double_quoted
            elif (
                character == "#"
                and not single_quoted
                and not double_quoted
                and (index == 0 or line[index - 1].isspace())
            ):
                return True
    return False


def _read_config(path: Path) -> tuple[str, dict]:
    if not path.is_file():
        raise MigrationError(f"project config does not exist: {path}")
    try:
        original = path.read_text(encoding="utf-8")
        loaded = yaml.safe_load(original)
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise MigrationError(f"cannot read project config {path}: {error}") from error
    if loaded is None:
        return original, {}
    if not isinstance(loaded, dict):
        raise MigrationError(f"project config must be a YAML mapping: {path}")
    return original, loaded


def _migrate_config(path: Path, original: str, legacy: dict) -> tuple[dict, str | None]:
    migrated = dict(legacy)
    for key in LEGACY_KEYS:
        migrated.pop(key, None)
    migrated.setdefault("brief", "PROJECT_BRIEF.md")
    try:
        normalize_config(migrated, source=path)
    except (TypeError, ValueError) as error:
        raise MigrationError(f"migrated config is invalid: {error}") from error

    rendered = yaml.safe_dump(migrated, allow_unicode=True, sort_keys=False)
    if rendered == original:
        return migrated, None
    write_text_atomic(path, rendered)
    return migrated, "migrated .waystone.yml"


def _project_name(root: Path, config: dict) -> str:
    configured = config.get("project")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    tasks_path = root / "tasks.yaml"
    if tasks_path.is_file():
        try:
            tasks = load_tasks(root)
        except (OSError, UnicodeError, ValueError, yaml.YAMLError) as error:
            raise MigrationError(f"cannot read {tasks_path}: {error}") from error
        task_project = tasks.get("project")
        if isinstance(task_project, str) and task_project.strip():
            return task_project.strip()
    return root.name


def _brief_bytes(project_name: str) -> bytes:
    headings = "\n\n".join(f"## {section}" for section in SECTIONS)
    return (
        "---\n"
        "schema: waystone-project-brief-1\n"
        "status: provisional\n"
        "---\n"
        f"# {project_name}\n\n"
        f"{headings}\n"
    ).encode("utf-8")


def _ensure_brief(root: Path, config: dict) -> str | None:
    path = root / "PROJECT_BRIEF.md"
    if path.exists():
        if not path.is_file():
            raise MigrationError(f"PROJECT_BRIEF.md is not a regular file: {path}")
        try:
            parse_project_brief(path.read_bytes(), path="PROJECT_BRIEF.md")
        except (OSError, WorkflowError) as error:
            raise MigrationError(f"existing PROJECT_BRIEF.md is invalid: {error}") from error
        return None

    content = _brief_bytes(_project_name(root, config))
    write_text_atomic(path, content.decode("utf-8"))
    try:
        parse_project_brief(path.read_bytes(), path="PROJECT_BRIEF.md")
    except (OSError, WorkflowError) as error:
        raise MigrationError(f"created PROJECT_BRIEF.md failed self-check: {error}") from error
    return "created and verified PROJECT_BRIEF.md skeleton"


def _legacy_directory(root: Path, value: object) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise MigrationError("legacy generated_dir must be a non-empty relative path")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or relative == Path("."):
        raise MigrationError(f"legacy generated_dir is not a safe project-relative path: {value!r}")
    return root / relative


def _remove_generated_dir(root: Path, value: object) -> str | None:
    target = _legacy_directory(root, value)
    if not target.exists() and not target.is_symlink():
        return None
    if target.is_symlink() or not target.is_dir():
        raise MigrationError(f"legacy generated_dir is not a real directory: {target}")
    resolved = target.resolve()
    if resolved != target or not resolved.is_relative_to(root):
        raise MigrationError(f"legacy generated_dir resolves outside the project: {target}")
    shutil.rmtree(target)
    return f"removed legacy generated directory {target}"


def _report_legacy_ssot(root: Path, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise MigrationError("legacy ssot must be a non-empty path")
    path = root / value
    if path.exists():
        print(f"[info] legacy SSOT preserved: {path}")
        print(f"[info] content mapping: follow {GUIDE}")
    else:
        print(f"[info] legacy SSOT not found: {path}")


def _migrate_registry() -> tuple[int, str | None]:
    path = registry_path()
    if not path.exists():
        print("[info] registry 없음 — 이후 waystone project register 실행")
        return 0, None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise MigrationError(f"cannot read registry {path}: {error}") from error
    projects = document.get("projects") if isinstance(document, dict) else None
    if not isinstance(projects, list) or any(not isinstance(entry, dict) for entry in projects):
        raise MigrationError(f"registry projects must be a list of objects: {path}")

    changed = 0
    for entry in projects:
        project_id = entry.get("project_id")
        if "path" in entry and (not isinstance(project_id, str) or not project_id.strip()):
            entry["project_id"] = f"project:{uuid.uuid4().hex}"
            changed += 1
    if changed:
        write_text_atomic(path, json.dumps(document, ensure_ascii=False, indent=2) + "\n")
        return changed, f"assigned project_id to {changed} registry entry(s)"
    return 0, None


def migrate(root_argument: str) -> int:
    root = Path(root_argument).expanduser().resolve()
    config_path = root / ".waystone.yml"
    original, legacy_config = _read_config(config_path)
    generated_dir = legacy_config.get("generated_dir", "docs/ssot")
    legacy_ssot = legacy_config.get("ssot", "SSOT.md")
    performed: list[str] = []
    skipped: list[str] = []

    config, result = _migrate_config(config_path, original, legacy_config)
    (performed if result else skipped).append(result or ".waystone.yml already canonical")
    if result and _has_yaml_comment(original):
        print("[warning] .waystone.yml 주석은 보존되지 않음")

    result = _ensure_brief(root, config)
    (performed if result else skipped).append(result or "PROJECT_BRIEF.md already valid")
    result = _remove_generated_dir(root, generated_dir)
    (performed if result else skipped).append(result or "legacy generated directory absent")
    _report_legacy_ssot(root, legacy_ssot)
    _, result = _migrate_registry()
    (performed if result else skipped).append(result or "registry project_ids already migrated")

    print("\nMigration summary")
    for item in performed:
        print(f"  performed: {item}")
    for item in skipped:
        print(f"  skipped: {item}")
    print(f"Remaining judgment work: follow {GUIDE}")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: uv run scripts/migrate_legacy_to_013.py <project-root>", file=sys.stderr)
        return 1
    try:
        return migrate(argv[0])
    except (MigrationError, OSError) as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
