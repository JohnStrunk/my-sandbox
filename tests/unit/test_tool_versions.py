import json
import re
import shutil
from pathlib import Path

import pytest
import yaml

from scripts.validate_tool_versions import (
    _LIMA_MANIFEST_VERSION_PATTERN,
    validate_tool_versions,
)

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
    "lima/fedora-packages.sh",
    "lima/certs/redhat-ipa-ca.crt",
    "lima/certs/redhat-rhcsv2-ca.crt",
    "lima/certs/redhat-root-ca.crt",
]

_CHECKSUM_MANAGED_TOOLS = {
    "acli",
    "actionlint",
    "antigravity_cli",
    "ast_grep",
    "hadolint",
    "golangci_lint",
    "krew",
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
def test_ci_installs_the_manifest_pinned_uv(repo_root: Path):
    """CI resolves uv from the manifest and proves the install matches it."""

    manifest = json.loads((repo_root / "lima" / "tool-versions.json").read_text())
    workflow = yaml.safe_load(
        (repo_root / ".github" / "workflows" / "ci-workflow.yaml").read_text()
    )

    assert manifest["tools"]["uv"]["consumers"]["ci"] is True
    for job in ("pre-commit", "test"):
        steps = workflow["jobs"][job]["steps"]
        names = [step.get("name") for step in steps]
        read_index = names.index("Read pinned uv version")
        install_index = names.index("Install uv")
        verify_index = names.index("Verify installed uv matches the pinned version")
        assert read_index < install_index < verify_index
        assert ".tools.uv.version" in steps[read_index]["run"]
        assert "lima/tool-versions.json" in steps[read_index]["run"]
        # The manifest value reaches $GITHUB_OUTPUT only if it matches the
        # safe-version policy that scripts/validate_tool_versions.py enforces.
        assert (
            r'select(test("^v?[0-9]+(\\.[0-9]+){1,3}([-+][A-Za-z0-9.]+)?\\z"))'
        ) in steps[read_index]["run"]
        assert steps[install_index]["with"]["version"] == (
            "${{ steps.uv-version.outputs.version }}"
        )
        assert "uv --version" in steps[verify_index]["run"]

    test_names = [step.get("name") for step in workflow["jobs"]["test"]["steps"]]
    assert test_names.index("Verify installed uv matches the pinned version") < (
        test_names.index("Verify The Source MCP environment installs")
    )


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
def test_validator_ignores_github_action_release_comments(
    repo_root: Path, tmp_path: Path
):
    """A SHA-pinned action's ``# vX.Y.Z`` comment is not a tool install."""

    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)

    workflow_path = copy_root / ".github" / "workflows" / "ci-workflow.yaml"
    workflow = workflow_path.read_text()
    action_line = next(
        line for line in workflow.splitlines() if "uses: astral-sh/setup-uv@" in line
    )
    mutated_line = action_line.replace("# v", "# v99.")
    assert mutated_line != action_line
    assert "# v99." in mutated_line
    workflow_path.write_text(workflow.replace(action_line, mutated_line, 1))

    assert validate_tool_versions(copy_root) == []


@pytest.mark.unit
def test_validator_checks_uses_like_shell_lines(repo_root: Path, tmp_path: Path):
    """Only a SHA-pinned action reference is exempt, not arbitrary run lines."""

    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)

    workflow_path = copy_root / ".github" / "workflows" / "ci-workflow.yaml"
    workflow = workflow_path.read_text()
    marker = "          ./scripts/verify-the-source-mcp-install.sh\n"
    assert marker in workflow
    workflow_path.write_text(
        workflow.replace(marker, marker + "          uses: astral-sh/uv@0.13.4\n", 1)
    )

    errors = validate_tool_versions(copy_root)

    assert any("hard-coded version for 'uv'" in error for error in errors)


@pytest.mark.unit
def test_validator_rejects_shell_functional_uses_lines_with_a_sha(
    repo_root: Path, tmp_path: Path
):
    """A valid SHA must not let a shell expression borrow the action exemption."""

    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)

    workflow_path = copy_root / ".github" / "workflows" / "ci-workflow.yaml"
    workflow = workflow_path.read_text()
    marker = "          ./scripts/verify-the-source-mcp-install.sh\n"
    assert marker in workflow
    pseudo_uses = (
        "          uses: astral-sh/uv||cmd@"
        "0123456789012345678901234567890123456789 # v0.13.5\n"
    )
    workflow_path.write_text(workflow.replace(marker, marker + pseudo_uses, 1))

    errors = validate_tool_versions(copy_root)

    assert any("hard-coded version for 'uv'" in error for error in errors)


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
        "coderabbit_cli",
        "node",
        "go",
        "gocyclo",
        "rustup",
        "helm",
        "kubectl",
        "docker_ce",
        "containerd_io",
        "docker_buildx_plugin",
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
        limactl = data["tools"]["limactl"]
        alias = limactl["version"].removeprefix("v")
        mismatch_version = "v0.0.0-mismatch"
        if alias == mismatch_version.removeprefix("v"):
            mismatch_version = "v0.0.1-mismatch"
        limactl["artifacts"]["arm64"]["version"] = mismatch_version

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


