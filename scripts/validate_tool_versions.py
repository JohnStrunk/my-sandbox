#!/usr/bin/env python3
"""Validate consumers of the canonical devbox tool version manifest."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import tomllib

_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_VERSION_REFERENCE_PATTERN = re.compile(r"\.tools\.([a-z][a-z0-9_]*)\.version")
_SAFE_VERSION_PATTERN = re.compile(r"^v?\d+(?:\.\d+){1,3}(?:[-+][A-Za-z0-9.]+)?$")
_VERSION_LITERAL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.-])v?\d+\.\d+(?:\.\d+)*(?:-[A-Za-z0-9.-]+)?"
    r"(?![A-Za-z0-9_.-])"
)
_TRUST_ANCHOR_PIN_PATTERN = re.compile(
    r'^\s*\["(?P<name>[a-z0-9-]+\.crt)"\]="(?P<digest>[0-9a-f]{64})"$',
    re.MULTILINE,
)
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_GIT_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
_PROVENANCE_PLACEHOLDER_PATTERN = re.compile(r"\{([a-z][a-z0-9_.]*)\}")
_SUPPORTED_ARCHES = frozenset({"amd64", "arm64"})
_EXPECTED_TRUST_ANCHORS = frozenset(
    {
        "redhat-ipa-ca.crt",
        "redhat-rhcsv2-ca.crt",
        "redhat-root-ca.crt",
    }
)
_ALLOWED_AGENT_SKILL_FIELDS = frozenset({"integrity", "commit", "sha256"})
_INTEGRITY_POLICIES = frozenset({"sha256", "version-only"})
_ALLOWED_ARTIFACT_FIELDS = frozenset(
    {"version", "sha256", "depName", "packageName", "datasource"}
)
_ALLOWED_ARTIFACT_DATASOURCES = frozenset(
    {
        "github-release-attachments",
        "custom.acli-amd64",
        "custom.acli-arm64",
    }
)
_CUSTOM_ARTIFACT_DATASOURCE_TOOL_ARCHES = {
    "custom.acli-amd64": ("acli", "amd64"),
    "custom.acli-arm64": ("acli", "arm64"),
}
_ALLOWED_PROVENANCE_PLACEHOLDERS = frozenset(
    {"version", "agent_skill.commit"}
    | {f"artifacts.{arch}.version" for arch in _SUPPORTED_ARCHES}
)
_ALLOWED_TOOL_FIELDS = frozenset(
    {
        "version",
        "datasource",
        "depName",
        "versioning",
        "consumers",
        "integrity",
        "artifacts",
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

        version = spec.get("version")
        if isinstance(version, str) and not _SAFE_VERSION_PATTERN.fullmatch(version):
            errors.append(
                f"{path}: tool '{name}' version must be a safe version token, "
                "not a URL, range, or package specifier"
            )

        integrity = spec.get("integrity")
        if not isinstance(integrity, str) or integrity not in _INTEGRITY_POLICIES:
            errors.append(
                f"{path}: tool '{name}' needs an explicit integrity policy of "
                "'sha256' or 'version-only'"
            )

        consumers = spec.get("consumers")
        if not isinstance(consumers, dict) or not consumers:
            errors.append(f"{path}: tool '{name}' needs a consumers object")
            continue

        unknown_consumers = set(consumers) - {
            "ci",
            "pre-commit",
            "pyproject",
            "lockfile",
            "lima",
        }
        if unknown_consumers:
            errors.append(
                f"{path}: tool '{name}' has unknown consumers: "
                + ", ".join(sorted(unknown_consumers))
            )

        for consumer in ("ci", "lima"):
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


_LIMA_MANIFEST_VERSION_PATTERN = re.compile(
    r"\b(?:manifest_version|ensure_npm_package)\s+([a-z][a-z0-9_]*)\b"
)
_LIMA_MANIFEST_CHECKSUM_PATTERN = re.compile(r"\bverify_download\s+([a-z][a-z0-9_]*)\b")
_LIMA_AGENT_SKILL_PATTERN = re.compile(
    r"\bmanifest_agent_skill\s+([a-z][a-z0-9_]*)\s+(integrity|commit|sha256)\b"
)


def _lima_scripts(repo_root: Path, errors: list[str]) -> dict[str, str]:
    """Return the contents of lima/*.sh, keyed by file name."""

    lima_dir = repo_root / "lima"
    if not lima_dir.is_dir():
        errors.append("lima/ directory with provisioning scripts is missing")
        return {}
    scripts = {
        path.name: _read_text(path, errors) for path in sorted(lima_dir.glob("*.sh"))
    }
    if not scripts:
        errors.append("lima/ contains no provisioning scripts")
    return scripts


def _check_lima_consumers(
    tools: dict[str, dict[str, object]],
    repo_root: Path,
    errors: list[str],
) -> None:
    """Require Lima provisioning to read each declared tool from the manifest.

    The VM's manifest is on a live host-shared mount. Reading it at each start
    lets a version bump be applied by
    restarting the VM without recreating it.
    """

    scripts = _lima_scripts(repo_root, errors)
    if not scripts:
        return
    active_text = "\n".join(_active_lines(text) for text in scripts.values())
    version_reads = set(_LIMA_MANIFEST_VERSION_PATTERN.findall(active_text))
    checksum_reads = set(_LIMA_MANIFEST_CHECKSUM_PATTERN.findall(active_text))
    skill_reads = set(_LIMA_AGENT_SKILL_PATTERN.findall(active_text))

    if not re.search(r"\bmanifest_integrity_policy\s+[\"']?\$tool\b", active_text):
        errors.append(
            "lima/*.sh must validate each tool's explicit integrity policy before "
            "using its manifest version"
        )

    declared = {
        name
        for name, spec in tools.items()
        if isinstance(spec.get("consumers"), dict)
        and spec["consumers"].get("lima") is True
    }
    for name in sorted(declared):
        spec = tools[name]
        if name not in version_reads:
            errors.append(
                f"lima/*.sh does not read '{name}' version from tool-versions.json"
            )
        if spec.get("integrity") == "sha256" and name not in checksum_reads:
            errors.append(
                f"lima/*.sh does not verify '{name}' SHA-256 artifacts from "
                "tool-versions.json"
            )
        agent_skill = spec.get("agent_skill")
        if isinstance(agent_skill, dict):
            required_skill_fields = {"integrity", "commit"}
            if agent_skill.get("integrity") == "sha256":
                required_skill_fields.add("sha256")
            for field in sorted(required_skill_fields):
                if (name, field) not in skill_reads:
                    errors.append(
                        f"lima/*.sh does not read '{name}' agent_skill['{field}'] "
                        "from tool-versions.json"
                    )

    for name in sorted(version_reads - declared):
        if name not in tools:
            errors.append(
                f"lima/*.sh reads '{name}' but the version manifest has no entry"
            )
        else:
            errors.append(
                f"lima/*.sh reads '{name}', but the manifest does not declare "
                "it as a 'lima' consumer"
            )

    for name in sorted(checksum_reads):
        spec = tools.get(name)
        if not isinstance(spec, dict):
            errors.append(
                f"lima/*.sh reads '{name}' checksums but the version manifest "
                "has no entry"
            )
        elif name not in declared:
            errors.append(
                f"lima/*.sh reads '{name}' checksums, but the manifest does not "
                "declare it as a 'lima' consumer"
            )

    for name, field in sorted(skill_reads):
        spec = tools.get(name)
        if not isinstance(spec, dict) or not isinstance(spec.get("agent_skill"), dict):
            errors.append(
                f"lima/*.sh reads '{name}' agent-skill metadata but the manifest "
                "does not declare it"
            )
        elif name not in declared:
            errors.append(
                f"lima/*.sh reads '{name}' agent-skill metadata, but the manifest "
                "does not declare it as a 'lima' consumer"
            )
        elif field not in spec["agent_skill"]:
            if (
                field == "sha256"
                and spec["agent_skill"].get("integrity") == "version-only"
            ):
                continue
            errors.append(
                f"lima/*.sh reads '{name}' agent_skill['{field}'] but the "
                "manifest does not declare it"
            )


def _declared_checksummed_fields(
    spec: dict[str, object],
) -> set[str]:
    fields: set[str] = set()
    artifacts = spec.get("artifacts")
    if isinstance(artifacts, dict):
        fields.update(f"artifacts.{arch}.sha256" for arch in artifacts)
    agent_skill = spec.get("agent_skill")
    if isinstance(agent_skill, dict) and agent_skill.get("integrity") == "sha256":
        fields.add("agent_skill.sha256")
    return fields


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and bool(_SHA256_PATTERN.fullmatch(value))


def _check_trust_anchors(repo_root: Path, errors: list[str]) -> None:
    """Verify the internal CA files against their manifest-pinned digests."""

    manifest_path = repo_root / "lima" / "tool-versions.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"Unable to load trust anchors from {manifest_path}: {exc}")
        return
    anchors = manifest.get("trust_anchors") if isinstance(manifest, dict) else None
    if not isinstance(anchors, dict):
        errors.append(f"{manifest_path} must contain a 'trust_anchors' object")
        return

    missing = _EXPECTED_TRUST_ANCHORS - anchors.keys()
    extra = anchors.keys() - _EXPECTED_TRUST_ANCHORS
    if missing:
        errors.append(
            "tool manifest is missing CA trust anchors: " + ", ".join(sorted(missing))
        )
    if extra:
        errors.append(
            "tool manifest has unknown CA trust anchors: " + ", ".join(sorted(extra))
        )

    system_script_path = repo_root / "lima" / "provision-system.sh"
    system_script = _read_text(system_script_path, errors)
    embedded_pins = dict(_TRUST_ANCHOR_PIN_PATTERN.findall(system_script))
    if embedded_pins != anchors:
        errors.append(
            "lima/provision-system.sh embedded CA trust-anchor pins must match "
            "lima/tool-versions.json"
        )

    for name in sorted(_EXPECTED_TRUST_ANCHORS & anchors.keys()):
        expected = anchors[name]
        if not _is_sha256(expected):
            errors.append(f"trust anchor '{name}' must have a SHA-256 digest")
            continue
        path = repo_root / "lima" / "certs" / name
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            errors.append(f"Unable to read CA trust anchor {path}: {exc}")
            continue
        if actual != expected:
            errors.append(
                f"CA trust anchor '{name}' does not match its manifest SHA-256"
            )


def _check_artifacts(
    name: str,
    spec: dict[str, object],
    artifacts: object,
    errors: list[str],
) -> None:
    if not isinstance(artifacts, dict):
        errors.append(
            f"tool '{name}' SHA-256 integrity needs per-architecture artifacts"
        )
        return
    if set(artifacts) != _SUPPORTED_ARCHES:
        errors.append(
            f"tool '{name}' SHA-256 artifacts must declare exactly amd64 and arm64"
        )
    package_name = spec.get("depName")
    alias = spec.get("version")
    for arch in sorted(set(artifacts) - _SUPPORTED_ARCHES):
        errors.append(
            f"tool '{name}' declares an artifact for unsupported architecture '{arch}'"
        )
    dep_names: set[str] = set()
    for arch, artifact in sorted(artifacts.items()):
        if not isinstance(artifact, dict):
            errors.append(f"tool '{name}' artifacts['{arch}'] must be an object")
            continue
        unknown_fields = set(artifact) - _ALLOWED_ARTIFACT_FIELDS
        if unknown_fields:
            errors.append(
                f"tool '{name}' artifacts['{arch}'] has unknown field(s): "
                + ", ".join(sorted(unknown_fields))
            )

        artifact_version = artifact.get("version")
        if not isinstance(artifact_version, str) or not _SAFE_VERSION_PATTERN.fullmatch(
            artifact_version
        ):
            errors.append(
                f"tool '{name}' artifacts['{arch}'].version must be a safe "
                "version or exact release tag"
            )
        elif isinstance(alias, str) and _normalized_version(artifact_version) != (
            _normalized_version(alias)
        ):
            errors.append(
                f"tool '{name}' artifacts['{arch}'].version is "
                f"'{artifact_version}', which does not normalize to top-level "
                f"version '{alias}'"
            )

        if not _is_sha256(artifact.get("sha256")):
            errors.append(
                f"tool '{name}' artifacts['{arch}'].sha256 must be a 64-char "
                "lowercase SHA-256 hex digest"
            )

        dep_name = artifact.get("depName")
        if not isinstance(dep_name, str) or not dep_name:
            errors.append(
                f"tool '{name}' artifacts['{arch}'].depName must be a non-empty string"
            )
        else:
            if dep_name in dep_names:
                errors.append(
                    f"tool '{name}' architecture artifact depNames must be unique"
                )
            dep_names.add(dep_name)
            if isinstance(package_name, str) and dep_name != f"{package_name}-{arch}":
                errors.append(
                    f"tool '{name}' artifacts['{arch}'].depName must be "
                    f"'{package_name}-{arch}'"
                )

        artifact_package_name = artifact.get("packageName")
        if artifact_package_name != package_name:
            errors.append(
                f"tool '{name}' artifacts['{arch}'].packageName must match "
                "the tool's depName so Renovate groups the alias and artifact"
            )

        datasource = artifact.get("datasource")
        if (
            not isinstance(datasource, str)
            or datasource not in _ALLOWED_ARTIFACT_DATASOURCES
        ):
            errors.append(
                f"tool '{name}' artifacts['{arch}'].datasource must be one of: "
                + ", ".join(sorted(_ALLOWED_ARTIFACT_DATASOURCES))
            )
        elif datasource in _CUSTOM_ARTIFACT_DATASOURCE_TOOL_ARCHES:
            expected_tool, expected_arch = _CUSTOM_ARTIFACT_DATASOURCE_TOOL_ARCHES[
                datasource
            ]
            if (name, arch) != (expected_tool, expected_arch):
                errors.append(
                    f"tool '{name}' artifacts['{arch}'].datasource must match the "
                    f"{expected_tool} architecture-specific datasource"
                )


def _check_agent_skill(name: str, agent_skill: object, errors: list[str]) -> None:
    if not isinstance(agent_skill, dict):
        errors.append(f"tool '{name}' 'agent_skill' must be an object")
        return
    for field in sorted(set(agent_skill) - _ALLOWED_AGENT_SKILL_FIELDS):
        errors.append(f"tool '{name}' agent_skill has unknown field '{field}'")
    integrity = agent_skill.get("integrity")
    if not isinstance(integrity, str) or integrity not in _INTEGRITY_POLICIES:
        errors.append(
            f"tool '{name}' agent_skill needs an explicit integrity policy of "
            "'sha256' or 'version-only'"
        )
    commit = agent_skill.get("commit")
    if not isinstance(commit, str) or not _GIT_COMMIT_PATTERN.fullmatch(commit):
        errors.append(
            f"tool '{name}' agent_skill['commit'] must be a 40-char lowercase "
            "Git commit hex"
        )
    if integrity == "version-only" and "sha256" in agent_skill:
        errors.append(
            f"tool '{name}' version-only agent_skill must not declare a stale "
            "SHA-256 digest"
        )
    if integrity == "sha256" and not _is_sha256(agent_skill.get("sha256")):
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
            f"tool '{name}' declares 'provenance' but has no SHA-256-managed "
            "artifact or agent-skill digest to verify"
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
    errors: list[str],
) -> None:
    for name, spec in tools.items():
        integrity = spec.get("integrity")
        if integrity == "sha256":
            _check_artifacts(name, spec, spec.get("artifacts"), errors)
        elif integrity == "version-only" and "artifacts" in spec:
            errors.append(
                f"tool '{name}' has artifacts despite declaring version-only integrity"
            )
        if "agent_skill" in spec:
            _check_agent_skill(name, spec["agent_skill"], errors)
        required = _declared_checksummed_fields(spec)
        if required and "provenance" not in spec:
            errors.append(
                f"tool '{name}' declares SHA-256 digests but has no "
                "'provenance.url_templates' to verify them"
            )
        elif "provenance" in spec:
            _check_provenance_block(name, spec, spec["provenance"], errors)


def validate_tool_versions(repo_root: Path) -> list[str]:
    """Return all manifest/consumer consistency errors for ``repo_root``."""

    errors: list[str] = []
    manifest_path = repo_root / "lima" / "tool-versions.json"
    tools = _load_manifest(manifest_path, errors)
    if errors:
        return errors
    _check_trust_anchors(repo_root, errors)
    if errors:
        return errors

    workflow_path = repo_root / ".github" / "workflows" / "ci-workflow.yaml"
    pre_commit_path = repo_root / ".pre-commit-config.yaml"
    pyproject_path = repo_root / "pyproject.toml"
    lockfile_path = repo_root / "uv.lock"

    workflow = _read_text(workflow_path, errors)
    pre_commit = _read_text(pre_commit_path, errors)
    pyproject = _read_text(pyproject_path, errors)
    lockfile = _read_text(lockfile_path, errors)
    if errors:
        return errors

    if "lima/tool-versions.json" not in workflow:
        errors.append(
            ".github/workflows/ci-workflow.yaml must read lima/tool-versions.json"
        )
    _check_consumer_references(
        ".github/workflows/ci-workflow.yaml", tools, workflow, "ci", errors
    )
    _check_no_hardcoded_versions(
        ".github/workflows/ci-workflow.yaml", tools, workflow, "ci", errors
    )
    _check_provenance(tools, errors)
    _check_pre_commit_consumers(tools, pre_commit, errors)
    _check_pyproject_consumers(tools, pyproject, errors)
    _check_lockfile_consumers(tools, lockfile, errors)
    _check_lima_consumers(tools, repo_root, errors)

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
