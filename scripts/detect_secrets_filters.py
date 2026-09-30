"""Repository-specific filters for the detect-secrets pre-commit hook."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MANIFEST_PATH = (_REPO_ROOT / "lima" / "tool-versions.json").resolve()
_LIMA_SCRIPT_DIR = (_REPO_ROOT / "lima").resolve()
_CHECKSUM_OBJECT_PATTERN = re.compile(r'"checksums"\s*:\s*\{(?P<body>[^{}]*)\}')
_CHECKSUM_PAIR_PATTERN = re.compile(
    r'"[^"]+"\s*:\s*"(?P<digest>[0-9a-f]{64})"',
    re.IGNORECASE,
)
_CHECKSUM_BLOCK_PATTERN = re.compile(r'"checksums"\s*:\s*\{\s*$')
_TRUST_ANCHOR_BLOCK_PATTERN = re.compile(r'"trust_anchors"\s*:\s*\{\s*$')
_OBJECT_END_PATTERN = re.compile(r"^\s*}\s*,?\s*$")


def _is_manifest(filename: str) -> bool:
    path = Path(filename)
    try:
        return path.resolve() == _MANIFEST_PATH
    except (OSError, RuntimeError):
        return path == Path("lima/tool-versions.json")


def _is_lima_script(filename: str) -> bool:
    path = Path(filename)
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        resolved = None
    if resolved is not None and resolved.parent == _LIMA_SCRIPT_DIR:
        return resolved.suffix == ".sh"
    parts = path.parts
    return len(parts) == 2 and parts[0] == "lima" and parts[1].endswith(".sh")


def _is_hex_detector(plugin: Any) -> bool:
    return plugin.__class__.__name__ == "HexHighEntropyString"


@lru_cache(maxsize=1)
def _manifest_checksum_lines() -> dict[str, frozenset[str]]:
    """Return exact manifest lines containing declared integrity digests."""

    try:
        lines = _MANIFEST_PATH.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return {}

    digests_by_line: dict[str, set[str]] = {}
    in_checksum_block = False
    for line in lines:
        checksum_object = _CHECKSUM_OBJECT_PATTERN.search(line)
        if checksum_object:
            digests = {
                match.group("digest").lower()
                for match in _CHECKSUM_PAIR_PATTERN.finditer(
                    checksum_object.group("body")
                )
            }
            if digests:
                digests_by_line.setdefault(line.rstrip(), set()).update(digests)
            continue

        if _CHECKSUM_BLOCK_PATTERN.search(line) or _TRUST_ANCHOR_BLOCK_PATTERN.search(
            line
        ):
            in_checksum_block = True
            continue
        if in_checksum_block and _OBJECT_END_PATTERN.fullmatch(line):
            in_checksum_block = False
            continue
        if in_checksum_block:
            digests = {
                match.group("digest").lower()
                for match in _CHECKSUM_PAIR_PATTERN.finditer(line)
            }
            if digests:
                digests_by_line.setdefault(line.rstrip(), set()).update(digests)

    return {line: frozenset(digests) for line, digests in digests_by_line.items()}


def _line_contains_checksum(line: str, secret: str) -> bool:
    digests = _manifest_checksum_lines().get(line.rstrip(), ())
    return secret.lower() in digests


def _manifest_release_digests() -> frozenset[str]:
    """Every digest declared in checksum or trust-anchor manifest blocks."""

    digests: set[str] = set()
    for declared in _manifest_checksum_lines().values():
        digests.update(declared)
    return frozenset(digests)


def is_manifest_release_checksum(
    filename: str,
    line: str,
    secret: str | None = None,
    plugin: Any = None,
) -> bool:
    """Ignore only hex-detector findings for manifest integrity digests.

    Two file shapes carry those checksums:

    - the manifest itself, where the finding's line must be one of the
      parsed checksum/trust-anchor lines (JSON has no comments, so an inline
      ``pragma: allowlist secret`` is not an option), and
    - ``lima/*.sh`` provisioning scripts, which embed the manifest's
      checksums verbatim to verify downloads (kept in sync by
      ``scripts/validate_tool_versions.py``). Any hex finding there that
      exactly matches a manifest-declared digest is a known false
      positive; anything else still fails the hook.
    """

    if secret is None or plugin is None:
        return False
    if not _is_hex_detector(plugin):
        return False
    if _is_manifest(filename):
        return _line_contains_checksum(line, secret)
    if _is_lima_script(filename):
        return secret.lower() in _manifest_release_digests()
    return False
