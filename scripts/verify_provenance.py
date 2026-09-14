#!/usr/bin/env python3
"""Verify and refresh the release-provenance checksums in the tool manifest.

The manifest declares, for each tool that carries release checksums or an
agent-skill archive pin, a ``provenance.url_templates`` block mapping every
checksummed field to the URL of the upstream asset it must hash to. Renovate
bumps only ``.version``, so a version-only update can leave the stored
checksums stale while the change still "looks" buildable.

* default mode recomputes each declared asset's SHA-256 and reports any field
  whose stored digest no longer matches (a stale checksum/skill pin).
* ``--update`` recomputes and rewrites the stale digests in place so that the
  version, platform checksums, and agent-skill pin are always refreshed as one
  unit, preserving the file's existing formatting.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "container" / "tool-versions.json"

_PLACEHOLDER_PATTERN = re.compile(r"\{([a-z][a-z0-9_.]*)\}")
_HEX_DIGIT_PATTERN = re.compile(r"[0-9a-f]{64}")

# url -> sha256 hex digest. Injectable so the test suite never touches the
# network.
Fetcher = Callable[[str], str]


class ProvenanceError(RuntimeError):
    """Raised for manifest/rendering problems that prevent verification."""


def _render_url(template: str, spec: dict[str, Any]) -> str:
    values: dict[str, Any] = {"version": spec.get("version")}
    agent_skill = spec.get("agent_skill")
    if isinstance(agent_skill, dict):
        values["agent_skill.commit"] = agent_skill.get("commit")

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        value = values.get(key)
        if not isinstance(value, str) or not value:
            raise ProvenanceError(f"provenance template references unset '{key}'")
        return value

    return _PLACEHOLDER_PATTERN.sub(replace, template)


def _stored_digest(spec: dict[str, Any], field: str) -> str | None:
    kind, _, sub = field.partition(".")
    container = spec.get(kind)
    if isinstance(container, dict):
        value = container.get(sub)
        if isinstance(value, str):
            return value
    return None


def _iter_entries(
    tools: dict[str, Any],
) -> Iterator[tuple[str, dict[str, Any], str, str]]:
    for name, spec in tools.items():
        if not isinstance(spec, dict):
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
    return tools


def fetch_sha256(url: str) -> str:
    request = Request(url, headers={"User-Agent": "devbox-provenance"})
    digest = hashlib.sha256()
    with urlopen(request, timeout=60) as response:  # noqa: S310 - manifest-pinned https URLs
        for chunk in iter(lambda: response.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
        expected = fetch(url)
        if expected != stored:
            errors.append(
                f"{name}: {field} is stale for this version. Stored {stored} "
                f"but the asset at {url} hashes to {expected}. "
                "Run 'scripts/verify_provenance.py --update' to refresh it."
            )
    return errors


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
        expected = fetch(url)
        if expected == stored:
            continue
        if not _HEX_DIGIT_PATTERN.fullmatch(stored):
            raise ProvenanceError(
                f"tool '{name}' {field} stored value is not a SHA-256 hex "
                "digest; refusing an ambiguous rewrite"
            )
        occurrences = text.count(stored)
        if occurrences != 1:
            raise ProvenanceError(
                f"tool '{name}' {field} stored digest appears {occurrences} "
                "times; refusing an ambiguous rewrite"
            )
        text = text.replace(stored, expected)
        changes.append((name, field, stored, expected))

    if changes:
        manifest_path.write_text(text)
    return changes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify or refresh release-provenance checksums."
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
                print("Release provenance already current; nothing to update.")
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

    print("Release provenance checksums match their pinned versions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
