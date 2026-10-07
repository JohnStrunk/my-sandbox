"""Unit tests for the lima/devbox.yaml VM template and its scripts."""

import hashlib
import os
import re
import stat
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
    "~/.local/state/devbox-opencode",
    "~/.local/state/opencode",
    "~/kb",
    "~/src",
}

EXPECTED_READ_ONLY_MOUNTS = {"~/.agents", "~/.local/state/opencode"}

# Skills stay guest-local; the persistent state mount is linked at
# ~/.local/state/opencode rather than its host source name.
EXPECTED_SHARED_RELS = sorted(
    location.removeprefix("~/")
    for location in EXPECTED_MOUNT_LOCATIONS
    if location not in {"~/.agents", "~/.local/state/devbox-opencode"}
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
            assert mount["writable"] is (
                mount["location"] not in EXPECTED_READ_ONLY_MOUNTS
            )
        # Never mount all of $HOME.
        assert mount["location"] != "~"
    # OpenCode's host state is only a read-only seed; the L1 service writes to
    # a separate persistent host directory.
    mounts_by_location = {mount["location"]: mount for mount in mounts}
    assert mounts_by_location["~/.local/state/opencode"]["writable"] is False
    assert mounts_by_location["~/.local/state/devbox-opencode"]["writable"] is True
    assert (
        mounts_by_location["~/.local/state/opencode"]["mountPoint"]
        == "{{.Home}}/.host-config/local/state/opencode-seed"
    )
    assert (
        mounts_by_location["~/.local/state/devbox-opencode"]["mountPoint"]
        == "{{.Home}}/.host-config/local/state/devbox-opencode"
    )


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
def test_readiness_checks_private_guest_task_scratch(repo_root: Path):
    probe = _script(repo_root, "probe-readiness.sh")

    assert "OPENCODE_TMP=/tmp/opencode" in probe
    assert "stat -c '%u:%g:%a' -- \"$OPENCODE_TMP\"" in probe
    assert '"$metadata" != "0:0:1777"' in probe
    assert 'mktemp -d "$OPENCODE_TMP/readiness.XXXXXXXX"' in probe
    assert "stat -c '%a' -- \"$task_dir\"" in probe
    assert 'printf \'%s\\n\' "$expected" >"$sentinel"' in probe
    assert 'read -r actual <"$sentinel"' in probe
    assert 'current_user="$(id -un)"' in probe
    assert "'$current_user'" in probe
    assert 'open("/proc/self/mountinfo", encoding="utf-8")' in probe
    assert "except UnicodeError:" in probe
    assert "raise SystemExit(2)" in probe


@pytest.mark.unit
def test_vm_skill_documents_private_ephemeral_task_scratch(repo_root: Path):
    skill = (repo_root / LIMA_DIR / "agent-skills/devbox-tools/SKILL.md").read_text()

    assert "Guest-local scratch path: `/tmp/opencode`" in skill
    assert 'task_dir="$(mktemp -d /tmp/opencode/task.XXXXXXXX)"' in skill
    assert 'chmod 0700 "$task_dir"' in skill
    assert "not host-mounted or persistent" in skill


@pytest.mark.unit
def test_probe_checks_readonly_host_agent_mount(repo_root: Path):
    probe = _script(repo_root, "probe-readiness.sh")

    assert '"$HOME/.host-config/agents"' in probe
    assert "$HOME/.agents is not the guest-local skill overlay" in probe
    assert "difft fd" in probe


@pytest.mark.unit
def test_agent_skill_integrity_policy_is_checked_before_archive_path_use(
    repo_root: Path,
):
    user_script = _script(repo_root, "provision-user.sh")

    assert "manifest_agent_skill ast_grep integrity" in user_script
    assert '[[ ! "$AST_GREP_COMMIT" =~ ^[0-9a-f]{40}$ ]]' in user_script
    assert '[[ ! "$AST_GREP_SKILL_SHA256" =~ ^[0-9a-f]{64}$ ]]' in user_script
    assert "  version-only)" in user_script
    assert "SHA-256 verification is skipped" in user_script
    assert (
        'AST_GREP_SKILL_STATE_VALUE="${AST_GREP_COMMIT}|'
        '${AST_GREP_SKILL_INTEGRITY}|${AST_GREP_SKILL_SHA256:-}"'
    ) in user_script
    assert '"$AST_GREP_SKILL_STATE_VALUE"' in user_script


@pytest.mark.unit
def test_binary_integrity_changes_invalidate_installed_version_stamps(
    repo_root: Path,
):
    system_script = _script(repo_root, "provision-system.sh")
    tool_script = _script(repo_root, "provision-tools.sh")
    downloaded_tools = (
        "node",
        "uv",
        "hadolint",
        "go",
        "limactl",
        "antigravity_cli",
        "acli",
        "kind",
        "kubectl",
        "helm",
        "ast_grep",
    )

    assert "manifest_integrity_fingerprint()" in system_script
    assert "artifact_integrity_matches()" in system_script
    assert "record_artifact_integrity()" in system_script
    assert "manifest_artifact_version()" in tool_script
    assert '[[ ! -x "$node_source/bin/node" ]]' in system_script
    assert 'rm -rf -- "$node_dir"' in system_script
    for tool in downloaded_tools:
        assert f"artifact_integrity_matches {tool} " in system_script
        assert f"record_artifact_integrity {tool} " in system_script

    assert "rustup_fingerprint" in tool_script
    assert '"$rustup_stamp"' in tool_script


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
def test_readiness_message_uses_runtime_config_launcher(repo_root: Path):
    message = _template(repo_root)["message"]

    assert "devbox opencode" in message
    assert "Avoid bare opencode" in message


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
    # Only the system script uses the single user variable rendered by Lima;
    # the host launcher normalizes it when computing the same fingerprint.
    for name in _SCRIPTS:
        found = re.findall(r"{{.*?}}", _script(repo_root, name))
        if name == "provision-system.sh":
            assert found == ["{{.User}}"]
        else:
            assert found == []


@pytest.mark.unit
def test_user_script_links_exactly_the_mounted_paths(repo_root: Path):
    user_script = _script(repo_root, "provision-user.sh")

    assert _shared_rels_from_loop(user_script) == EXPECTED_SHARED_RELS
    assert 'DEVBOX_SRC_ROOT="${PARAM_SrcPath:-}"' in user_script
    assert 'src) target="$DEVBOX_SRC_ROOT" ;;' in user_script
    assert ".local/state/opencode)" in user_script
    assert 'target="$HOME/.host-config/local/state/devbox-opencode"' in user_script
    assert "seed-opencode-state.py" in user_script


@pytest.mark.unit
def test_probe_checks_exactly_the_mounted_paths(repo_root: Path):
    probe = _script(repo_root, "probe-readiness.sh")

    assert _shared_rels_from_loop(probe) == EXPECTED_SHARED_RELS
    assert 'src) target="${PARAM_SrcPath:-}" ;;' in probe
    assert (
        'state_seed_options="$(findmnt -rn -M "$state_seed_mount" -o OPTIONS)"' in probe
    )
    assert "knowledge base is not readable via canonical path" in probe


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
def test_docker_ce_uses_pinned_signature_checked_rpms_and_rootless_service(
    repo_root: Path,
):
    system_script = _script(repo_root, "provision-system.sh")
    user_script = _script(repo_root, "provision-user.sh")
    manifest = _template(repo_root)
    tool_versions = (repo_root / "lima/tool-versions.json").read_text()
    docker_key = (repo_root / "lima/keys/docker-ce.asc").read_bytes()
    docker_key_sha256 = hashlib.sha256(docker_key).hexdigest()

    assert '"docker_ce"' in tool_versions
    assert '"containerd_io"' in tool_versions
    assert f"DOCKER_GPG_KEY_SHA256={docker_key_sha256}" in system_script
    assert "copy_repo_file lima/keys/docker-ce.asc" in system_script
    assert 'rpm --import "$docker_gpg_key_file"' in system_script
    assert "repo_gpgcheck=1" in system_script
    assert "file:///etc/pki/rpm-gpg/RPM-GPG-KEY-docker-ce" in system_script
    assert "[docker-ce-stable]" in system_script
    assert (
        "https://download.docker.com/linux/fedora/$releasever/$basearch/stable"
        in system_script
    )
    assert "enabled=0" in system_script
    assert "gpgcheck=1" in system_script
    assert "--enablerepo=docker-ce-stable" in system_script
    assert "manifest_version docker_ce" in system_script
    assert "manifest_version containerd_io" in system_script
    assert '"docker-ce-3:${docker_version}"' in system_script
    assert '"docker-ce-cli-1:${docker_version}"' in system_script
    assert '"docker-ce-rootless-extras-${docker_version}"' in system_script
    assert '"containerd.io-${containerd_version}"' in system_script
    assert "docker_release" not in system_script
    assert "rpm -q --queryformat '%{NAME} %{EPOCHNUM} %{VERSION}'" in system_script
    assert "--setopt=tsflags=noscripts" in system_script
    assert "assert_rpm_owner /usr/bin/docker docker-ce-cli" in system_script
    assert "assert_rpm_owner /usr/bin/dockerd docker-ce" in system_script
    assert "assert_rpm_owner /usr/bin/containerd containerd.io" in system_script
    assert "rpm -q podman-docker" in system_script
    assert (
        'KIND_EXPERIMENTAL_PROVIDER="${KIND_EXPERIMENTAL_PROVIDER:-docker}"'
        in system_script
    )
    assert 'systemctl disable --now "$unit"' in system_script
    assert 'XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"' in user_script
    assert "export XDG_RUNTIME_DIR" in user_script
    assert (
        'export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"'
        in user_script
    )
    assert "systemctl --user show-environment" in user_script
    assert "dockerd-rootless-setuptool.sh install" not in user_script
    assert "ExecStart=/usr/bin/dockerd-rootless.sh" in user_script
    assert "Requires=dbus.socket" in user_script
    assert "Type=notify" in user_script
    assert "Delegate=yes" in user_script
    assert "WantedBy=default.target" in user_script
    assert "systemctl --user daemon-reload" in user_script
    assert "systemctl --user enable docker.service" in user_script
    assert "systemctl --user start docker.service" in user_script
    assert "systemctl --user restart docker.service" in user_script
    assert "systemctl --user enable --now podman.socket" in user_script
    assert "docker" in manifest["probes"][0]["description"].lower()


@pytest.mark.unit
def test_readiness_checks_distinct_docker_ce_and_podman_endpoints(
    repo_root: Path,
):
    probe = _script(repo_root, "probe-readiness.sh")

    assert 'DOCKER_SOCKET="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/docker.sock"' in probe
    assert (
        'PODMAN_SOCKET="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock"'
        in probe
    )
    assert "systemctl --user is-active --quiet docker.service" in probe
    assert "rootful Docker system services must remain inactive" in probe
    assert "rootful Docker system services must remain disabled" in probe
    assert "the rootful system containerd service must remain inactive" in probe
    assert "the rootful system containerd service must remain disabled" in probe
    assert "the guest user must not be a member of the rootful docker group" in probe
    assert "http://d/version" in probe
    assert ".tools.docker_ce.version" in probe
    assert '--unix-socket "$PODMAN_SOCKET" http://d/_ping' in probe
    assert "the separate rootless Podman API is not responding" in probe


@pytest.mark.unit
def test_system_script_stamps_limactl_integrity_without_running_it_as_root(
    repo_root: Path,
):
    system_script = _script(repo_root, "provision-system.sh")

    assert 'artifact_integrity_matches limactl "$LIMACTL_FINGERPRINT"' in system_script
    assert 'record_artifact_integrity limactl "$LIMACTL_FINGERPRINT"' in system_script
    assert 'verify_download limactl "$arch"' in system_script
    # limactl refuses to run as root, so provisioning cannot query its version.
    assert "/usr/local/bin/limactl --version" not in system_script


@pytest.mark.unit
def test_root_manifest_path_is_canonical_and_snapshotted(repo_root: Path):
    system_script = _script(repo_root, "provision-system.sh")

    assert 'DEVBOX_SRC_ROOT="$(realpath -e -- "$DEVBOX_SRC_ROOT")"' in system_script
    assert 'DEVBOX_REPO="$(realpath -e -- "$DEVBOX_REPO")"' in system_script
    assert '[[ -L "$MANIFEST_SOURCE" || ! -f "$MANIFEST_SOURCE" ]]' in system_script
    assert 'copy_repo_file lima/tool-versions.json "$MANIFEST"' in system_script
    assert "os.O_NOFOLLOW" in system_script
    assert "copy_repo_file lima/provision-tools.sh" in system_script


@pytest.mark.unit
def test_system_provisions_opencode_tmp_without_following_or_recursing(
    repo_root: Path, tmp_path: Path
):
    system_script = _script(repo_root, "provision-system.sh")
    match = re.search(
        r"/usr/bin/python3 -I -S - /tmp opencode 0 0 <<'PY'\n(.*?)\nPY",
        system_script,
        re.DOTALL,
    )
    assert match, "descriptor-safe /tmp/opencode provisioning helper is missing"
    setup_program = match.group(1)
    assert "os.O_NOFOLLOW" in setup_program
    assert "mnt_id:" in setup_program
    assert "mount_id(directory_fd) != parent_mount_id" in setup_program
    assert "os.fchown(directory_fd, owner_uid, owner_gid)" in setup_program
    assert "os.fchmod(directory_fd, 0o1777)" in setup_program
    assert setup_program.index("mount_id(directory_fd) != parent_mount_id") < (
        setup_program.index("os.fchown(directory_fd, owner_uid, owner_gid)")
    )
    setup_position = system_script.index("/usr/bin/python3 -I -S - /tmp opencode 0 0")
    builder_install_position = system_script.index(
        'as_toolbuilder /bin/bash "$tool_script_snapshot"'
    )
    assert setup_position < builder_install_position

    parent = tmp_path / "parent"
    parent.mkdir()

    def setup() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                "-",
                str(parent),
                "opencode",
                str(os.getuid()),
                str(os.getgid()),
            ],
            input=setup_program,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )

    target = parent / "opencode"
    created = setup()
    assert created.returncode == 0, created.stderr
    created_stat = target.stat()
    assert created_stat.st_uid == os.getuid()
    assert created_stat.st_gid == os.getgid()
    assert stat.S_IMODE(created_stat.st_mode) == 0o1777

    preserved = target / "existing-content"
    preserved.write_text("leave existing entries untouched\n")
    target.chmod(0o700)
    repaired = setup()
    assert repaired.returncode == 0, repaired.stderr
    assert stat.S_IMODE(target.stat().st_mode) == 0o1777
    assert preserved.read_text() == "leave existing entries untouched\n"

    preserved.unlink()
    target.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "marker"
    marker.write_text("must not be changed\n")
    target.symlink_to(outside, target_is_directory=True)
    symlinked = setup()
    assert symlinked.returncode != 0
    assert "symlink" in symlinked.stderr
    assert marker.read_text() == "must not be changed\n"

    target.unlink()
    target.write_text("not a directory\n")
    non_directory = setup()
    assert non_directory.returncode != 0
    assert "not a directory" in non_directory.stderr
    assert target.read_text() == "not a directory\n"


