import json
from pathlib import Path

import pytest

import scripts.verify_provenance as vp
from scripts.verify_provenance import (
    ProvenanceError,
    _render_url,
    update_provenance,
    verify_provenance,
)

# Placeholder digests are built by repetition so the source never carries a
# realistic high-entropy hex secret (and to keep them clearly non-secret).
_VERSION = "0.45.3"
_COMMIT = "ab" * 20  # 40-char git-style hex
_OLD_ASSET = "ab" * 32  # stored amd64 sha256
_ARM_ASSET = "cd" * 32  # stored arm64 sha256 (kept current)
_OLD_SKILL = "ef" * 32  # stored agent-skill sha256 (kept current)
_NEW_ASSET = "12" * 32  # recomputed amd64 sha256

_ASSET_TEMPLATE = (
    "https://github.com/ast-grep/ast-grep/releases/download/"
    "{version}/app-x86_64-unknown-linux-gnu.zip"
)
_ARM_TEMPLATE = (
    "https://github.com/ast-grep/ast-grep/releases/download/"
    "{version}/app-aarch64-unknown-linux-gnu.zip"
)
_SKILL_TEMPLATE = (
    "https://github.com/ast-grep/agent-skill/archive/{agent_skill.commit}.tar.gz"
)
_ASSET_URL = (
    "https://github.com/ast-grep/ast-grep/releases/download/"
    f"{_VERSION}/app-x86_64-unknown-linux-gnu.zip"
)
_ARM_URL = (
    "https://github.com/ast-grep/ast-grep/releases/download/"
    f"{_VERSION}/app-aarch64-unknown-linux-gnu.zip"
)
_SKILL_URL = f"https://github.com/ast-grep/agent-skill/archive/{_COMMIT}.tar.gz"


def _spec(*, asset: str, skill: str) -> dict:
    return {
        "version": _VERSION,
        "checksums": {"amd64": asset, "arm64": _ARM_ASSET},
        "agent_skill": {"commit": _COMMIT, "sha256": skill},
        "provenance": {
            "url_templates": {
                "checksums.amd64": _ASSET_TEMPLATE,
                "checksums.arm64": _ARM_TEMPLATE,
                "agent_skill.sha256": _SKILL_TEMPLATE,
            }
        },
        "consumers": {"docker": True},
    }


def _write_manifest(path: Path, spec: dict) -> None:
    path.write_text(json.dumps({"tools": {"ast_grep": spec}}, indent=2) + "\n")


def _fake_fetch(mapping: dict[str, str]):
    def fetch(url: str) -> str:
        return mapping[url]

    return fetch


def _current_fetch(amd64: str = _OLD_ASSET) -> dict[str, str]:
    return {_ASSET_URL: amd64, _ARM_URL: _ARM_ASSET, _SKILL_URL: _OLD_SKILL}


@pytest.mark.unit
def test_render_url_substitutes_placeholders():
    spec = _spec(asset=_OLD_ASSET, skill=_OLD_SKILL)
    assert _render_url(_ASSET_TEMPLATE, spec) == _ASSET_URL
    assert _render_url(_SKILL_TEMPLATE, spec) == _SKILL_URL


@pytest.mark.unit
def test_render_url_rejects_unknown_placeholder():
    spec = _spec(asset=_OLD_ASSET, skill=_OLD_SKILL)
    with pytest.raises(ProvenanceError):
        _render_url("https://example.test/{bogus}", spec)


@pytest.mark.unit
def test_verify_provenance_passes_when_current(tmp_path: Path):
    manifest = tmp_path / "tool-versions.json"
    _write_manifest(manifest, _spec(asset=_OLD_ASSET, skill=_OLD_SKILL))

    errors = verify_provenance(manifest, fetch=_fake_fetch(_current_fetch()))

    assert errors == []


@pytest.mark.unit
def test_verify_provenance_flags_stale_digest(tmp_path: Path):
    manifest = tmp_path / "tool-versions.json"
    _write_manifest(manifest, _spec(asset=_OLD_ASSET, skill=_OLD_SKILL))

    errors = verify_provenance(
        manifest, fetch=_fake_fetch(_current_fetch(amd64=_NEW_ASSET))
    )

    assert len(errors) == 1
    assert "ast_grep" in errors[0]
    assert "checksums.amd64" in errors[0]
    assert _NEW_ASSET in errors[0]


