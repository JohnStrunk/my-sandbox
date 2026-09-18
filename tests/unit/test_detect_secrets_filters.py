import json

import pytest

import scripts.detect_secrets_filters as filters
from scripts.verify_provenance import update_provenance

_DIGEST = "ab" * 32


class HexHighEntropyString:
    pass


class OtherDetector:
    pass


@pytest.mark.unit
def test_filter_ignores_multiline_manifest_release_checksum(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    line = f'    "amd64": "{_DIGEST}",'
    manifest = tmp_path / "tool-versions.json"
    manifest.write_text('{\n  "checksums": {\n' + line + "\n  }\n}\n")
    monkeypatch.setattr(filters, "_MANIFEST_PATH", manifest)
    filters._manifest_checksum_lines.cache_clear()

    try:
        assert filters.is_manifest_release_checksum(
            str(manifest),
            line,
            _DIGEST,
            HexHighEntropyString(),
        )
    finally:
        filters._manifest_checksum_lines.cache_clear()


@pytest.mark.unit
def test_filter_ignores_inline_manifest_release_checksum(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    line = f'  "checksums": {{"amd64": "{_DIGEST}"}}'
    manifest = tmp_path / "tool-versions.json"
    manifest.write_text("{\n" + line + "\n}\n")
    monkeypatch.setattr(filters, "_MANIFEST_PATH", manifest)
    filters._manifest_checksum_lines.cache_clear()

    try:
        assert filters.is_manifest_release_checksum(
            str(manifest),
            line,
            _DIGEST,
            HexHighEntropyString(),
        )
    finally:
        filters._manifest_checksum_lines.cache_clear()


@pytest.mark.unit
def test_filter_keeps_checksum_shaped_secret_outside_manifest():
    line = f'"token": "{_DIGEST}"'

    assert not filters.is_manifest_release_checksum(
        "config.json",
        line,
        _DIGEST,
        HexHighEntropyString(),
    )


@pytest.mark.unit
def test_filter_keeps_secret_in_manifest_outside_checksums():
    line = f'    "token": "{_DIGEST}",'

    assert not filters.is_manifest_release_checksum(
        "container/tool-versions.json",
        line,
        _DIGEST,
        HexHighEntropyString(),
    )


@pytest.mark.unit
def test_filter_keeps_checksum_for_another_detector():
    line = f'"checksums": {{"amd64": "{_DIGEST}"}}'

    assert not filters.is_manifest_release_checksum(
        "container/tool-versions.json",
        line,
        _DIGEST,
        OtherDetector(),
    )


@pytest.mark.unit
def test_checksum_parser_handles_long_and_numeric_architecture_blocks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    digests = [f"{index:02x}" * 32 for index in range(6)]
    checksum_lines = [
        f'    "{name}": "{digest}",'
        for name, digest in zip(
            ("amd64", "arm64", "386", "ppc64le", "s390x", "riscv64"),
            digests,
            strict=True,
        )
    ]
    manifest = tmp_path / "tool-versions.json"
    manifest.write_text(
        '{\n  "checksums": {\n' + "\n".join(checksum_lines) + "\n  }\n}\n"
    )
    monkeypatch.setattr(filters, "_MANIFEST_PATH", manifest)
    filters._manifest_checksum_lines.cache_clear()

    try:
        parsed = filters._manifest_checksum_lines()
        assert {digest for line in checksum_lines for digest in parsed[line]} == set(
            digests
        )
    finally:
        filters._manifest_checksum_lines.cache_clear()


@pytest.mark.unit
def test_refresh_workflow_accepts_updated_checksum(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    old_digest = "ab" * 32
    refreshed_digest = "cd" * 32
    manifest = tmp_path / "tool-versions.json"
    manifest.write_text(
        json.dumps(
            {
                "tools": {
                    "example": {
                        "version": "1.0.0",
                        "checksums": {"amd64": old_digest},
                        "provenance": {
                            "url_templates": {
                                "checksums.amd64": (
                                    "https://example.test/{version}/tool"
                                )
                            }
                        },
                    }
                }
            },
            indent=2,
        )
        + "\n"
    )

    assert update_provenance(
        manifest,
        fetch=lambda _url: refreshed_digest,
    ) == [("example", "checksums.amd64", old_digest, refreshed_digest)]

    refreshed_line = next(
        line for line in manifest.read_text().splitlines() if refreshed_digest in line
    )
    monkeypatch.setattr(filters, "_MANIFEST_PATH", manifest)
    filters._manifest_checksum_lines.cache_clear()
    try:
        assert filters.is_manifest_release_checksum(
            str(manifest),
            refreshed_line,
            refreshed_digest,
            HexHighEntropyString(),
        )
    finally:
        filters._manifest_checksum_lines.cache_clear()
