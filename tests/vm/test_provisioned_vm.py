import os
import shlex
from pathlib import Path

import pytest

from tests.conftest import (
    LimaVM,
    expected_lima_provisioning_fingerprint,
    expected_lima_system_script_sha256,
)


@pytest.mark.vm
def test_provisioned_vm_toolchain_matches_manifest(devbox_vm: LimaVM):
    result = devbox_vm.run(["devbox-toolchain-check"], timeout=300)

    assert result.returncode == 0, (
        "The Lima VM toolchain does not match its pinned manifest.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "ERROR:" not in result.stdout


@pytest.mark.vm
def test_fresh_vm_fingerprint_matches_the_current_checkout(
    repo_root: Path, devbox_vm: LimaVM
):
    fresh_vm = (
        os.environ.get("MY_SANDBOX_VM_TEST_FRESH") == "1" or devbox_vm.name is not None
    )
    system_stamp = devbox_vm.run(
        ["cat", "/var/lib/devbox-vm/system-provision.sha256"], timeout=30
    )
    assert system_stamp.returncode == 0, system_stamp.stderr
    expected_system_sha = expected_lima_system_script_sha256(repo_root)
    if system_stamp.stdout.strip() != expected_system_sha:
        if not fresh_vm:
            pytest.skip(
                "the existing guest embeds a different system provisioner; "
                "use a fresh VM to validate the provisioning fingerprint"
            )
        pytest.fail(
            "fresh VM system-script digest differs from the current rendered "
            "provision-system.sh"
        )

    guest_stamp = devbox_vm.run(
        [
            "cat",
            f"{devbox_vm.guest_home}/.local/share/devbox-toolchain/provisioning.fingerprint",
        ],
        timeout=30,
    )
    assert guest_stamp.returncode == 0, guest_stamp.stderr
    expected_fingerprint = expected_lima_provisioning_fingerprint(repo_root)
    if guest_stamp.stdout.strip() != expected_fingerprint:
        if not fresh_vm:
            pytest.skip(
                "the existing guest has stale provisioning inputs; use a fresh "
                "VM to validate the complete fingerprint"
            )
        pytest.fail(
            "fresh VM provisioning fingerprint differs from the current checkout"
        )


@pytest.mark.vm
def test_project_utility_commands_are_discoverable(devbox_vm: LimaVM):
    command = (
        'for tool in devbox-go diff file patch podman; do command -v "$tool"; done'
    )
    result = devbox_vm.run(["bash", "-ceu", command], timeout=60)

    assert result.returncode == 0, (
        "VM-provisioned project utilities are unavailable.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.vm
def test_agent_guidance_and_skills_are_visible_in_vm(devbox_vm: LimaVM):
    if devbox_vm.name is None and os.environ.get("MY_SANDBOX_VM_TEST_FRESH") != "1":
        source = Path(devbox_vm.repo_path) / "lima/agent-skills/devbox-tools/SKILL.md"
        active = Path(devbox_vm.guest_home) / ".agents/skills/devbox-tools/SKILL.md"
        if not active.is_file() or active.read_bytes() != source.read_bytes():
            pytest.skip(
                "the existing guest has stale staged skills; use a fresh VM "
                "to verify skill provisioning"
            )

    source_path = f"{devbox_vm.repo_path}/lima/agent-skills/devbox-tools/SKILL.md"
    active_path = f"{devbox_vm.guest_home}/.agents/skills/devbox-tools/SKILL.md"
    ast_grep_path = f"{devbox_vm.guest_home}/.agents/skills/ast-grep/SKILL.md"
    outline_path = f"{devbox_vm.guest_home}/.agents/skills/ast-grep-outline/SKILL.md"
    command = f"""
set -euo pipefail
source={shlex.quote(source_path)}
active={shlex.quote(active_path)}
test -r "$source"
test -r "$active"
cmp -s "$source" "$active"
grep -q 'name: "devbox-tools"' "$active"
test -r {shlex.quote(ast_grep_path)}
test -r {shlex.quote(outline_path)}
"""
    result = devbox_vm.run(["bash", "-ceu", command], timeout=60)

    assert result.returncode == 0, (
        "The VM did not stage agent guidance and skills.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.vm
def test_host_credential_environment_is_not_forwarded_to_vm_processes(
    devbox_vm: LimaVM, monkeypatch: pytest.MonkeyPatch
):
    """The fixture's test-process environment must not carry host secrets.

    In local guest mode, this does not hide same-UID files under
    ``~/.host-config``; only trusted source may be run with ``--guest-vm``.
    """
    secret = "mock-vm-credential-must-not-cross-boundary"  # pragma: allowlist secret
    monkeypatch.setenv("GH_TOKEN", secret)
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)

    result = devbox_vm.run(["bash", "-ceu", "env"], timeout=30)

    assert result.returncode == 0, result.stderr
    assert secret not in result.stdout
    assert secret not in result.stderr
    assert "GH_TOKEN=" not in result.stdout
    assert "ANTHROPIC_API_KEY=" not in result.stdout
    assert "GH_TOKEN" not in devbox_vm.env
    assert "ANTHROPIC_API_KEY" not in devbox_vm.env
