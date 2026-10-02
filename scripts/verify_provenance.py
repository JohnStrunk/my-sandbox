#!/usr/bin/env python3
"""Verify SHA-256-managed assets in the canonical tool manifest.

Every tool and downloaded agent skill declares an explicit integrity policy.
This script fetches and hashes only ``sha256`` records; ``version-only`` records
are intentionally not checksum-verified. Checksum-managed architecture records
map through ``provenance.url_templates`` to their exact upstream release assets.

The default mode is check-only, and CI invokes it without a write mode.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "lima" / "tool-versions.json"

_PLACEHOLDER_PATTERN = re.compile(r"\{([a-z][a-z0-9_.]*)\}")
_HEX_DIGIT_PATTERN = re.compile(r"[0-9a-f]{64}")

# Network failures must surface as a clear message, never a raw traceback.
# URLError (incl. HTTPError, connection, and TLS failures) and read-time
# timeouts/reset errors are all reported as "could not be fetched".
_FETCH_ERRORS = (URLError, TimeoutError, http.client.HTTPException, OSError)
_FETCH_ATTEMPTS = 3
_FETCH_BACKOFF_SECONDS = 2.0

# url -> sha256 hex digest. Injectable so the test suite never touches the
# network.
Fetcher = Callable[[str], str]


class ProvenanceError(RuntimeError):
    """Raised for manifest/rendering problems that prevent verification."""


def _render_url(template: str, spec: dict[str, Any]) -> str:
    version = spec.get("version")
    if isinstance(version, str):
        # Lima provisioning strips a leading 'v' before building the asset URL;
        # mirror that so a 'v'-prefixed version
        # does not 404 a perfectly good release.
        version = version.removeprefix("v")
    values: dict[str, Any] = {"version": version}
    artifacts = spec.get("artifacts")
    if isinstance(artifacts, dict):
        for arch, artifact in artifacts.items():
            if isinstance(artifact, dict):
                values[f"artifacts.{arch}.version"] = artifact.get("version")
    agent_skill = spec.get("agent_skill")
    if isinstance(agent_skill, dict):
        values["agent_skill.commit"] = agent_skill.get("commit")

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        value = values.get(key)
        if not isinstance(value, str) or not value:
            raise ProvenanceError(f"provenance template references unset '{key}'")
        return value

    rendered = _PLACEHOLDER_PATTERN.sub(replace, template)
    parsed = urlsplit(rendered)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ProvenanceError("provenance URL must be an absolute HTTPS URL")
    return rendered


def _stored_digest(spec: dict[str, Any], field: str) -> str | None:
    value: Any = spec
    for component in field.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(component)
    return value if isinstance(value, str) else None


def _iter_entries(
    tools: dict[str, Any],
) -> Iterator[tuple[str, dict[str, Any], str, str]]:
    for name, spec in tools.items():
        if not isinstance(spec, dict):
            continue
        has_tool_sha256 = spec.get("integrity") == "sha256"
        agent_skill = spec.get("agent_skill")
        has_skill_sha256 = (
            isinstance(agent_skill, dict) and agent_skill.get("integrity") == "sha256"
        )
        if not has_tool_sha256 and not has_skill_sha256:
            continue
        provenance = spec.get("provenance")
        if not isinstance(provenance, dict):
            continue
        templates = provenance.get("url_templates")
        if not isinstance(templates, dict):
            continue
        for field, template in templates.items():
            if not isinstance(template, str):
                raise ProvenanceError(
                    f"tool '{name}' provenance.url_templates['{field}'] "
                    "must be a URL string"
                )
            yield name, spec, field, template


def load_manifest(manifest_path: Path) -> dict[str, Any]:
    try:
        data = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ProvenanceError(f"Unable to load {manifest_path}: {exc}") from exc
    tools = data.get("tools")
    if not isinstance(tools, dict):
        raise ProvenanceError(f"{manifest_path} must contain a 'tools' object")
    _validate_integrity_policies(tools, manifest_path)
    return tools


def _validate_integrity_policies(tools: dict[str, Any], manifest_path: Path) -> None:
    """Reject missing or contradictory policies instead of skipping silently."""

    required_arches = {"amd64", "arm64"}
    for name, spec in tools.items():
        if not isinstance(spec, dict):
            raise ProvenanceError(f"{manifest_path}: tool '{name}' must be an object")
        policy = spec.get("integrity")
        if not isinstance(policy, str) or policy not in {"sha256", "version-only"}:
            raise ProvenanceError(
                f"{manifest_path}: tool '{name}' has a missing or unknown "
                "integrity policy; expected 'sha256' or 'version-only'"
            )

        required: set[str] = set()
        artifacts = spec.get("artifacts")
        if policy == "sha256":
            if not isinstance(artifacts, dict) or set(artifacts) != required_arches:
                raise ProvenanceError(
                    f"{manifest_path}: tool '{name}' SHA-256 integrity must declare "
                    "amd64 and arm64 artifact records"
                )
            for arch, artifact in artifacts.items():
                if not isinstance(artifact, dict) or not isinstance(
                    artifact.get("sha256"), str
                ):
                    raise ProvenanceError(
                        f"{manifest_path}: tool '{name}' artifacts['{arch}'] is "
                        "missing its SHA-256 digest"
                    )
                required.add(f"artifacts.{arch}.sha256")
        elif "artifacts" in spec:
            raise ProvenanceError(
                f"{manifest_path}: tool '{name}' declares artifacts with "
                "version-only integrity"
            )

        agent_skill = spec.get("agent_skill")
        if agent_skill is not None:
            if not isinstance(agent_skill, dict):
                raise ProvenanceError(
                    f"{manifest_path}: tool '{name}' agent_skill must be an object"
                )
            skill_policy = agent_skill.get("integrity")
            if not isinstance(skill_policy, str) or skill_policy not in {
                "sha256",
                "version-only",
            }:
                raise ProvenanceError(
                    f"{manifest_path}: tool '{name}' agent_skill has a missing or "
                    "unknown integrity policy"
                )
            if skill_policy == "sha256":
                if not isinstance(agent_skill.get("sha256"), str):
                    raise ProvenanceError(
                        f"{manifest_path}: tool '{name}' SHA-256 agent_skill is "
                        "missing its digest"
                    )
                required.add("agent_skill.sha256")
            elif "sha256" in agent_skill:
                raise ProvenanceError(
                    f"{manifest_path}: tool '{name}' version-only agent_skill "
                    "must not declare a SHA-256 digest"
                )

        provenance = spec.get("provenance")
        if required:
            templates = (
                provenance.get("url_templates")
                if isinstance(provenance, dict)
                else None
            )
            if not isinstance(templates, dict) or set(templates) != required:
                raise ProvenanceError(
                    f"{manifest_path}: tool '{name}' provenance URL templates must "
                    "match its SHA-256-managed fields exactly"
                )
            if any(not isinstance(url, str) for url in templates.values()):
                raise ProvenanceError(
                    f"{manifest_path}: tool '{name}' provenance URLs must be strings"
                )
        elif "provenance" in spec:
            raise ProvenanceError(
                f"{manifest_path}: tool '{name}' has provenance without any "
                "SHA-256-managed fields"
            )


def _fetch_once(url: str) -> str:
    request = Request(url, headers={"User-Agent": "devbox-provenance"})
    digest = hashlib.sha256()
    with urlopen(request, timeout=60) as response:  # noqa: S310 - manifest-pinned https URLs
        for chunk in iter(lambda: response.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _no_retry(reason: BaseException) -> bool:
    # A permanent client-side failure (missing asset) will not succeed on a
    # retry, so fail fast rather than hammering the registry.
    code = getattr(reason, "code", None)
    return isinstance(reason, http.client.HTTPException) or (
        isinstance(code, int) and 400 <= code < 500
    )


def fetch_sha256(url: str) -> str:
    for attempt in range(1, _FETCH_ATTEMPTS + 1):
        try:
            return _fetch_once(url)
        except _FETCH_ERRORS as exc:
            if attempt == _FETCH_ATTEMPTS or _no_retry(exc):
                raise
            time.sleep(_FETCH_BACKOFF_SECONDS * attempt)
    raise AssertionError("unreachable")  # pragma: no cover


def verify_provenance(
    manifest_path: Path = DEFAULT_MANIFEST,
    fetch: Fetcher = fetch_sha256,
) -> list[str]:
    """Return one error per checksummed field whose stored digest is stale."""

    tools = load_manifest(manifest_path)
    errors: list[str] = []
    for name, spec, field, template in _iter_entries(tools):
        stored = _stored_digest(spec, field)
        if stored is None:
            errors.append(
                f"{name}: provenance.url_templates['{field}'] has no matching "
                "value in the manifest"
            )
            continue
        try:
            url = _render_url(template, spec)
        except ProvenanceError as exc:
            errors.append(f"{name}: {exc}")
            continue
        try:
            expected = fetch(url)
        except _FETCH_ERRORS as exc:
            errors.append(
                f"{name}: {field} could not be fetched at {url}: {exc}. This is an "
                "infrastructure/unpublished-asset failure, not a checksum mismatch."
            )
            continue
        if expected != stored:
            remediation = (
                "The agent-skill commit is not Renovate-managed; verify the pinned "
                "upstream commit, then refresh its digest with "
                "scripts/verify_provenance.py --update."
                if field == "agent_skill.sha256"
                else "The checksum-managed Renovate artifact update should refresh "
                "it; check that the matching release asset is available."
            )
            errors.append(
                f"{name}: {field} is stale for this version. Stored {stored} "
                f"but the asset at {url} hashes to {expected}. {remediation}"
            )
    return errors


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(content)
    os.replace(tmp, path)


def _bounded_digest(stored: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![0-9a-f]){re.escape(stored)}(?![0-9a-f])")


def update_provenance(
    manifest_path: Path = DEFAULT_MANIFEST,
    fetch: Fetcher = fetch_sha256,
) -> list[tuple[str, str, str, str]]:
    """Rewrite stale digests in place; return (tool, field, old, new) changes."""

    text = manifest_path.read_text()
    tools = load_manifest(manifest_path)
    changes: list[tuple[str, str, str, str]] = []
    for name, spec, field, template in _iter_entries(tools):
        stored = _stored_digest(spec, field)
        if stored is None:
            raise ProvenanceError(
                f"tool '{name}' provenance.url_templates['{field}'] has no "
                "matching manifest value to update"
            )
        url = _render_url(template, spec)
        try:
            expected = fetch(url)
        except _FETCH_ERRORS as exc:
            raise ProvenanceError(
                f"tool '{name}' {field} could not be fetched at {url}: {exc}"
            ) from exc
        if expected == stored:
            continue
        if not _HEX_DIGIT_PATTERN.fullmatch(stored):
            raise ProvenanceError(
                f"tool '{name}' {field} stored value is not a SHA-256 hex "
                "digest; refusing an ambiguous rewrite"
            )
        pattern = _bounded_digest(stored)
        occurrences = len(pattern.findall(text))
        if occurrences != 1:
            raise ProvenanceError(
                f"tool '{name}' {field} stored digest appears {occurrences} "
                "times; refusing an ambiguous rewrite"
            )
        text = pattern.sub(expected, text, count=1)
        changes.append((name, field, stored, expected))

    if changes:
        _atomic_write(manifest_path, text)
    return changes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify SHA-256-managed artifacts in the tool manifest."
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="Path to tool-versions.json (defaults to the repo manifest).",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Rewrite stale digests in the manifest instead of only checking.",
    )
    args = parser.parse_args(argv)

    try:
        if args.update:
            changes = update_provenance(args.manifest)
            if not changes:
                print(
                    "No SHA-256-managed digest needed updating; version-only "
                    "entries are not checked or refreshed."
                )
                return 0
            for name, field, old, new in changes:
                print(f"Updated {name} {field}: {old[:12]}... -> {new[:12]}...")
            print(f"Refreshed {len(changes)} provenance digest(s).")
            return 0

        errors = verify_provenance(args.manifest)
    except ProvenanceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(
        "SHA-256-managed artifact digests match their pinned release assets; "
        "version-only entries are intentionally not checked."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
