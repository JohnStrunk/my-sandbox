import json
import shutil
from pathlib import Path

import pytest

from scripts.validate_tool_versions import validate_tool_versions

_VALIDATOR_INPUTS = [
    "container/tool-versions.json",
    "container/Dockerfile",
    ".github/workflows/ci-workflow.yaml",
    ".pre-commit-config.yaml",
    "pyproject.toml",
    "uv.lock",
]


def _copy_validator_inputs(repo_root: Path, copy_root: Path) -> None:
    for relative_path in _VALIDATOR_INPUTS:
        source = repo_root / relative_path
        destination = copy_root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


@pytest.mark.unit
def test_tool_version_manifest_is_synchronized(repo_root: Path):
    assert validate_tool_versions(repo_root) == []


@pytest.mark.unit
def test_tool_version_validator_reports_pre_commit_drift(
    repo_root: Path, tmp_path: Path
):
    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)

    pre_commit_path = copy_root / ".pre-commit-config.yaml"
    pre_commit_path.write_text(
        pre_commit_path.read_text().replace("rev: v2.15.1", "rev: v2.15.0", 1)
    )

    errors = validate_tool_versions(copy_root)

    assert any("hadolint" in error and "2.15.0" in error for error in errors)


@pytest.mark.unit
def test_tool_version_validator_rejects_hardcoded_consumer_versions(
    repo_root: Path, tmp_path: Path
):
    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)

    dockerfile_path = copy_root / "container" / "Dockerfile"
    dockerfile_path.write_text(
        dockerfile_path.read_text().replace(
            '"markdownlint-cli2@${markdownlint_version}"',
            '"markdownlint-cli2@0.23.1"',
            1,
        )
    )

    errors = validate_tool_versions(copy_root)

    assert any("hard-coded version" in error for error in errors)


@pytest.mark.unit
def test_tool_version_manifest_contains_renovate_metadata(repo_root: Path):
    manifest = json.loads((repo_root / "container" / "tool-versions.json").read_text())

    assert manifest["tools"]
    for name, spec in manifest["tools"].items():
        assert spec["version"], name
        assert spec["datasource"], name
        assert spec["depName"], name
        assert spec["consumers"], name
