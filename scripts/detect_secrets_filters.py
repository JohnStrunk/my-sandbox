"""Repository-specific filters for the detect-secrets pre-commit hook."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MANIFEST_PATH = (_REPO_ROOT / "container" / "tool-versions.json").resolve()
_CHECKSUM_OBJECT_PATTERN = re.compile(r'"checksums"\s*:\s*\{(?P<body>[^{}]*)\}')
_CHECKSUM_PAIR_PATTERN = re.compile(
    r'"[^"]+"\s*:\s*"(?P<digest>[0-9a-f]{64})"',
    re.IGNORECASE,
)
_CHECKSUM_BLOCK_PATTERN = re.compile(r'"checksums"\s*:\s*\{\s*$')
_OBJECT_END_PATTERN = re.compile(r"^\s*}\s*,?\s*$")


def _is_manifest(filename: str) -> bool:
    path = Path(filename)
    try:
        return path.resolve() == _MANIFEST_PATH
    except (OSError, RuntimeError):
        return path == Path("container/tool-versions.json")


def _is_hex_detector(plugin: Any) -> bool:
    return plugin.__class__.__name__ == "HexHighEntropyString"


@lru_cache(maxsize=1)
def _manifest_checksum_lines() -> dict[str, frozenset[str]]:
    """Return the exact manifest lines that contain declared checksums."""

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

        if _CHECKSUM_BLOCK_PATTERN.search(line):
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


def is_manifest_release_checksum(
    filename: str,
    line: str,
    secret: str | None = None,
    plugin: Any = None,
) -> bool:
    """Ignore only hex-detector findings for manifest release checksums."""

    if secret is None or plugin is None:
        return False

    return (
        _is_manifest(filename)
        and _is_hex_detector(plugin)
        and _line_contains_checksum(line, secret)
    )