@pytest.mark.unit
def test_update_provenance_rewrites_only_stale_values(tmp_path: Path):
    manifest = tmp_path / "tool-versions.json"
    _write_manifest(manifest, _spec(asset=_OLD_ASSET, skill=_OLD_SKILL))
    original = manifest.read_text()

    changes = update_provenance(
        manifest, fetch=_fake_fetch(_current_fetch(amd64=_NEW_ASSET))
    )

    assert changes == [("ast_grep", "checksums.amd64", _OLD_ASSET, _NEW_ASSET)]
    updated = manifest.read_text()
    assert _NEW_ASSET in updated
    assert _OLD_ASSET not in updated
    # The skill pin (current) and the arm64 checksum (current) are untouched.
    assert _OLD_SKILL in updated
    assert _ARM_ASSET in updated
    assert updated == original.replace(_OLD_ASSET, _NEW_ASSET)
    assert json.loads(updated)  # still valid JSON


@pytest.mark.unit
def test_update_provenance_no_op_when_current(tmp_path: Path):
    manifest = tmp_path / "tool-versions.json"
    _write_manifest(manifest, _spec(asset=_OLD_ASSET, skill=_OLD_SKILL))
    original = manifest.read_text()

    changes = update_provenance(manifest, fetch=_fake_fetch(_current_fetch()))

    assert changes == []
    assert manifest.read_text() == original


@pytest.mark.unit
def test_update_provenance_refuses_ambiguous_digest(tmp_path: Path):
    # Both arch checksums carry the same stored value, so a single stored
    # digest appears twice and an in-place rewrite would be ambiguous.
    manifest = tmp_path / "tool-versions.json"
    spec = _spec(asset=_OLD_ASSET, skill=_OLD_SKILL)
    spec["checksums"]["arm64"] = _OLD_ASSET
    _write_manifest(manifest, spec)

    with pytest.raises(ProvenanceError, match="appears 2 times"):
        update_provenance(
            manifest,
            fetch=_fake_fetch(
                {
                    _ASSET_URL: _NEW_ASSET,
                    _ARM_URL: _NEW_ASSET,
                    _SKILL_URL: _OLD_SKILL,
                }
            ),
        )


@pytest.mark.unit
def test_verify_provenance_reports_fetch_failure_not_traceback(tmp_path: Path):
    manifest = tmp_path / "tool-versions.json"
    _write_manifest(manifest, _spec(asset=_OLD_ASSET, skill=_OLD_SKILL))

    def boom(url: str) -> str:
        raise OSError("network unreachable")

    errors = verify_provenance(manifest, fetch=boom)

    # Every checksummed field is reported as an infrastructure failure rather
    # than raising out of the command.
    assert len(errors) == 3
    assert all("could not be fetched" in error for error in errors)


@pytest.mark.unit
def test_update_provenance_raises_on_fetch_failure(tmp_path: Path):
    manifest = tmp_path / "tool-versions.json"
    _write_manifest(manifest, _spec(asset=_NEW_ASSET, skill=_OLD_SKILL))

    def boom(url: str) -> str:
        raise OSError("network unreachable")

    with pytest.raises(ProvenanceError, match="could not be fetched"):
        update_provenance(manifest, fetch=boom)
    # A failed update must never have touched the file.
    assert (
        manifest.read_text()
        == json.dumps(
            {"tools": {"ast_grep": _spec(asset=_NEW_ASSET, skill=_OLD_SKILL)}},
            indent=2,
        )
        + "\n"
    )


@pytest.mark.unit
def test_main_exit_code_zero_when_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    manifest = tmp_path / "tool-versions.json"
    _write_manifest(manifest, _spec(asset=_OLD_ASSET, skill=_OLD_SKILL))
    monkeypatch.setattr(vp, "verify_provenance", lambda path: [])
    assert vp.main(["--manifest", str(manifest)]) == 0


@pytest.mark.unit
def test_main_exit_code_one_when_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    manifest = tmp_path / "tool-versions.json"
    _write_manifest(manifest, _spec(asset=_OLD_ASSET, skill=_OLD_SKILL))
    monkeypatch.setattr(vp, "verify_provenance", lambda path: ["stale checksum"])
    assert vp.main(["--manifest", str(manifest)]) == 1


@pytest.mark.unit
def test_main_exit_code_two_on_provenance_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    manifest = tmp_path / "tool-versions.json"

    def raise_provenance(path):
        raise ProvenanceError("bad manifest")

    monkeypatch.setattr(vp, "verify_provenance", raise_provenance)
    assert vp.main(["--manifest", str(manifest)]) == 2


@pytest.mark.unit
def test_main_update_exit_code_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    manifest = tmp_path / "tool-versions.json"

    def fake_update(path):
        return [("ast_grep", "checksums.amd64", "a", "b")]

    monkeypatch.setattr(vp, "update_provenance", fake_update)
    assert vp.main(["--update", "--manifest", str(manifest)]) == 0