@pytest.mark.unit
def test_coderabbit_manifest_pins_the_official_cli_version_feed(repo_root: Path):
    manifest = json.loads((repo_root / "lima" / "tool-versions.json").read_text())
    coderabbit = manifest["tools"]["coderabbit_cli"]

    assert re.fullmatch(r"\d+\.\d+\.\d+", coderabbit["version"])
    assert coderabbit["datasource"] == "custom.coderabbit-cli"
    assert coderabbit["depName"] == "coderabbit-cli"
    assert coderabbit["integrity"] == "version-only"
    assert coderabbit["consumers"]["lima"] is True


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
        "actionlint",
        "antigravity_cli",
        "ast_grep",
        "coderabbit_cli",
        "go",
        "gocyclo",
        "golangci_lint",
        "google_workspace_cli",
        "hadolint",
        "helm",
        "kind",
        "krew",
        "kubectl",
        "limactl",
        "markdownlint_cli2",
        "mermaid_cli",
        "minikube",
        "node",
        "opencode",
        "pipenv",
        "playwright_cli",
        "pre_commit",
        "repomix",
        "ruby",
        "rust",
        "rustup",
        "semble",
        "uv",
        "yamllint",
    }
    for tool in expected:
        consumers = manifest["tools"][tool]["consumers"]
        assert consumers.get("lima") is True, tool
    assert "github_mcp_server" not in manifest["tools"]


@pytest.mark.unit
def test_docker_rpm_tools_are_not_lima_consumers(repo_root: Path):
    """Docker RPM versions float in the signed DNF transaction, not a pin.

    The manifest keeps the entries so Renovate and the version validator still
    describe them, but Lima provisioning must not read them as exact versions.
    """

    manifest = json.loads((repo_root / "lima" / "tool-versions.json").read_text())

    for tool in ("docker_ce", "containerd_io", "docker_buildx_plugin"):
        spec = manifest["tools"][tool]
        assert spec["consumers"] == {"lima": False}, tool
        assert spec["integrity"] == "version-only", tool
        assert spec["datasource"] == "github-releases", tool
        assert re.fullmatch(r"\d+\.\d+\.\d+", spec["version"]), tool


@pytest.mark.unit
def test_docker_legacy_pins_stay_outside_lima_consumers(repo_root: Path):
    """The rollback pin reader must not register Docker as a Lima consumer.

    The live manifest keeps ``consumers.lima: false`` for all three Docker
    records. The embedded provisioner reads their versions only through
    ``docker_legacy_pin_version`` in its all-true legacy branch, so the manifest
    validator stays clean and sees no generic ``manifest_version`` read.
    """

    manifest = json.loads((repo_root / "lima" / "tool-versions.json").read_text())
    docker_tools = {"docker_ce", "containerd_io", "docker_buildx_plugin"}
    for tool in sorted(docker_tools):
        assert manifest["tools"][tool]["consumers"]["lima"] is False, tool

    assert validate_tool_versions(repo_root) == []

    system_script = (repo_root / "lima" / "provision-system.sh").read_text()
    assert "docker_legacy_pin_version()" in system_script
    assert "docker_legacy_pin_version docker_ce" in system_script
    version_reads = set(_LIMA_MANIFEST_VERSION_PATTERN.findall(system_script))
    assert not version_reads & docker_tools


@pytest.mark.unit
def test_ruby_manifest_pins_fedora_44_ruby(repo_root: Path):
    manifest = json.loads((repo_root / "lima" / "tool-versions.json").read_text())
    ruby = manifest["tools"]["ruby"]

    assert ruby["version"] == "4.0.7"
    assert ruby["datasource"] == "ruby-version"
    assert ruby["depName"] == "ruby"
    assert ruby["integrity"] == "version-only"
    assert ruby["consumers"]["lima"] is True


