import json
import shutil
from pathlib import Path

import pytest

from scripts.validate_tool_versions import validate_tool_versions

_VALIDATOR_INPUTS = [
    "lima/tool-versions.json",
    ".github/workflows/ci-workflow.yaml",
    ".pre-commit-config.yaml",
    "pyproject.toml",
    "uv.lock",
    "lima/provision-system.sh",
    "lima/provision-user.sh",
    "lima/provision-tools.sh",
    "lima/probe-readiness.sh",
    "lima/certs/redhat-ipa-ca.crt",
    "lima/certs/redhat-rhcsv2-ca.crt",
    "lima/certs/redhat-root-ca.crt",
]

_CHECKSUM_MANAGED_TOOLS = {
    "acli",
    "antigravity_cli",
    "ast_grep",
    "hadolint",
    "kind",
    "limactl",
    "minikube",
    "uv",
}


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
    manifest = json.loads((copy_root / "lima" / "tool-versions.json").read_text())
    markdownlint_version = manifest["tools"]["markdownlint_cli2"]["version"]
    current_revision = f"v{markdownlint_version.removeprefix('v')}"
    drift_revision = f"{current_revision}-stale"
    pre_commit_text = pre_commit_path.read_text()
    current_rev_line = f'rev: "{current_revision}"'
    assert current_rev_line in pre_commit_text
    pre_commit_path.write_text(
        pre_commit_text.replace(current_rev_line, f'rev: "{drift_revision}"', 1)
    )

    errors = validate_tool_versions(copy_root)

    assert any(
        "markdownlint_cli2" in error and drift_revision in error for error in errors
    )


@pytest.mark.unit
def test_tool_version_validator_rejects_hardcoded_consumer_versions(
    repo_root: Path, tmp_path: Path
):
    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)

    workflow_path = copy_root / ".github" / "workflows" / "ci-workflow.yaml"
    workflow_path.write_text(
        workflow_path.read_text().replace(
            '"markdownlint-cli2@${markdownlint_version}"',
            '"markdownlint-cli2@0.23.2"',
            1,
        )
    )

    errors = validate_tool_versions(copy_root)

    assert any("hard-coded version" in error for error in errors)


@pytest.mark.unit
def test_tool_version_validator_rejects_non_version_manifest_values(
    repo_root: Path, tmp_path: Path
):
    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)

    def mutate(data):
        data["tools"]["opencode"]["version"] = "https://attacker.invalid/package.tgz"

    _edit_manifest(copy_root, mutate)

    errors = validate_tool_versions(copy_root)

    assert any(
        "opencode" in error and "safe version token" in error for error in errors
    )


@pytest.mark.unit
def test_tool_version_manifest_contains_renovate_metadata(repo_root: Path):
    manifest = json.loads((repo_root / "lima" / "tool-versions.json").read_text())

    assert manifest["tools"]
    for name, spec in manifest["tools"].items():
        assert spec["version"], name
        assert spec["datasource"], name
        assert spec["depName"], name
        assert spec["integrity"] in {"sha256", "version-only"}, name
        assert spec["consumers"], name
        assert "docker" not in spec["consumers"], name
    assert "github_mcp_server" not in manifest["tools"]


@pytest.mark.unit
def test_checksum_managed_tool_set_is_an_explicit_v1_policy(repo_root: Path):
    manifest = json.loads((repo_root / "lima" / "tool-versions.json").read_text())
    checksum_managed = {
        name
        for name, spec in manifest["tools"].items()
        if spec["integrity"] == "sha256"
    }

    assert checksum_managed == _CHECKSUM_MANAGED_TOOLS
    assert manifest["tools"]["ast_grep"]["agent_skill"]["integrity"] == "sha256"
    for tool in (
        "node",
        "go",
        "rustup",
        "helm",
        "kubectl",
        "docker_ce",
        "containerd_io",
    ):
        assert manifest["tools"][tool]["integrity"] == "version-only"
        assert "artifacts" not in manifest["tools"][tool]


@pytest.mark.unit
@pytest.mark.parametrize("policy", [None, "md5", ""])
def test_validator_requires_explicit_integrity_policy(
    repo_root: Path, tmp_path: Path, policy: str | None
):
    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)

    def mutate(data):
        if policy is None:
            del data["tools"]["node"]["integrity"]
        else:
            data["tools"]["node"]["integrity"] = policy

    _edit_manifest(copy_root, mutate)

    errors = validate_tool_versions(copy_root)

    assert any("node" in error and "integrity policy" in error for error in errors)


