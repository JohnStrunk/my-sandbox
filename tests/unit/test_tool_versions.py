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
    "lima/provision-system.sh",
    "lima/provision-user.sh",
    "lima/probe-readiness.sh",
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


@pytest.mark.unit
def test_opencode_manifest_pins_the_v2_npm_cli(repo_root: Path):
    manifest = json.loads((repo_root / "container" / "tool-versions.json").read_text())
    opencode = manifest["tools"]["opencode"]

    assert opencode["version"].startswith("2.")
    assert opencode["datasource"] == "npm"
    assert opencode["depName"] == "@opencode/cli"


def _edit_manifest(copy_root: Path, mutate) -> None:
    manifest_path = copy_root / "container" / "tool-versions.json"
    data = json.loads(manifest_path.read_text())
    mutate(data)
    manifest_path.write_text(json.dumps(data, indent=2) + "\n")


@pytest.mark.unit
def test_validator_flags_malformed_checksum(repo_root: Path, tmp_path: Path):
    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)

    def mutate(data):
        data["tools"]["ast_grep"]["checksums"]["amd64"] = "deadbeef"

    _edit_manifest(copy_root, mutate)

    errors = validate_tool_versions(copy_root)

    assert any(
        "ast_grep" in error and "64-char" in error and "amd64" in error
        for error in errors
    )


@pytest.mark.unit
def test_validator_flags_missing_provenance(repo_root: Path, tmp_path: Path):
    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)

    def mutate(data):
        del data["tools"]["ast_grep"]["provenance"]

    _edit_manifest(copy_root, mutate)

    errors = validate_tool_versions(copy_root)

    assert any("ast_grep" in error and "provenance" in error for error in errors)


@pytest.mark.unit
def test_validator_flags_dangling_provenance_template(repo_root: Path, tmp_path: Path):
    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)

    def mutate(data):
        data["tools"]["ast_grep"]["provenance"]["url_templates"]["checksums.riscv"] = (
            "https://example.invalid/riscv.zip"
        )

    _edit_manifest(copy_root, mutate)

    errors = validate_tool_versions(copy_root)

    assert any(
        "ast_grep" in error and "riscv" in error and "no matching" in error
        for error in errors
    )


@pytest.mark.unit
def test_validator_flags_provenance_dockerfile_drift(repo_root: Path, tmp_path: Path):
    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)

    dockerfile_path = copy_root / "container" / "Dockerfile"
    dockerfile_path.write_text(
        dockerfile_path.read_text().replace(
            "ast_grep_checksum=\"$(jq -er '.tools.ast_grep.checksums.arm64' "
            '/tmp/devbox-tool-versions.json)" ;;',
            'ast_grep_checksum="unused" ;;',
            1,
        )
    )

    errors = validate_tool_versions(copy_root)

    assert any("arm64" in error and "never reads it" in error for error in errors)


@pytest.mark.unit
def test_validator_flags_unknown_manifest_field(repo_root: Path, tmp_path: Path):
    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)

    def mutate(data):
        data["tools"]["ast_grep"]["chevkcsums"] = {"amd64": "x"}

    _edit_manifest(copy_root, mutate)

    errors = validate_tool_versions(copy_root)

    assert any("ast_grep" in error and "unknown field" in error for error in errors)


@pytest.mark.unit
def test_manifest_declares_lima_consumers(repo_root: Path):
    manifest = json.loads((repo_root / "container" / "tool-versions.json").read_text())

    for tool in ("opencode", "uv", "limactl"):
        consumers = manifest["tools"][tool]["consumers"]
        assert consumers.get("lima") is True, tool


@pytest.mark.unit
def test_tool_version_validator_reports_lima_pin_drift(repo_root: Path, tmp_path: Path):
    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)

    script = copy_root / "lima" / "provision-system.sh"
    script.write_text(
        script.read_text().replace(
            'OPENCODE_VERSION="2.0.16"', 'OPENCODE_VERSION="2.0.15"', 1
        )
    )

    errors = validate_tool_versions(copy_root)

    assert any("pin for 'opencode'" in error and "2.0.15" in error for error in errors)


@pytest.mark.unit
def test_tool_version_validator_reports_missing_lima_pin(
    repo_root: Path, tmp_path: Path
):
    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)

    script = copy_root / "lima" / "provision-system.sh"
    script.write_text(
        "\n".join(
            line
            for line in script.read_text().splitlines()
            if not line.startswith("LIMACTL_VERSION=")
        )
        + "\n"
    )

    errors = validate_tool_versions(copy_root)

    assert any("does not pin 'limactl'" in error for error in errors)


