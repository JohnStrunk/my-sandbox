import json
from pathlib import Path

import pytest
import yaml

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
_OLD_SKILL = "ef" * 32  # stored agent-skill sha256
_OLD_ASSET = "ab" * 32  # stored amd64 sha256
_ARM_ASSET = "cd" * 32  # stored arm64 sha256 (kept current)
_NEW_ASSET = "12" * 32  # recomputed amd64 sha256
_NEW_SKILL = "34" * 32  # recomputed agent-skill sha256

_ASSET_TEMPLATE = (
    "https://github.com/ast-grep/ast-grep/releases/download/"
    "{artifacts.amd64.version}/app-x86_64-unknown-linux-gnu.zip"
)
_ARM_TEMPLATE = (
    "https://github.com/ast-grep/ast-grep/releases/download/"
    "{artifacts.arm64.version}/app-aarch64-unknown-linux-gnu.zip"
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


def _spec(*, asset: str) -> dict:
    return {
        "version": _VERSION,
        "integrity": "sha256",
        "artifacts": {
            "amd64": {
                "version": _VERSION,
                "sha256": asset,
                "depName": "ast-grep/ast-grep-amd64",
                "packageName": "ast-grep/ast-grep",
                "datasource": "github-release-attachments",
            },
            "arm64": {
                "version": _VERSION,
                "sha256": _ARM_ASSET,
                "depName": "ast-grep/ast-grep-arm64",
                "packageName": "ast-grep/ast-grep",
                "datasource": "github-release-attachments",
            },
        },
        "agent_skill": {"integrity": "version-only", "commit": _COMMIT},
        "provenance": {
            "url_templates": {
                "artifacts.amd64.sha256": _ASSET_TEMPLATE,
                "artifacts.arm64.sha256": _ARM_TEMPLATE,
            }
        },
        "consumers": {"lima": True},
    }


def _write_manifest(path: Path, spec: dict) -> None:
    path.write_text(json.dumps({"tools": {"ast_grep": spec}}, indent=2) + "\n")


def _fake_fetch(mapping: dict[str, str]):
    def fetch(url: str) -> str:
        return mapping[url]

    return fetch


def _current_fetch(amd64: str = _OLD_ASSET) -> dict[str, str]:
    return {_ASSET_URL: amd64, _ARM_URL: _ARM_ASSET}


@pytest.mark.unit
def test_render_url_substitutes_placeholders():
    spec = _spec(asset=_OLD_ASSET)
    assert _render_url(_ASSET_TEMPLATE, spec) == _ASSET_URL


@pytest.mark.unit
def test_render_url_rejects_unknown_placeholder():
    spec = _spec(asset=_OLD_ASSET)
    with pytest.raises(ProvenanceError):
        _render_url("https://example.test/{bogus}", spec)


@pytest.mark.unit
def test_render_url_rejects_non_https_template():
    spec = _spec(asset=_OLD_ASSET)
    with pytest.raises(ProvenanceError, match="absolute HTTPS URL"):
        _render_url("http://example.test/{artifacts.amd64.version}", spec)


@pytest.mark.unit
def test_verify_provenance_passes_when_current(tmp_path: Path):
    manifest = tmp_path / "tool-versions.json"
    _write_manifest(manifest, _spec(asset=_OLD_ASSET))

    errors = verify_provenance(manifest, fetch=_fake_fetch(_current_fetch()))

    assert errors == []


@pytest.mark.unit
def test_verify_provenance_flags_stale_digest(tmp_path: Path):
    manifest = tmp_path / "tool-versions.json"
    _write_manifest(manifest, _spec(asset=_OLD_ASSET))

    errors = verify_provenance(
        manifest, fetch=_fake_fetch(_current_fetch(amd64=_NEW_ASSET))
    )

    assert len(errors) == 1
    assert "ast_grep" in errors[0]
    assert "artifacts.amd64.sha256" in errors[0]
    assert _NEW_ASSET in errors[0]


@pytest.mark.unit
def test_verify_provenance_flags_and_refreshes_sha256_agent_skill(
    tmp_path: Path,
):
    spec = _spec(asset=_OLD_ASSET)
    spec["agent_skill"] = {
        "integrity": "sha256",
        "commit": _COMMIT,
        "sha256": _OLD_SKILL,
    }
    spec["provenance"]["url_templates"]["agent_skill.sha256"] = _SKILL_TEMPLATE
    manifest = tmp_path / "tool-versions.json"
    _write_manifest(manifest, spec)
    fetch = _fake_fetch({**_current_fetch(), _SKILL_URL: _NEW_SKILL})

    errors = verify_provenance(manifest, fetch=fetch)

    assert len(errors) == 1
    assert "agent_skill.sha256" in errors[0]
    assert _NEW_SKILL in errors[0]
    assert "agent-skill commit is not Renovate-managed" in errors[0]

    changes = update_provenance(manifest, fetch=fetch)

    assert changes == [("ast_grep", "agent_skill.sha256", _OLD_SKILL, _NEW_SKILL)]
    assert verify_provenance(manifest, fetch=fetch) == []


@pytest.mark.unit
def test_update_provenance_rewrites_only_stale_values(tmp_path: Path):
    manifest = tmp_path / "tool-versions.json"
    _write_manifest(manifest, _spec(asset=_OLD_ASSET))
    original = manifest.read_text()

    changes = update_provenance(
        manifest, fetch=_fake_fetch(_current_fetch(amd64=_NEW_ASSET))
    )

    assert changes == [("ast_grep", "artifacts.amd64.sha256", _OLD_ASSET, _NEW_ASSET)]
    updated = manifest.read_text()
    assert _NEW_ASSET in updated
    assert _OLD_ASSET not in updated
    # Version-only skill pins are absent from provenance verification.
    assert _ARM_ASSET in updated
    assert updated == original.replace(_OLD_ASSET, _NEW_ASSET)
    assert json.loads(updated)  # still valid JSON


@pytest.mark.unit
def test_update_provenance_no_op_when_current(tmp_path: Path):
    manifest = tmp_path / "tool-versions.json"
    _write_manifest(manifest, _spec(asset=_OLD_ASSET))
    original = manifest.read_text()

    changes = update_provenance(manifest, fetch=_fake_fetch(_current_fetch()))

    assert changes == []
    assert manifest.read_text() == original


@pytest.mark.unit
def test_version_only_node_bump_skips_checksum_verification(
    repo_root: Path, tmp_path: Path
):
    manifest_data = json.loads((repo_root / "lima" / "tool-versions.json").read_text())
    node = manifest_data["tools"]["node"]
    assert node["integrity"] == "version-only"
    assert "checksums" not in node
    assert "artifacts" not in node
    assert "provenance" not in node
    node["version"] = "99.0.0"

    manifest = tmp_path / "tool-versions.json"
    manifest.write_text(json.dumps({"tools": {"node": node}}, indent=2) + "\n")

    requested_urls: list[str] = []

    def unexpected_fetch(url: str) -> str:
        requested_urls.append(url)
        raise AssertionError(f"version-only entry fetched for checksum: {url}")

    assert verify_provenance(manifest, fetch=unexpected_fetch) == []
    assert requested_urls == []


@pytest.mark.unit
def test_update_provenance_refuses_ambiguous_digest(tmp_path: Path):
    # Both architecture records carry the same digest, so an in-place rewrite
    # would be ambiguous.
    manifest = tmp_path / "tool-versions.json"
    spec = _spec(asset=_OLD_ASSET)
    spec["artifacts"]["arm64"]["sha256"] = _OLD_ASSET
    _write_manifest(manifest, spec)

    with pytest.raises(ProvenanceError, match="appears 2 times"):
        update_provenance(
            manifest,
            fetch=_fake_fetch(
                {
                    _ASSET_URL: _NEW_ASSET,
                    _ARM_URL: _NEW_ASSET,
                }
            ),
        )


@pytest.mark.unit
def test_verify_provenance_reports_fetch_failure_not_traceback(tmp_path: Path):
    manifest = tmp_path / "tool-versions.json"
    _write_manifest(manifest, _spec(asset=_OLD_ASSET))

    def boom(url: str) -> str:
        raise OSError("network unreachable")

    errors = verify_provenance(manifest, fetch=boom)

    # Only the two SHA-256-managed binary records are fetched; the version-only
    # skill archive is explicitly skipped.
    assert len(errors) == 2
    assert all("could not be fetched" in error for error in errors)


@pytest.mark.unit
def test_update_provenance_raises_on_fetch_failure(tmp_path: Path):
    manifest = tmp_path / "tool-versions.json"
    _write_manifest(manifest, _spec(asset=_NEW_ASSET))

    def boom(url: str) -> str:
        raise OSError("network unreachable")

    with pytest.raises(ProvenanceError, match="could not be fetched"):
        update_provenance(manifest, fetch=boom)
    # A failed update must never have touched the file.
    assert (
        manifest.read_text()
        == json.dumps(
            {"tools": {"ast_grep": _spec(asset=_NEW_ASSET)}},
            indent=2,
        )
        + "\n"
    )


@pytest.mark.unit
def test_main_exit_code_zero_when_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    manifest = tmp_path / "tool-versions.json"
    _write_manifest(manifest, _spec(asset=_OLD_ASSET))
    monkeypatch.setattr(vp, "verify_provenance", lambda path: [])
    assert vp.main(["--manifest", str(manifest)]) == 0


@pytest.mark.unit
def test_main_exit_code_one_when_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    manifest = tmp_path / "tool-versions.json"
    _write_manifest(manifest, _spec(asset=_OLD_ASSET))
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
        return [("ast_grep", "artifacts.amd64.sha256", "a", "b")]

    monkeypatch.setattr(vp, "update_provenance", fake_update)
    assert vp.main(["--update", "--manifest", str(manifest)]) == 0


@pytest.mark.unit
def test_ci_and_merge_queue_gate_on_release_provenance(repo_root: Path):
    workflow = yaml.safe_load(
        (repo_root / ".github" / "workflows" / "ci-workflow.yaml").read_text()
    )
    jobs = workflow["jobs"]
    job = workflow["jobs"]["pre-commit"]
    steps = [
        step
        for step in job["steps"]
        if step.get("name") == "Verify release provenance checksums"
    ]

    assert len(steps) == 1
    assert "if" not in job
    assert "if" not in steps[0]
    assert "continue-on-error" not in job
    assert "continue-on-error" not in steps[0]
    success_job = jobs["ci-success"]
    assert success_job["if"] == "always()"
    assert set(success_job["needs"]) == set(jobs) - {"ci-success"}
    assert "pre-commit" in success_job["needs"]
    success_steps = [
        step for step in success_job["steps"] if step.get("name") == "Check all jobs"
    ]
    assert len(success_steps) == 1
    assert success_steps[0]["env"]["RESULTS"] == "${{ join(needs.*.result, ' ') }}"
    assert 'if [[ "$r" != "success" ]]; then' in success_steps[0]["run"]
    assert not any("--update" in str(step.get("run", "")) for step in job["steps"])
    assert steps[0]["run"] == "python3 scripts/verify_provenance.py"

    mergify = yaml.safe_load((repo_root / ".github" / "mergify.yml").read_text())
    queue_conditions = mergify["queue_rules"][0]["queue_conditions"]
    assert 'check-success="CI Workflow - Success"' in queue_conditions