@pytest.mark.unit
def test_validator_requires_explicit_agent_skill_integrity_policy(
    repo_root: Path, tmp_path: Path
):
    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)

    def mutate(data):
        del data["tools"]["ast_grep"]["agent_skill"]["integrity"]

    _edit_manifest(copy_root, mutate)

    errors = validate_tool_versions(copy_root)

    assert any(
        "ast_grep" in error
        and "agent_skill needs an explicit integrity policy" in error
        for error in errors
    )


@pytest.mark.unit
def test_validator_requires_artifact_versions_to_match_top_level_alias(
    repo_root: Path, tmp_path: Path
):
    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)

    def mutate(data):
        data["tools"]["limactl"]["artifacts"]["arm64"]["version"] = "v2.2.1"

    _edit_manifest(copy_root, mutate)

    errors = validate_tool_versions(copy_root)

    assert any(
        "limactl" in error and "does not normalize to top-level version" in error
        for error in errors
    )


@pytest.mark.unit
def test_validator_requires_datasource_for_each_checksum_artifact(
    repo_root: Path, tmp_path: Path
):
    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)

    def mutate(data):
        del data["tools"]["acli"]["artifacts"]["amd64"]["datasource"]

    _edit_manifest(copy_root, mutate)

    errors = validate_tool_versions(copy_root)

    assert any(
        "acli" in error and "artifacts['amd64'].datasource" in error for error in errors
    )


@pytest.mark.unit
def test_validator_rejects_unknown_artifact_datasource(repo_root: Path, tmp_path: Path):
    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)

    def mutate(data):
        data["tools"]["acli"]["artifacts"]["amd64"]["datasource"] = "untrusted-feed"

    _edit_manifest(copy_root, mutate)

    errors = validate_tool_versions(copy_root)

    assert any(
        "acli" in error and "artifacts['amd64'].datasource must be one of" in error
        for error in errors
    )


@pytest.mark.unit
def test_validator_rejects_acli_datasource_for_the_other_architecture(
    repo_root: Path, tmp_path: Path
):
    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)

    def mutate(data):
        data["tools"]["acli"]["artifacts"]["amd64"]["datasource"] = "custom.acli-arm64"

    _edit_manifest(copy_root, mutate)

    errors = validate_tool_versions(copy_root)

    assert any(
        "acli" in error
        and "datasource must match the acli architecture-specific datasource" in error
        for error in errors
    )


@pytest.mark.unit
def test_version_only_policy_rejects_stale_checksum_metadata(
    repo_root: Path, tmp_path: Path
):
    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)

    def mutate(data):
        node = data["tools"]["node"]
        node["artifacts"] = {"amd64": {"sha256": "ab" * 32}}

    _edit_manifest(copy_root, mutate)

    errors = validate_tool_versions(copy_root)

    assert any("node" in error and "version-only" in error for error in errors)


@pytest.mark.unit
def test_opencode_manifest_pins_the_v2_npm_cli(repo_root: Path):
    manifest = json.loads((repo_root / "lima" / "tool-versions.json").read_text())
    opencode = manifest["tools"]["opencode"]

    assert opencode["version"].startswith("2.")
    assert opencode["datasource"] == "npm"
    assert opencode["depName"] == "@opencode/cli"


def _edit_manifest(copy_root: Path, mutate) -> None:
    manifest_path = copy_root / "lima" / "tool-versions.json"
    data = json.loads(manifest_path.read_text())
    mutate(data)
    manifest_path.write_text(json.dumps(data, indent=2) + "\n")


@pytest.mark.unit
def test_validator_rejects_a_modified_ca_trust_anchor(repo_root: Path, tmp_path: Path):
    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)
    certificate = copy_root / "lima" / "certs" / "redhat-ipa-ca.crt"
    certificate.write_bytes(certificate.read_bytes() + b"modified\n")

    errors = validate_tool_versions(copy_root)

    assert any("CA trust anchor 'redhat-ipa-ca.crt'" in error for error in errors)


@pytest.mark.unit
def test_validator_keeps_embedded_ca_pins_in_sync_with_the_manifest(
    repo_root: Path, tmp_path: Path
):
    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)
    manifest = json.loads((copy_root / "lima" / "tool-versions.json").read_text())
    digest = manifest["trust_anchors"]["redhat-root-ca.crt"]
    system_script = copy_root / "lima" / "provision-system.sh"
    system_script.write_text(system_script.read_text().replace(digest, "0" * 64, 1))

    errors = validate_tool_versions(copy_root)

    assert any("embedded CA trust-anchor pins" in error for error in errors)


