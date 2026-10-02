import json

import pytest

import scripts.detect_secrets_filters as filters
from scripts.verify_provenance import update_provenance

_DIGEST = "ab" * 32


def _artifact_digest_line(digest: str) -> str:
    return f'        "sha256": "{digest}",'


def _artifact_manifest(digest: str) -> str:
    return "\n".join(
        (
            "{",
            '  "artifacts": {',
            '    "amd64": {',
            _artifact_digest_line(digest),
            "    }",
            "  }",
            "}",
            "",
        )
    )


def _agent_skill_manifest(digest: str) -> str:
    return "\n".join(
        (
            "{",
            '  "agent_skill": {',
            '    "integrity": "sha256",',
            f'    "sha256": "{digest}"',
            "  }",
            "}",
            "",
        )
    )


class HexHighEntropyString:
    pass


class OtherDetector:
    pass


@pytest.mark.unit
def test_filter_ignores_multiline_manifest_release_checksum(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    line = _artifact_digest_line(_DIGEST)
    manifest = tmp_path / "tool-versions.json"
    manifest.write_text(_artifact_manifest(_DIGEST))
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
def test_filter_ignores_manifest_trust_anchor_digest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    line = f'    "redhat-root-ca.crt": "{_DIGEST}",'
    manifest = tmp_path / "tool-versions.json"
    manifest.write_text("\n".join(("{", '  "trust_anchors": {', line, "  }", "}", "")))
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
def test_filter_does_not_ignore_unknown_trust_anchor_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    line = f'    "unknown-ca.crt": "{_DIGEST}",'
    manifest = tmp_path / "tool-versions.json"
    manifest.write_text("\n".join(("{", '  "trust_anchors": {', line, "  }", "}", "")))
    monkeypatch.setattr(filters, "_MANIFEST_PATH", manifest)
    filters._manifest_checksum_lines.cache_clear()

    try:
        assert not filters.is_manifest_release_checksum(
            str(manifest),
            line,
            _DIGEST,
            HexHighEntropyString(),
        )
    finally:
        filters._manifest_checksum_lines.cache_clear()


@pytest.mark.unit
def test_filter_ignores_artifact_sha256_in_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    line = _artifact_digest_line(_DIGEST)
    manifest = tmp_path / "tool-versions.json"
    manifest.write_text(_artifact_manifest(_DIGEST))
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
def test_filter_ignores_sha256_agent_skill_pin_in_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    line = f'    "sha256": "{_DIGEST}"'
    manifest = tmp_path / "tool-versions.json"
    manifest.write_text(_agent_skill_manifest(_DIGEST))
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
def test_filter_does_not_ignore_uppercase_digest_in_lima_script(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    manifest = tmp_path / "tool-versions.json"
    manifest.write_text(_artifact_manifest(_DIGEST))
    monkeypatch.setattr(filters, "_MANIFEST_PATH", manifest)
    filters._manifest_checksum_lines.cache_clear()

    try:
        assert not filters.is_manifest_release_checksum(
            "lima/provision-system.sh",
            f'sha256 "{_DIGEST.upper()}"',
            _DIGEST.upper(),
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
        "lima/tool-versions.json",
        line,
        _DIGEST,
        HexHighEntropyString(),
    )


@pytest.mark.unit
def test_filter_keeps_checksum_for_another_detector():
    line = _artifact_digest_line(_DIGEST)

    assert not filters.is_manifest_release_checksum(
        "lima/tool-versions.json",
        line,
        _DIGEST,
        OtherDetector(),
    )


@pytest.mark.unit
def test_filter_ignores_manifest_checksum_embedded_in_lima_script(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    # Keep the filter correct for any Lima consumer that explicitly embeds a
    # manifest checksum; current provisioning reads checksum fields dynamically.
    manifest = tmp_path / "tool-versions.json"
    manifest.write_text(_artifact_manifest(_DIGEST))
    monkeypatch.setattr(filters, "_MANIFEST_PATH", manifest)
    filters._manifest_checksum_lines.cache_clear()

    try:
        assert filters.is_manifest_release_checksum(
            "lima/provision-system.sh",
            f'  limactl_sha256="{_DIGEST}" ;;',
            _DIGEST,
            HexHighEntropyString(),
        )
    finally:
        filters._manifest_checksum_lines.cache_clear()


@pytest.mark.unit
def test_filter_keeps_unknown_hex_in_lima_script(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    # Only digests the manifest actually declares are allowed in lima
    # scripts; a secret hex string there must stay a finding.
    manifest = tmp_path / "tool-versions.json"
    manifest.write_text(_artifact_manifest("cd" * 32))
    monkeypatch.setattr(filters, "_MANIFEST_PATH", manifest)
    filters._manifest_checksum_lines.cache_clear()

    try:
        assert not filters.is_manifest_release_checksum(
            "lima/provision-system.sh",
            f'  api_token="{_DIGEST}"',
            _DIGEST,
            HexHighEntropyString(),
        )
    finally:
        filters._manifest_checksum_lines.cache_clear()


@pytest.mark.unit
def test_filter_keeps_checksum_outside_lima_scripts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    # The lima allowance is directory-scoped: a manifest digest embedded
    # in any other shell script still fails the hook.
    manifest = tmp_path / "tool-versions.json"
    manifest.write_text(_artifact_manifest(_DIGEST))
    monkeypatch.setattr(filters, "_MANIFEST_PATH", manifest)
    filters._manifest_checksum_lines.cache_clear()

    try:
        assert not filters.is_manifest_release_checksum(
            "scripts/provision-system.sh",
            f'  limactl_sha256="{_DIGEST}"',
            _DIGEST,
            HexHighEntropyString(),
        )
    finally:
        filters._manifest_checksum_lines.cache_clear()


@pytest.mark.unit
def test_checksum_parser_handles_each_architecture_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    digests = [f"{index:02x}" * 32 for index in range(2)]
    checksum_lines = [_artifact_digest_line(digest) for digest in digests]
    architecture_blocks = [
        f'    "{arch}": {{\n{line}\n    }}'
        for arch, line in zip(("amd64", "arm64"), checksum_lines, strict=True)
    ]
    manifest = tmp_path / "tool-versions.json"
    manifest.write_text(
        '{\n  "artifacts": {\n' + ",\n".join(architecture_blocks) + "\n  }\n}\n"
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
                        "integrity": "sha256",
                        "artifacts": {
                            "amd64": {
                                "version": "v1.0.0",
                                "sha256": old_digest,
                                "depName": "example/tool-amd64",
                                "packageName": "example/tool",
                            },
                            "arm64": {
                                "version": "v1.0.0",
                                "sha256": "de" * 32,
                                "depName": "example/tool-arm64",
                                "packageName": "example/tool",
                            },
                        },
                        "provenance": {
                            "url_templates": {
                                "artifacts.amd64.sha256": (
                                    "https://example.test/"
                                    "{artifacts.amd64.version}/tool"
                                ),
                                "artifacts.arm64.sha256": (
                                    "https://example.test/"
                                    "{artifacts.arm64.version}/tool-arm"
                                ),
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
        fetch=lambda url: {
            "https://example.test/v1.0.0/tool": refreshed_digest,
            "https://example.test/v1.0.0/tool-arm": "de" * 32,
        }[url],
    ) == [("example", "artifacts.amd64.sha256", old_digest, refreshed_digest)]

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