@pytest.mark.unit
def test_root_snapshot_copy_rejects_symlinks(repo_root: Path, tmp_path: Path):
    system_script = _script(repo_root, "provision-system.sh")
    match = re.search(r"<<'PY'\n(.*?)\nPY\n}", system_script, re.DOTALL)
    assert match, "safe repository-copy Python helper is missing"
    copier = match.group(1)

    repo = tmp_path / "repo"
    lima = repo / "lima"
    lima.mkdir(parents=True)
    source = lima / "tool-versions.json"
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

    copied = copy("lima/tool-versions.json")
    assert copied.returncode == 0, copied.stderr
    assert output.read_text() == source.read_text()

    symlink_target = tmp_path / "outside.json"
    symlink_target.write_text('{"secret": true}\n')
    source.unlink()
    source.symlink_to(symlink_target)
    rejected_file_link = copy("lima/tool-versions.json")
    assert rejected_file_link.returncode != 0

    source.unlink()
    os.mkfifo(source)
    rejected_fifo = copy("lima/tool-versions.json")
    assert rejected_fifo.returncode != 0

    source.unlink()
    lima.rmdir()
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


@pytest.mark.unit
def test_red_hat_internal_ca_trust_is_provisioned_for_guest_tools(repo_root: Path):
    system_script = _script(repo_root, "provision-system.sh")
    source_wrapper = (repo_root / "lima" / "run-the-source-mcp.sh").read_text()

    assert 'copy_repo_file "lima/certs/$cert" "$ca_snapshot"' in system_script
    assert "'.trust_anchors[$cert]'" in system_script
    assert "sha256sum -c -" in system_script
    assert "update-ca-trust" in system_script
    assert "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt" in system_script
    assert "REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt" in system_script
    assert "SSL_CERT_FILE" in source_wrapper
    assert "REQUESTS_CA_BUNDLE" in source_wrapper
    for name in (
        "redhat-ipa-ca.crt",
        "redhat-rhcsv2-ca.crt",
        "redhat-root-ca.crt",
    ):
        certificate = repo_root / "lima" / "certs" / name
        assert "-----BEGIN CERTIFICATE-----" in certificate.read_text()