@pytest.mark.unit
def test_validator_flags_malformed_checksum(repo_root: Path, tmp_path: Path):
    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)

    def mutate(data):
        data["tools"]["ast_grep"]["artifacts"]["amd64"]["sha256"] = "deadbeef"

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
        data["tools"]["ast_grep"]["provenance"]["url_templates"][
            "artifacts.riscv64.sha256"
        ] = "https://example.invalid/riscv.zip"

    _edit_manifest(copy_root, mutate)

    errors = validate_tool_versions(copy_root)

    assert any(
        "ast_grep" in error and "riscv64" in error and "no matching" in error
        for error in errors
    )


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
    manifest = json.loads((repo_root / "lima" / "tool-versions.json").read_text())

    expected = {
        "acli",
        "antigravity_cli",
        "ast_grep",
        "go",
        "google_workspace_cli",
        "hadolint",
        "helm",
        "kind",
        "kubectl",
        "limactl",
        "markdownlint_cli2",
        "minikube",
        "node",
        "opencode",
        "pipenv",
        "playwright_cli",
        "pre_commit",
        "repomix",
        "rust",
        "rustup",
        "semble",
        "uv",
    }
    for tool in expected:
        consumers = manifest["tools"][tool]["consumers"]
        assert consumers.get("lima") is True, tool
    assert "github_mcp_server" not in manifest["tools"]


@pytest.mark.unit
def test_tool_version_validator_reports_missing_lima_manifest_read(
    repo_root: Path, tmp_path: Path
):
    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)

    script = copy_root / "lima" / "provision-tools.sh"
    script.write_text(
        script.read_text().replace(
            "ensure_npm_package opencode", "ensure_npm_package not_opencode", 1
        )
    )

    errors = validate_tool_versions(copy_root)

    assert any("does not read 'opencode' version" in error for error in errors)


@pytest.mark.unit
def test_tool_version_validator_rejects_undeclared_lima_manifest_read(
    repo_root: Path, tmp_path: Path
):
    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)

    script = copy_root / "lima" / "provision-user.sh"
    script.write_text(script.read_text() + "manifest_version unknown_tool\n")

    errors = validate_tool_versions(copy_root)

    assert any("reads 'unknown_tool'" in error for error in errors)


@pytest.mark.unit
def test_tool_version_validator_requires_lima_release_checksum_reads(
    repo_root: Path, tmp_path: Path
):
    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)

    script = copy_root / "lima" / "provision-system.sh"
    script.write_text(
        "\n".join(
            line
            for line in script.read_text().splitlines()
            if "verify_download limactl" not in line
        )
        + "\n"
    )

    errors = validate_tool_versions(copy_root)

    assert any(
        "does not verify 'limactl' SHA-256 artifacts" in error for error in errors
    )


@pytest.mark.unit
def test_tool_version_validator_rejects_undeclared_lima_checksum_read(
    repo_root: Path, tmp_path: Path
):
    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)

    script = copy_root / "lima" / "provision-user.sh"
    script.write_text(script.read_text() + "verify_download unknown_tool\n")

    errors = validate_tool_versions(copy_root)

    assert any("reads 'unknown_tool' checksums" in error for error in errors)


@pytest.mark.unit
def test_tool_version_validator_requires_agent_skill_provenance_reads(
    repo_root: Path, tmp_path: Path
):
    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)

    script = copy_root / "lima" / "provision-user.sh"
    script.write_text(
        "\n".join(
            line
            for line in script.read_text().splitlines()
            if "manifest_agent_skill ast_grep integrity" not in line
        )
        + "\n"
    )

    errors = validate_tool_versions(copy_root)

    assert any(
        "does not read 'ast_grep' agent_skill['integrity']" in error for error in errors
    )


@pytest.mark.unit
def test_validator_allows_checksums_for_non_lima_consumers(
    repo_root: Path, tmp_path: Path
):
    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)

    # SHA-256 digests only need to be read by an installing consumer. yamllint
    # is consumed by pre-commit alone, so a Lima provisioning read is not
    # required for its metadata. A valid non-Lima entry with artifact records
    # and provenance still passes structural validation.
    def mutate(data):
        tool = data["tools"]["yamllint"]
        tool["integrity"] = "sha256"
        tool["artifacts"] = {
            "amd64": {
                "version": tool["version"],
                "sha256": "ab" * 32,
                "depName": "adrienverge/yamllint-amd64",
                "packageName": "adrienverge/yamllint",
                "datasource": "github-release-attachments",
            },
            "arm64": {
                "version": tool["version"],
                "sha256": "cd" * 32,
                "depName": "adrienverge/yamllint-arm64",
                "packageName": "adrienverge/yamllint",
                "datasource": "github-release-attachments",
            },
        }
        data["tools"]["yamllint"]["provenance"] = {
            "url_templates": {
                "artifacts.amd64.sha256": "https://example.test/yamllint/{artifacts.amd64.version}/amd64",
                "artifacts.arm64.sha256": "https://example.test/yamllint/{artifacts.arm64.version}/arm64",
            }
        }

    _edit_manifest(copy_root, mutate)

    assert validate_tool_versions(copy_root) == []