@pytest.mark.unit
def test_tool_version_validator_rejects_undeclared_lima_pin(
    repo_root: Path, tmp_path: Path
):
    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)

    script = copy_root / "lima" / "provision-user.sh"
    script.write_text(script.read_text() + 'TOTALLY_NEW_TOOL_VERSION="1.2.3"\n')

    errors = validate_tool_versions(copy_root)

    assert any("totally_new_tool" in error for error in errors)


@pytest.mark.unit
def test_tool_version_validator_checks_lima_renovate_metadata(
    repo_root: Path, tmp_path: Path
):
    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)

    script = copy_root / "lima" / "provision-system.sh"
    script.write_text(
        script.read_text().replace(
            "# renovate: datasource=github-releases depName=lima-vm/lima",
            "# renovate: datasource=github-releases depName=wrong/tool",
            1,
        )
    )

    errors = validate_tool_versions(copy_root)

    assert any("renovate" in error and "limactl" in error for error in errors)


@pytest.mark.unit
def test_tool_version_validator_flags_duplicate_lima_pin(
    repo_root: Path, tmp_path: Path
):
    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)

    # A second LIMACTL_VERSION anywhere in lima/*.sh is ambiguous, even
    # when it matches the manifest.
    script = copy_root / "lima" / "provision-user.sh"
    script.write_text(script.read_text() + 'LIMACTL_VERSION="2.2.0"\n')

    errors = validate_tool_versions(copy_root)

    assert any("pins 'limactl' more than once" in error for error in errors)


@pytest.mark.unit
def test_tool_version_validator_rejects_pin_of_non_lima_tool(
    repo_root: Path, tmp_path: Path
):
    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)

    # hadolint is in the manifest, but only as a docker/ci/pre-commit
    # consumer; the VM scripts must not pin it.
    script = copy_root / "lima" / "provision-user.sh"
    script.write_text(script.read_text() + 'HADOLINT_VERSION="2.15.1"\n')

    errors = validate_tool_versions(copy_root)

    assert any(
        "pins 'hadolint'" in error and "'lima' consumer" in error for error in errors
    )


@pytest.mark.unit
def test_tool_version_validator_ignores_commented_lima_pin(
    repo_root: Path, tmp_path: Path
):
    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)

    # A commented-out pin must not count (it must not mask a missing or
    # duplicate live pin either).
    script = copy_root / "lima" / "provision-system.sh"
    script.write_text(script.read_text() + '# OPENCODE_VERSION="9.9.9"\n')

    assert validate_tool_versions(copy_root) == []


@pytest.mark.unit
def test_tool_version_validator_accepts_v_prefixed_lima_pin(
    repo_root: Path, tmp_path: Path
):
    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)

    script = copy_root / "lima" / "provision-system.sh"
    script.write_text(
        script.read_text().replace(
            'OPENCODE_VERSION="2.0.16"', 'OPENCODE_VERSION="v2.0.16"', 1
        )
    )

    assert validate_tool_versions(copy_root) == []


@pytest.mark.unit
def test_tool_version_validator_flags_missing_lima_checksum(
    repo_root: Path, tmp_path: Path
):
    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)

    # Corrupt the script's embedded copy of the manifest-declared amd64
    # digest: the download would no longer be verified against the
    # manifest.
    manifest = json.loads((copy_root / "container" / "tool-versions.json").read_text())
    digest = manifest["tools"]["limactl"]["checksums"]["amd64"]
    script = copy_root / "lima" / "provision-system.sh"
    script.write_text(script.read_text().replace(digest, "00" * 32, 1))

    errors = validate_tool_versions(copy_root)

    assert any(
        "must embed the 'limactl' amd64 release checksum" in error for error in errors
    )


@pytest.mark.unit
def test_validator_allows_checksums_for_non_docker_consumers(
    repo_root: Path, tmp_path: Path
):
    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)

    # Checksums only need to be read by an installing consumer. yamllint
    # is consumed by pre-commit alone, so a Dockerfile read is not
    # required for its metadata (the lima scripts play that role for
    # lima-only tools such as limactl). A fully valid non-docker entry
    # with checksums + provenance passes validation end to end.
    def mutate(data):
        data["tools"]["yamllint"]["checksums"] = {"amd64": "ab" * 32}
        data["tools"]["yamllint"]["provenance"] = {
            "url_templates": {
                "checksums.amd64": "https://example.test/yamllint-{version}"
            }
        }

    _edit_manifest(copy_root, mutate)

    assert validate_tool_versions(copy_root) == []