@pytest.mark.unit
def test_actionlint_manifest_pins_verified_architecture_artifacts(repo_root: Path):
    manifest = json.loads((repo_root / "lima" / "tool-versions.json").read_text())
    actionlint = manifest["tools"]["actionlint"]

    version = actionlint["version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
    assert actionlint["datasource"] == "github-releases"
    assert actionlint["depName"] == "rhysd/actionlint"
    assert actionlint["integrity"] == "sha256"
    assert actionlint["consumers"]["lima"] is True
    assert set(actionlint["artifacts"]) == {"amd64", "arm64"}
    url_templates = actionlint["provenance"]["url_templates"]
    for arch in ("amd64", "arm64"):
        artifact = actionlint["artifacts"][arch]
        # Renovate updates the manifest, so tie each release tag to the
        # top-level pin instead of hard-coding a release version.
        assert artifact["version"] == f"v{version}"
        assert artifact["depName"] == f"rhysd/actionlint-{arch}"
        assert artifact["packageName"] == "rhysd/actionlint"
        assert artifact["datasource"] == "github-release-attachments"
        assert re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"])
        assert url_templates[f"artifacts.{arch}.sha256"] == (
            "https://github.com/rhysd/actionlint/releases/download/"
            f"{{artifacts.{arch}.version}}/actionlint_{{version}}_linux_{arch}.tar.gz"
        )


@pytest.mark.unit
def test_yamllint_lima_consumer_keeps_pre_commit_pin(repo_root: Path):
    manifest = json.loads((repo_root / "lima" / "tool-versions.json").read_text())
    yamllint = manifest["tools"]["yamllint"]

    assert re.fullmatch(r"\d+\.\d+\.\d+", yamllint["version"])
    assert yamllint["integrity"] == "version-only"
    assert yamllint["consumers"]["lima"] is True
    assert yamllint["consumers"]["pre-commit"] == (
        "https://github.com/adrienverge/yamllint"
    )


@pytest.mark.unit
def test_krew_manifest_pins_verified_architecture_artifacts(repo_root: Path):
    manifest = json.loads((repo_root / "lima" / "tool-versions.json").read_text())
    krew = manifest["tools"]["krew"]

    version = krew["version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
    assert krew["integrity"] == "sha256"
    assert set(krew["artifacts"]) == {"amd64", "arm64"}
    url_templates = krew["provenance"]["url_templates"]
    for arch in ("amd64", "arm64"):
        artifact = krew["artifacts"][arch]
        assert artifact["version"] == f"v{version}"
        assert artifact["depName"] == f"kubernetes-sigs/krew-{arch}"
        assert re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"])
        assert url_templates[f"artifacts.{arch}.sha256"] == (
            "https://github.com/kubernetes-sigs/krew/releases/download/"
            f"{{artifacts.{arch}.version}}/krew-linux_{arch}.tar.gz"
        )


@pytest.mark.unit
def test_krew_provisioner_extracts_the_dot_prefixed_archive_member(repo_root: Path):
    provisioner = (repo_root / "lima" / "provision-user.sh").read_text()

    assert (
        'tar -C "$tmp" -xzf "$tmp/$archive" "./krew-linux_${KREW_ARCH}"' in provisioner
    )


@pytest.mark.unit
def test_krew_bootstrap_overwrite_precedes_post_install_version_check(
    repo_root: Path,
):
    provisioner = (repo_root / "lima" / "provision-user.sh").read_text()
    install = provisioner.index('    "$bootstrap" install krew')
    overwrite = provisioner.index(
        'install -D -m 0755 "$bootstrap" "$KREW_ROOT/bin/kubectl-krew"', install
    )
    version_check = provisioner.index(
        '  krew_installed_version="$(kubectl krew version', install
    )

    assert install < overwrite < version_check


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
def test_tool_version_validator_reports_missing_ruby_manifest_read(
    repo_root: Path, tmp_path: Path
):
    copy_root = tmp_path / "repo"
    _copy_validator_inputs(repo_root, copy_root)

    script = copy_root / "lima" / "fedora-packages.sh"
    source = script.read_text()
    assert source.count("manifest_version ruby") >= 1
    script.write_text(source.replace("manifest_version ruby", "manifest_version node"))

    errors = validate_tool_versions(copy_root)

    assert any("does not read 'ruby' version" in error for error in errors)


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

    # SHA-256 digests only need to be read by an installing consumer. mergify
    # is consumed by pre-commit alone, so a Lima provisioning read is not
    # required for its metadata. A valid non-Lima entry with artifact records
    # and provenance still passes structural validation.
    def mutate(data):
        tool = data["tools"]["mergify"]
        tool["integrity"] = "sha256"
        tool["artifacts"] = {
            "amd64": {
                "version": tool["version"],
                "sha256": "ab" * 32,
                "depName": "Mergifyio/mergify-pre-commit-amd64",
                "packageName": "Mergifyio/mergify-pre-commit",
                "datasource": "github-release-attachments",
            },
            "arm64": {
                "version": tool["version"],
                "sha256": "cd" * 32,
                "depName": "Mergifyio/mergify-pre-commit-arm64",
                "packageName": "Mergifyio/mergify-pre-commit",
                "datasource": "github-release-attachments",
            },
        }
        data["tools"]["mergify"]["provenance"] = {
            "url_templates": {
                "artifacts.amd64.sha256": "https://example.test/mergify/{artifacts.amd64.version}/amd64",
                "artifacts.arm64.sha256": "https://example.test/mergify/{artifacts.arm64.version}/arm64",
            }
        }

    _edit_manifest(copy_root, mutate)

    assert validate_tool_versions(copy_root) == []
