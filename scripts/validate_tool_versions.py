#!/usr/bin/env python3
"""Validate consumers of the canonical devbox tool version manifest."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import tomllib

_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_VERSION_REFERENCE_PATTERN = re.compile(r"\.tools\.([a-z][a-z0-9_]*)\.version")
_VERSION_LITERAL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.-])v?\d+\.\d+(?:\.\d+)*(?:-[A-Za-z0-9.-]+)?"
    r"(?![A-Za-z0-9_.-])"
)


def _read_text(path: Path, errors: list[str]) -> str:
    try:
        return path.read_text()
    except OSError as exc:
        errors.append(f"Unable to read {path}: {exc}")
        return ""


def _load_manifest(path: Path, errors: list[str]) -> dict[str, dict[str, object]]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"Unable to load {path}: {exc}")
        return {}

    if not isinstance(data, dict) or not isinstance(data.get("tools"), dict):
        errors.append(f"{path} must contain a top-level 'tools' object")
        return {}

    tools = data["tools"]
    for name, spec in tools.items():
        if not isinstance(name, str) or not _KEY_PATTERN.fullmatch(name):
            errors.append(f"Invalid tool name in {path}: {name!r}")
            continue
        if not isinstance(spec, dict):
            errors.append(f"{path}: tool '{name}' must be an object")
            continue

        for field in ("version", "datasource", "depName"):
            value = spec.get(field)
            if (
                not isinstance(value, str)
                or not value
                or any(character.isspace() for character in value)
            ):
                errors.append(
                    f"{path}: tool '{name}' needs a non-empty, whitespace-free "
                    f"'{field}'"
                )

        consumers = spec.get("consumers")
        if not isinstance(consumers, dict) or not consumers:
            errors.append(f"{path}: tool '{name}' needs a consumers object")
            continue

        unknown_consumers = set(consumers) - {
            "docker",
            "ci",
            "pre-commit",
            "pyproject",
            "lockfile",
        }
        if unknown_consumers:
            errors.append(
                f"{path}: tool '{name}' has unknown consumers: "
                + ", ".join(sorted(unknown_consumers))
            )

        for consumer in ("docker", "ci"):
            if consumer in consumers and not isinstance(consumers[consumer], bool):
                errors.append(
                    f"{path}: tool '{name}' consumer '{consumer}' must be boolean"
                )

        for consumer in ("pre-commit", "pyproject", "lockfile"):
            if consumer in consumers and (
                not isinstance(consumers[consumer], str) or not consumers[consumer]
            ):
                errors.append(
                    f"{path}: tool '{name}' consumer '{consumer}' must be a reference"
                )

        versioning = spec.get("versioning")
        if versioning is not None and (
            not isinstance(versioning, str) or not versioning
        ):
            errors.append(f"{path}: tool '{name}' has an invalid versioning value")

    return tools


def _normalized_version(value: str) -> str:
    return value.removeprefix("v")


def _active_lines(text: str) -> str:
    """Ignore full-line comments when checking executable consumers."""

    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _version_references(text: str) -> set[str]:
    return set(_VERSION_REFERENCE_PATTERN.findall(_active_lines(text)))


def _dependency_tokens(spec: dict[str, object], name: str) -> set[str]:
    tokens = {name}
    dep_name = spec.get("depName")
    if isinstance(dep_name, str):
        tokens.add(dep_name)
        tokens.add(dep_name.rsplit("/", 1)[-1])
    return tokens


def _check_no_hardcoded_versions(
    name: str,
    tools: dict[str, dict[str, object]],
    text: str,
    consumer: str,
    errors: list[str],
) -> None:
    """Reject package install lines that bypass the manifest lookup."""

    for line in _active_lines(text).splitlines():
        if not _VERSION_LITERAL_PATTERN.search(line):
            continue
        for tool_name, spec in tools.items():
            consumers = spec.get("consumers")
            if not isinstance(consumers, dict) or consumers.get(consumer) is not True:
                continue
            if any(
                re.search(
                    rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])",
                    line,
                    re.IGNORECASE,
                )
                for token in _dependency_tokens(spec, tool_name)
            ):
                errors.append(
                    f"{name} contains a hard-coded version for '{tool_name}'; "
                    "read it from the manifest"
                )


def _check_consumer_references(
    name: str,
    tools: dict[str, dict[str, object]],
    text: str,
    consumer: str,
    errors: list[str],
) -> None:
    references = _version_references(text)
    unknown_references = references - tools.keys()
    for reference in sorted(unknown_references):
        errors.append(
            f"{name} references unknown tool '{reference}' in the version manifest"
        )

    expected_references: set[str] = set()
    for tool_name, spec in tools.items():
        consumers = spec.get("consumers")
        if isinstance(consumers, dict) and consumers.get(consumer) is True:
            expected_references.add(tool_name)
    for missing in sorted(expected_references - references):
        errors.append(f"{name} does not read the canonical version for '{missing}'")

    undeclared = references - expected_references
    for extra in sorted(undeclared & tools.keys()):
        errors.append(
            f"{name} reads '{extra}', but the manifest does not declare it as a "
            f"'{consumer}' consumer"
        )


def _pre_commit_revisions(text: str) -> dict[str, list[str]]:
    revisions: dict[str, list[str]] = {}
    current_repo: str | None = None
    for line in text.splitlines():
        repo_match = re.match(r"\s*-\s*repo:\s*(\S+)\s*$", line)
        if repo_match:
            current_repo = repo_match.group(1).strip("\"'")
            continue

        if current_repo is None:
            continue
        rev_match = re.match(r"\s*rev:\s*[\"']?([^\"'#\s]+)", line)
        if rev_match:
            revisions.setdefault(current_repo, []).append(rev_match.group(1))
            current_repo = None

    return revisions


def _check_pre_commit_consumers(
    tools: dict[str, dict[str, object]], text: str, errors: list[str]
) -> None:
    revisions = _pre_commit_revisions(text)
    declared_repositories: set[str] = set()

    for name, spec in tools.items():
        consumers = spec.get("consumers")
        if not isinstance(consumers, dict):
            continue
        repository = consumers.get("pre-commit")
        if not isinstance(repository, str):
            continue

        declared_repositories.add(repository)
        actual_revisions = revisions.get(repository, [])
        if not actual_revisions:
            errors.append(
                f".pre-commit-config.yaml is missing the repository for '{name}': "
                f"{repository}"
            )
        elif len(actual_revisions) > 1:
            errors.append(
                f".pre-commit-config.yaml has multiple revisions for '{name}'"
            )
        elif _normalized_version(actual_revisions[0]) != _normalized_version(
            str(spec["version"])
        ):
            errors.append(
                f".pre-commit-config.yaml revision for '{name}' is "
                f"'{actual_revisions[0]}', expected '{spec['version']}' "
                "from the manifest"
            )

    for repository in sorted(set(revisions) - declared_repositories):
        errors.append(
            f".pre-commit-config.yaml repository '{repository}' has no canonical "
            "version entry"
        )


def _check_pyproject_consumers(
    tools: dict[str, dict[str, object]], text: str, errors: list[str]
) -> None:
    try:
        project = tomllib.loads(text).get("project", {})
    except tomllib.TOMLDecodeError as exc:
        errors.append(f"Unable to parse pyproject.toml: {exc}")
        return

    requirements: list[str] = []
    dependencies = project.get("dependencies", [])
    if isinstance(dependencies, list):
        requirements.extend(item for item in dependencies if isinstance(item, str))
    optional_dependencies = project.get("optional-dependencies", {})
    if isinstance(optional_dependencies, dict):
        for group in optional_dependencies.values():
            if isinstance(group, list):
                requirements.extend(item for item in group if isinstance(item, str))

    for name, spec in tools.items():
        consumers = spec.get("consumers")
        if not isinstance(consumers, dict):
            continue
        requirement = consumers.get("pyproject")
        if not isinstance(requirement, str):
            continue

        prefix = f"{requirement}=="
        matches = [
            dependency[len(prefix) :]
            for dependency in requirements
            if dependency.startswith(prefix)
        ]
        if not matches:
            errors.append(
                f"pyproject.toml is missing the pinned requirement for '{name}'"
            )
        elif len(matches) > 1:
            errors.append(
                f"pyproject.toml contains multiple pinned requirements for '{name}'"
            )
        elif _normalized_version(matches[0]) != _normalized_version(
            str(spec["version"])
        ):
            errors.append(
                f"pyproject.toml requirement for '{name}' is '{matches[0]}', "
                f"expected '{spec['version']}' from the manifest"
            )


def _lockfile_versions(text: str) -> dict[str, list[str]]:
    package_pattern = re.compile(
        r'(?ms)^\[\[package\]\]\s*\nname = "([^"]+)".*?^version = "([^"]+)"'
    )
    versions: dict[str, list[str]] = {}
    for package_name, version in package_pattern.findall(text):
        versions.setdefault(package_name, []).append(version)
    return versions


def _check_lockfile_consumers(
    tools: dict[str, dict[str, object]], text: str, errors: list[str]
) -> None:
    versions = _lockfile_versions(text)
    for name, spec in tools.items():
        consumers = spec.get("consumers")
        if not isinstance(consumers, dict):
            continue
        package_name = consumers.get("lockfile")
        if not isinstance(package_name, str):
            continue

        actual_versions = versions.get(package_name, [])
        if not actual_versions:
            errors.append(
                f"uv.lock is missing the package entry for '{name}': {package_name}"
            )
        elif len(actual_versions) > 1:
            errors.append(
                f"uv.lock contains multiple package entries for '{package_name}'"
            )
        elif _normalized_version(actual_versions[0]) != _normalized_version(
            str(spec["version"])
        ):
            errors.append(
                f"uv.lock version for '{name}' is '{actual_versions[0]}', "
                f"expected '{spec['version']}' from the manifest"
            )


def validate_tool_versions(repo_root: Path) -> list[str]:
    """Return all manifest/consumer consistency errors for ``repo_root``."""

    errors: list[str] = []
    manifest_path = repo_root / "container" / "tool-versions.json"
    tools = _load_manifest(manifest_path, errors)
    if errors:
        return errors

    dockerfile_path = repo_root / "container" / "Dockerfile"
    workflow_path = repo_root / ".github" / "workflows" / "ci-workflow.yaml"
    pre_commit_path = repo_root / ".pre-commit-config.yaml"
    pyproject_path = repo_root / "pyproject.toml"
    lockfile_path = repo_root / "uv.lock"

    dockerfile = _read_text(dockerfile_path, errors)
    workflow = _read_text(workflow_path, errors)
    pre_commit = _read_text(pre_commit_path, errors)
    pyproject = _read_text(pyproject_path, errors)
    lockfile = _read_text(lockfile_path, errors)
    if errors:
        return errors

    if "COPY tool-versions.json /tmp/devbox-tool-versions.json" not in dockerfile:
        errors.append(
            "container/Dockerfile must copy container/tool-versions.json into the build"
        )
    if "rm -f /tmp/devbox-tool-versions.json" not in dockerfile:
        errors.append(
            "container/Dockerfile must remove the manifest from the final image"
        )
    if re.search(r"^\s*ARG\s+[A-Z0-9_]+_VERSION\s*=", dockerfile, re.MULTILINE):
        errors.append(
            "container/Dockerfile still declares a *_VERSION build argument; "
            "read it from tool-versions.json instead"
        )

    _check_consumer_references(
        "container/Dockerfile", tools, dockerfile, "docker", errors
    )
    _check_no_hardcoded_versions(
        "container/Dockerfile", tools, dockerfile, "docker", errors
    )

    if "container/tool-versions.json" not in workflow:
        errors.append(
            ".github/workflows/ci-workflow.yaml must read container/tool-versions.json"
        )
    if "ARG PRE_COMMIT_VERSION" in workflow or "awk -F=" in workflow:
        errors.append(
            ".github/workflows/ci-workflow.yaml must not parse version arguments "
            "from container/Dockerfile"
        )
    _check_consumer_references(
        ".github/workflows/ci-workflow.yaml", tools, workflow, "ci", errors
    )
    _check_no_hardcoded_versions(
        ".github/workflows/ci-workflow.yaml", tools, workflow, "ci", errors
    )
    _check_pre_commit_consumers(tools, pre_commit, errors)
    _check_pyproject_consumers(tools, pyproject, errors)
    _check_lockfile_consumers(tools, lockfile, errors)

    return errors


def main() -> int:
    errors = validate_tool_versions(Path(__file__).resolve().parents[1])
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("Tool version manifest and consumers are synchronized.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
