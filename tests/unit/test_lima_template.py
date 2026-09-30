"""Unit tests for the lima/devbox.yaml VM template and its scripts."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

LIMA_DIR = "lima"

EXPECTED_MOUNT_LOCATIONS = {
    "~/.agents",
    "~/.config/acli",
    "~/.config/gcloud",
    "~/.config/gh",
    "~/.config/gws",
    "~/.config/opencode",
    "~/.local/share/opencode",
    "~/kb",
    "~/src",
}

EXPECTED_SHARED_RELS = sorted(
    location.removeprefix("~/")
    for location in EXPECTED_MOUNT_LOCATIONS
    if location != "~/.agents"
)

_SCRIPTS = (
    "provision-system.sh",
    "provision-user.sh",
    "probe-readiness.sh",
)


def _template(repo_root: Path) -> dict:
    return yaml.safe_load((repo_root / LIMA_DIR / "devbox.yaml").read_text())


def _script(repo_root: Path, name: str) -> str:
    return (repo_root / LIMA_DIR / name).read_text()


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def _shared_rels_from_loop(text: str) -> list[str]:
    """Extract the entries of a `for rel in ...` loop body."""

    match = re.search(r"^for rel in \\\n(.*?)^\s*do$", text, re.DOTALL | re.MULTILINE)
    assert match, "no 'for rel in' loop found"
    entries = []
    for line in match.group(1).splitlines():
        entry = line.strip().rstrip("\\").strip()
        if entry:
            entries.append(entry)
    return entries


@pytest.mark.unit
def test_template_uses_qemu_with_nested_virtualization(repo_root: Path):
    config = _template(repo_root)

    assert config["vmType"] == "qemu"
    assert config["nestedVirtualization"] is True
    # The default cpuType ("host" on Linux/KVM) exposes VMX/SVM to the
    # guest; overriding it would break nested virtualization.
    assert "cpuType" not in config


@pytest.mark.unit
def test_template_minimum_lima_version(repo_root: Path):
    config = _template(repo_root)

    version = config["minimumLimaVersion"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
    # 2.1.3 is the floor for the CVE-2026-53657 guest-agent fix.
    assert _version_tuple(version) >= (2, 1, 3)


@pytest.mark.unit
def test_template_resources(repo_root: Path):
    config = _template(repo_root)

    assert 6 <= config["cpus"] <= 8
    assert config["memory"] == "16GiB"
    assert config["disk"] == "100GiB"


@pytest.mark.unit
def test_template_images_are_digest_pinned_fedora(repo_root: Path):
    config = _template(repo_root)

    images = config["images"]
    assert {image["arch"] for image in images} == {"x86_64", "aarch64"}
    for image in images:
        assert image["location"].startswith("https://")
        assert "fedora" in image["location"].lower()
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", image["digest"])


@pytest.mark.unit
def test_template_mounts_are_the_expected_same_path_set(repo_root: Path):
    config = _template(repo_root)

    mounts = config["mounts"]
    locations = {mount["location"] for mount in mounts}
    assert locations == EXPECTED_MOUNT_LOCATIONS
    for mount in mounts:
        if mount["location"] == "~/src":
            assert mount["mountPoint"] == "{{.Param.SrcPath}}"
            assert mount["writable"] is True
        else:
            assert mount["mountPoint"].startswith("{{.Home}}/.host-config/")
            assert mount["writable"] is (mount["location"] != "~/.agents")
        # Never mount all of $HOME.
        assert mount["location"] != "~"
    # OpenCode's volatile state stays VM-local.
    assert "~/.local/state/opencode" not in locations


@pytest.mark.unit
def test_host_agent_skills_are_read_only_and_overlayed_in_guest(repo_root: Path):
    config = _template(repo_root)
    agents_mount = next(
        mount for mount in config["mounts"] if mount["location"] == "~/.agents"
    )
    user_script = _script(repo_root, "provision-user.sh")

    assert agents_mount["writable"] is False
    assert agents_mount["mountPoint"] == "{{.Home}}/.host-config/agents"
    assert 'if [[ -L "$HOME/.agents" ]]; then' in user_script
    assert 'host_agents="$HOME/.host-config/agents"' in user_script
    assert "devbox-tools" in user_script
    assert 'ln -s "$source" "$destination"' in user_script


@pytest.mark.unit
def test_third_party_package_installs_run_as_credential_isolated_builder(
    repo_root: Path,
):
    system_script = _script(repo_root, "provision-system.sh")
    user_script = _script(repo_root, "provision-user.sh")
    tool_script = _script(repo_root, "provision-tools.sh")

    assert "npm install --global" not in system_script
    assert "npm install --global" not in user_script
    assert 'npm install --global --prefix "$HOME/.local"' in tool_script
    assert "uv tool install --force" in tool_script
    assert 'as_toolbuilder /bin/bash "$tool_script_snapshot"' in system_script
    assert "env -i" in system_script
    assert 'setfacl --modify "u:$(id -u "$DEVBOX_USER"):--x"' in system_script
    assert 'runuser -u "$TOOL_BUILDER_USER"' in system_script
    assert 'protect_guest_mount_parent "$src_alias_parent" "SrcPath"' in system_script
    assert 'case "$src_alias_parent_real/" in' in system_script
    assert '"$DEVBOX_GUEST_HOME_REAL/"*' in system_script
    assert '[[ -x "$SRC_ROOT" ]]' in tool_script


@pytest.mark.unit
def test_readiness_uses_builder_check_stamp_not_guest_tool_execution(
    repo_root: Path,
):
    probe = _script(repo_root, "probe-readiness.sh")

    assert "manifest.sha256" in probe
    assert '"$HOME/.local/bin/devbox-toolchain-check"' not in probe
    assert 'PLAYWRIGHT_BROWSERS_PATH="$TOOL_BUILDER_HOME/.cache/ms-playwright"' in probe
    assert 'HF_HOME="$TOOL_BUILDER_HOME/.cache/semble/huggingface"' in probe


@pytest.mark.unit
def test_probe_checks_readonly_host_agent_mount(repo_root: Path):
    probe = _script(repo_root, "probe-readiness.sh")

    assert '"$HOME/.host-config/agents"' in probe
    assert "$HOME/.agents is not the guest-local skill overlay" in probe
    assert "difft fd" in probe


@pytest.mark.unit
def test_agent_skill_provenance_is_validated_before_archive_path_use(
    repo_root: Path,
):
    user_script = _script(repo_root, "provision-user.sh")

    assert '[[ ! "$AST_GREP_COMMIT" =~ ^[0-9a-f]{40}$ ]]' in user_script
    assert '[[ ! "$AST_GREP_SKILL_SHA256" =~ ^[0-9a-f]{64}$ ]]' in user_script


@pytest.mark.unit
def test_template_disables_containerd(repo_root: Path):
    config = _template(repo_root)

    assert config["containerd"] == {"system": False, "user": False}


@pytest.mark.unit
def test_full_template_keeps_9p_until_direct_virtiofs_validation(
    repo_root: Path,
):
    config = _template(repo_root)

    assert config["mountType"] == "9p"
    template = (repo_root / LIMA_DIR / "devbox.yaml").read_text()
    assert "virtiofsd could not start" in template


@pytest.mark.unit
def test_provision_and_probe_scripts_are_referenced_and_valid(
    repo_root: Path,
):
    config = _template(repo_root)

    assert [(entry["mode"], entry["file"]) for entry in config["provision"]] == [
        ("system", "provision-system.sh"),
        ("user", "provision-user.sh"),
    ]
    probes = config["probes"]
    assert len(probes) == 1
    assert probes[0]["mode"] == "readiness"
    assert "file" not in probes[0]
    assert probes[0]["script"] == _script(repo_root, "probe-readiness.sh")

    for name in _SCRIPTS:
        path = repo_root / LIMA_DIR / name
        assert path.is_file(), name
        # Lima requires provision and probe scripts to start with '#!'.
        assert path.read_text().startswith("#!"), name


@pytest.mark.unit
def test_params_are_consumed_by_the_user_script(repo_root: Path):
    config = _template(repo_root)

    assert config["param"] == {
        "GitUserName": "",
        "GitUserEmail": "",
        "SrcPath": "",
        "RepoPath": "",
        "KbPath": "",
    }
    user_script = _script(repo_root, "provision-user.sh")
    # Lima rejects params that no script references ($PARAM_<Key>).
    assert "PARAM_GitUserName" in user_script
    assert "PARAM_GitUserEmail" in user_script
    assert "PARAM_RepoPath" in user_script
    assert "PARAM_RepoPath" in _script(repo_root, "provision-system.sh")
    assert "PARAM_SrcPath" in _script(repo_root, "provision-system.sh")
    assert "PARAM_KbPath" in _script(repo_root, "provision-system.sh")


@pytest.mark.unit
def test_scripts_contain_no_unexpected_go_template_expressions(
    repo_root: Path,
):
    # Lima renders provision/probe scripts as Go templates. Only the
    # system script may use a template variable, and only {{.User}}.
    for name in _SCRIPTS:
        found = re.findall(r"{{.*?}}", _script(repo_root, name))
        if name == "provision-system.sh":
            assert set(found) == {"{{.User}}"}
        else:
            assert found == []


@pytest.mark.unit
def test_user_script_links_exactly_the_mounted_paths(repo_root: Path):
    user_script = _script(repo_root, "provision-user.sh")

    assert _shared_rels_from_loop(user_script) == EXPECTED_SHARED_RELS
    assert 'DEVBOX_SRC_ROOT="${PARAM_SrcPath:-}"' in user_script
    assert 'src) target="$DEVBOX_SRC_ROOT" ;;' in user_script


@pytest.mark.unit
def test_probe_checks_exactly_the_mounted_paths(repo_root: Path):
    probe = _script(repo_root, "probe-readiness.sh")

    assert _shared_rels_from_loop(probe) == EXPECTED_SHARED_RELS
    assert 'src) target="${PARAM_SrcPath:-}" ;;' in probe


@pytest.mark.unit
def test_system_script_adds_user_to_kvm_group(repo_root: Path):
    system_script = _script(repo_root, "provision-system.sh")

    assert 'DEVBOX_USER="{{.User}}"' in system_script
    assert 'usermod --append --groups kvm "$DEVBOX_USER"' in system_script


@pytest.mark.unit
def test_system_script_installs_qemu_img_for_nested_lima(repo_root: Path):
    system_script = _script(repo_root, "provision-system.sh")
    match = re.search(
        r"^packages=\(\n(.*?)^\)",
        system_script,
        re.DOTALL | re.MULTILINE,
    )

    assert match
    assert re.search(r"^\s*qemu-img\s*$", match.group(1), re.MULTILINE)


@pytest.mark.unit
def test_external_gcloud_rpm_skips_root_scriptlets(repo_root: Path):
    system_script = _script(repo_root, "provision-system.sh")

    assert "--setopt=tsflags=noscripts" in system_script


@pytest.mark.unit
def test_system_script_uses_a_stamp_for_limactl_version(repo_root: Path):
    system_script = _script(repo_root, "provision-system.sh")

    assert "limactl_stamp=/usr/local/share/devbox-vm/limactl.version" in system_script
    assert 'verify_download limactl "$arch"' in system_script
    # limactl refuses to run as root, so provisioning cannot query its version.
    assert "/usr/local/bin/limactl --version" not in system_script


@pytest.mark.unit
def test_root_manifest_path_is_canonical_and_snapshotted(repo_root: Path):
    system_script = _script(repo_root, "provision-system.sh")

    assert 'DEVBOX_SRC_ROOT="$(realpath -e -- "$DEVBOX_SRC_ROOT")"' in system_script
    assert 'DEVBOX_REPO="$(realpath -e -- "$DEVBOX_REPO")"' in system_script
    assert '[[ -L "$MANIFEST_SOURCE" || ! -f "$MANIFEST_SOURCE" ]]' in system_script
    assert 'copy_repo_file container/tool-versions.json "$MANIFEST"' in system_script
    assert "os.O_NOFOLLOW" in system_script
    assert "copy_repo_file lima/provision-tools.sh" in system_script


@pytest.mark.unit
def test_root_snapshot_copy_rejects_symlinks(repo_root: Path, tmp_path: Path):
    system_script = _script(repo_root, "provision-system.sh")
    match = re.search(r"<<'PY'\n(.*?)\nPY\n}", system_script, re.DOTALL)
    assert match, "safe repository-copy Python helper is missing"
    copier = match.group(1)

    repo = tmp_path / "repo"
    container = repo / "container"
    container.mkdir(parents=True)
    source = container / "tool-versions.json"
    source.write_text('{"tools": {}}\n')
    output = tmp_path / "snapshot.json"

    def copy(relative: str):
        return subprocess.run(
            [sys.executable, "-I", "-S", "-", str(repo), relative, str(output)],
            input=copier,
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )

    copied = copy("container/tool-versions.json")
    assert copied.returncode == 0, copied.stderr
    assert output.read_text() == source.read_text()

    symlink_target = tmp_path / "outside.json"
    symlink_target.write_text('{"secret": true}\n')
    source.unlink()
    source.symlink_to(symlink_target)
    rejected_file_link = copy("container/tool-versions.json")
    assert rejected_file_link.returncode != 0

    source.unlink()
    os.mkfifo(source)
    rejected_fifo = copy("container/tool-versions.json")
    assert rejected_fifo.returncode != 0

    source.unlink()
    source.write_text("regular file\n")
    real_lima = tmp_path / "real-lima"
    real_lima.mkdir()
    (real_lima / "provision-tools.sh").write_text("safe\n")
    (repo / "lima").symlink_to(real_lima, target_is_directory=True)
    rejected_directory_link = copy("lima/provision-tools.sh")
    assert rejected_directory_link.returncode != 0


@pytest.mark.unit
def test_provisioning_stamps_do_not_use_guest_writable_system_directory(
    repo_root: Path,
):
    system_script = _script(repo_root, "provision-system.sh")
    user_script = _script(repo_root, "provision-user.sh")

    assert "install -d -m 0755 -o root -g root /var/lib/devbox-vm" in system_script
    assert "mktemp /var/lib/devbox-vm/system-provision.sha256.XXXXXX" in system_script
    assert (
        'fingerprint_file="$HOME/.local/share/devbox-toolchain/provisioning.fingerprint"'
        in user_script
    )
    assert "/var/lib/devbox-vm/provisioning.fingerprint" not in user_script


@pytest.mark.unit
def test_repository_wrappers_are_staged_without_root_privileges(repo_root: Path):
    system_script = _script(repo_root, "provision-system.sh")
    user_script = _script(repo_root, "provision-user.sh")

    assert 'as_toolbuilder /bin/bash "$tool_script_snapshot"' in system_script
    assert '"/var/lib/devbox-vm/tool-assets/check_toolchain.py"' in user_script
    assert '"/var/lib/devbox-vm/tool-assets/devbox-go"' in user_script


@pytest.mark.unit
def test_rootless_bridge_setup_keeps_loopback_routing_disabled(repo_root: Path):
    system_script = _script(repo_root, "provision-system.sh")
    probe = _script(repo_root, "probe-readiness.sh")

    assert "net.ipv4.conf.default.route_localnet = 1" not in system_script
    assert "check_sysctl net.ipv4.conf.default.route_localnet 0" in probe
