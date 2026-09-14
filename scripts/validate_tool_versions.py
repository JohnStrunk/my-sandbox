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
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_GIT_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
_PROVENANCE_REFERENCE_PATTERN = re.compile(
    r"\.tools\.([a-z][a-z0-9_]*)\.(checksums|agent_skill)\.([a-z][a-z0-9_]*)"
)
_PROVENANCE_PLACEHOLDER_PATTERN = re.compile(r"\{([a-z][a-z0-9_.]*)\}")
_SUPPORTED_ARCHES = frozenset({"amd64", "arm64"})
_ALLOWED_AGENT_SKILL_FIELDS = frozenset({"commit", "sha256"})
_ALLOWED_PROVENANCE_PLACEHOLDERS = frozenset({"version", "agent_skill.commit"})
_ALLOWED_TOOL_FIELDS = frozenset(
    {
        "version",
        "datasource",
        "depName",
        "versioning",
        "consumers",
        "checksums",
        "agent_skill",
        "provenance",
    }
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

        unknown_fields = set(spec) - _ALLOWED_TOOL_FIELDS
        if unknown_fields:
            errors.append(
                f"{path}: tool '{name}' has unknown field(s): "
                + ", ".join(sorted(unknown_fields))
            )

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


def _provenance_references(text: str) -> dict[tuple[str, str], set[str]]:
    """Group ``.tools.<tool>.<kind>.<field>`` reads by (tool, kind)."""

    refs: dict[tuple[str, str], set[str]] = {}
    for tool, kind, field in _PROVENANCE_REFERENCE_PATTERN.findall(_active_lines(text)):
        refs.setdefault((tool, kind), set()).add(field)
    return refs


def _declared_provenance_fields(
    tools: dict[str, dict[str, object]],
) -> dict[tuple[str, str], set[str]]:
    declared: dict[tuple[str, str], set[str]] = {}
    for name, spec in tools.items():
        checksums = spec.get("checksums")
        if isinstance(checksums, dict):
            declared[(name, "checksums")] = set(checksums)
        agent_skill = spec.get("agent_skill")
        if isinstance(agent_skill, dict):
            declared[(name, "agent_skill")] = set(agent_skill)
    return declared


def _declared_checksummed_fields(
    spec: dict[str, object],
) -> set[str]:
    fields: set[str] = set()
    checksums = spec.get("checksums")
    if isinstance(checksums, dict):
        fields.update(f"checksums.{arch}" for arch in checksums)
    agent_skill = spec.get("agent_skill")
    if isinstance(agent_skill, dict) and "sha256" in agent_skill:
        fields.add("agent_skill.sha256")
    return fields


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and bool(_SHA256_PATTERN.fullmatch(value))


def _check_checksums(name: str, checksums: object, errors: list[str]) -> None:
    if not isinstance(checksums, dict) or not checksums:
        errors.append(f"tool '{name}' 'checksums' must be a non-empty object")
        return
    for arch in sorted(set(checksums) - _SUPPORTED_ARCHES):
        errors.append(
            f"tool '{name}' declares a checksum for unsupported architecture '{arch}'"
        )
    for arch, value in sorted(checksums.items()):
        if not _is_sha256(value):
            errors.append(
                f"tool '{name}' checksums['{arch}'] must be a 64-char lowercase "
                "SHA-256 hex digest"
            )


def _check_agent_skill(name: str, agent_skill: object, errors: list[str]) -> None:
    if not isinstance(agent_skill, dict):
        errors.append(f"tool '{name}' 'agent_skill' must be an object")
        return
    for field in sorted(set(agent_skill) - _ALLOWED_AGENT_SKILL_FIELDS):
        errors.append(f"tool '{name}' agent_skill has unknown field '{field}'")
    if "commit" in agent_skill and not (
        isinstance(agent_skill["commit"], str)
        and bool(_GIT_COMMIT_PATTERN.fullmatch(agent_skill["commit"]))
    ):
        errors.append(
            f"tool '{name}' agent_skill['commit'] must be a 40-char lowercase "
            "Git commit hex"
        )
    if "sha256" in agent_skill and not _is_sha256(agent_skill["sha256"]):
        errors.append(
            f"tool '{name}' agent_skill['sha256'] must be a 64-char lowercase "
            "SHA-256 hex digest"
        )


def _check_provenance_block(
    name: str, spec: dict[str, object], provenance: object, errors: list[str]
) -> None:
    required = _declared_checksummed_fields(spec)
    if not required:
        errors.append(
            f"tool '{name}' declares 'provenance' but has no checksum or "
            "agent-skill metadata to verify"
        )
    if not isinstance(provenance, dict):
        errors.append(f"tool '{name}' 'provenance' must be an object")
        return
    for field in sorted(set(provenance) - {"url_templates"}):
        errors.append(f"tool '{name}' provenance has unknown field '{field}'")
    templates = provenance.get("url_templates")
    if not isinstance(templates, dict) or not templates:
        errors.append(
            f"tool '{name}' provenance must declare a non-empty 'url_templates' object"
        )
        return
    for missing in sorted(required - set(templates)):
        errors.append(f"tool '{name}' provenance.url_templates is missing '{missing}'")
    for extra in sorted(set(templates) - required):
        errors.append(
            f"tool '{name}' provenance.url_templates has no matching checksum "
            f"field for '{extra}'"
        )
    for field, template in sorted(templates.items()):
        if not isinstance(template, str) or not template.startswith("https://"):
            errors.append(
                f"tool '{name}' provenance.url_templates['{field}'] must be an "
                "https URL"
            )
            continue
        for placeholder in _PROVENANCE_PLACEHOLDER_PATTERN.findall(template):
            if placeholder not in _ALLOWED_PROVENANCE_PLACEHOLDERS:
                errors.append(
                    f"tool '{name}' provenance.url_templates['{field}'] uses "
                    f"unknown placeholder '{{{placeholder}}}'"
                )


def _check_provenance(
    tools: dict[str, dict[str, object]],
    dockerfile_name: str,
    dockerfile_text: str,
    errors: list[str],
) -> None:
    for name, spec in tools.items():
        if "checksums" in spec:
            _check_checksums(name, spec["checksums"], errors)
        if "agent_skill" in spec:
            _check_agent_skill(name, spec["agent_skill"], errors)
        if "provenance" in spec:
            _check_provenance_block(name, spec, spec["provenance"], errors)
        elif _declared_checksummed_fields(spec):
            errors.append(
                f"tool '{name}' declares checksum/agent-skill metadata but "
                "has no 'provenance.url_templates' to verify it"
            )

    _check_provenance_dockerfile_coherence(
        tools, dockerfile_name, dockerfile_text, errors
    )


def _check_provenance_dockerfile_coherence(
    tools: dict[str, dict[str, object]],
    dockerfile_name: str,
    dockerfile_text: str,
    errors: list[str],
) -> None:
    """Ensure every Dockerfile checksum/skill read has a manifest entry."""

    reads = _provenance_references(dockerfile_text)
    declared = _declared_provenance_fields(tools)

    for tool in sorted({tool for tool, _kind in reads}):
        if tool not in tools:
            errors.append(
                f"{dockerfile_name} references unknown tool '{tool}' provenance"
            )

    for (tool, kind), read_fields in sorted(reads.items()):
        declared_fields = declared.get((tool, kind), set())
        for missing in sorted(read_fields - declared_fields):
            errors.append(
                f"{dockerfile_name} reads '{tool}' {kind}['{missing}'] but the "
                "manifest does not declare it"
            )
    for (tool, kind), declared_fields in sorted(declared.items()):
        read_fields = reads.get((tool, kind), set())
        for unused in sorted(declared_fields - read_fields):
            errors.append(
                f"the manifest declares '{tool}' {kind}['{unused}'] but "
                f"{dockerfile_name} never reads it"
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
    _check_provenance(tools, "container/Dockerfile", dockerfile, errors)

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
