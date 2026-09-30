import pytest

from tests.conftest import LimaVM


@pytest.mark.vm
def test_provisioned_vm_toolchain_matches_manifest(devbox_vm: LimaVM):
    result = devbox_vm.run(["devbox-toolchain-check"], timeout=300)

    assert result.returncode == 0, (
        "The Lima VM toolchain does not match its pinned manifest.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "ERROR:" not in result.stdout


@pytest.mark.vm
def test_agent_guidance_and_skills_are_visible_in_vm(devbox_vm: LimaVM):
    command = rf"""
set -euo pipefail
source={devbox_vm.repo_path}/container/agent-skills/devbox-tools/SKILL.md
active={devbox_vm.guest_home}/.agents/skills/devbox-tools/SKILL.md
test -r "$source"
test -r "$active"
cmp -s "$source" "$active"
grep -q 'name: "devbox-tools"' "$active"
test -r {devbox_vm.guest_home}/.agents/skills/ast-grep/SKILL.md
test -r {devbox_vm.guest_home}/.agents/skills/ast-grep-outline/SKILL.md
"""
    result = devbox_vm.run(["bash", "-ceu", command], timeout=60)

    assert result.returncode == 0, (
        "The VM did not stage agent guidance and skills.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.vm
def test_host_credentials_do_not_reach_vm_processes_or_logs(
    devbox_vm: LimaVM, monkeypatch: pytest.MonkeyPatch
):
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
