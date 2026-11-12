"""Unit tests for the guest-visible devbox VM level marker."""

import os
import shlex
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml


def _helper(repo_root: Path) -> Path:
    return repo_root / "lima" / "provision-vm-level.sh"


def _run_helper(
    repo_root: Path, marker_dir: Path, level: str
) -> subprocess.CompletedProcess[str]:
    env = os.environ | {"DEVBOX_VM_LEVEL_DIR": str(marker_dir)}
    return subprocess.run(
        ["bash", str(_helper(repo_root)), level],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


def _probe_harness(repo_root: Path, marker: Path) -> str:
    probe = (repo_root / "lima" / "probe-readiness.sh").read_text()
    block = probe[probe.index("fail()") : probe.index("\nTOOL_BUILDER_HOME=")]
    return "set -euo pipefail\n" + block.replace(
        "VM_LEVEL_FILE=/etc/devbox/vm-level",
        f"VM_LEVEL_FILE={shlex.quote(str(marker))}",
    )


def _run_probe_harness(
    env: dict[str, str], harness: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", harness],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


def _probe_stub_environment(
    repo_root: Path,
    tmp_path: Path,
    marker: Path,
    *,
    etc_target: str = "/",
    marker_dir_target: str | None = None,
    marker_file_target: str | None = None,
    marker_fstype: str = "ext4",
    dir_metadata: str = "0:0:755",
    file_metadata: str = "0:0:644",
    stat_failure: str | None = None,
    findmnt_failure: str | None = None,
) -> tuple[dict[str, str], str]:
    marker_dir_target = (
        marker_dir_target if marker_dir_target is not None else etc_target
    )
    marker_file_target = (
        marker_file_target if marker_file_target is not None else etc_target
    )
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stat_stub = stub_dir / "stat"
    stat_stub.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'last=""\n'
        'for argument in "$@"; do last="$argument"; done\n'
        'if [[ "${MOCK_STAT_FAIL:-}" == directory ]]; then\n'
        '  if [[ "$last" == "$MOCK_MARKER_DIR" ]]; then exit 1; fi\n'
        "fi\n"
        'if [[ "${MOCK_STAT_FAIL:-}" == file ]]; then\n'
        '  if [[ "$last" == "$MOCK_MARKER_FILE" ]]; then exit 1; fi\n'
        "fi\n"
        'case "$last" in\n'
        '  "$MOCK_MARKER_DIR") printf \'%s\\n\' "$MOCK_DIR_METADATA" ;;\n'
        '  "$MOCK_MARKER_FILE") printf \'%s\\n\' "$MOCK_FILE_METADATA" ;;\n'
        "  *) exit 1 ;;\n"
        "esac\n"
    )
    stat_stub.chmod(0o755)
    findmnt_stub = stub_dir / "findmnt"
    findmnt_stub.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'args=" $* "\n'
        'path=""\n'
        "while [[ $# -gt 0 ]]; do\n"
        '  case "$1" in\n'
        '    -T) path="$2"; shift 2 ;;\n'
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        'if [[ "${MOCK_FINDMNT_FAIL:-}" == fstype ]]; then\n'
        '  if [[ "$args" == *" -o FSTYPE "* ]]; then exit 1; fi\n'
        "fi\n"
        'if [[ "${MOCK_FINDMNT_FAIL:-}" == etc ]]; then\n'
        '  if [[ "$path" == "$MOCK_ETC_DIR" ]]; then exit 1; fi\n'
        "fi\n"
        'if [[ "${MOCK_FINDMNT_FAIL:-}" == marker_dir ]]; then\n'
        '  if [[ "$path" == "$MOCK_MARKER_DIR" ]]; then exit 1; fi\n'
        "fi\n"
        'if [[ "${MOCK_FINDMNT_FAIL:-}" == marker_file ]]; then\n'
        '  if [[ "$path" == "$MOCK_MARKER_FILE" ]]; then exit 1; fi\n'
        "fi\n"
        'case "$args" in\n'
        '  *" -o FSTYPE "*) printf \'%s\\n\' "$MOCK_MARKER_FSTYPE" ;;\n'
        '  *" -o TARGET "*)\n'
        '    case "$path" in\n'
        '      "$MOCK_ETC_DIR") printf \'%s\\n\' "$MOCK_ETC_TARGET" ;;\n'
        '      "$MOCK_MARKER_DIR") printf \'%s\\n\' "$MOCK_MARKER_DIR_TARGET" ;;\n'
        '      "$MOCK_MARKER_FILE") printf \'%s\\n\' "$MOCK_MARKER_FILE_TARGET" ;;\n'
        "      *) exit 1 ;;\n"
        "    esac ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n"
    )
    findmnt_stub.chmod(0o755)
    env = os.environ | {
        "PATH": f"{stub_dir}:{os.environ['PATH']}",
        "MOCK_ETC_DIR": "/etc",
        "MOCK_ETC_TARGET": etc_target,
        "MOCK_MARKER_DIR": str(marker.parent),
        "MOCK_MARKER_DIR_TARGET": marker_dir_target,
        "MOCK_MARKER_FILE": str(marker),
        "MOCK_MARKER_FILE_TARGET": marker_file_target,
        "MOCK_MARKER_FSTYPE": marker_fstype,
        "MOCK_DIR_METADATA": dir_metadata,
        "MOCK_FILE_METADATA": file_metadata,
        "MOCK_STAT_FAIL": stat_failure or "",
        "MOCK_FINDMNT_FAIL": findmnt_failure or "",
    }
    return env, _probe_harness(repo_root, marker)


@pytest.mark.unit
@pytest.mark.parametrize("level", ["L1", "L2", "L10", "L99"])
def test_helper_atomically_writes_one_marker_line(
    repo_root: Path, tmp_path: Path, level: str
):
    marker_dir = tmp_path / "etc" / "devbox"

    result = _run_helper(repo_root, marker_dir, level)

    assert result.returncode == 0, result.stderr
    marker = marker_dir / "vm-level"
    assert marker.is_file()
    assert not marker.is_symlink()
    assert marker.read_text() == f"{level}\n"
    assert stat.S_IMODE(marker.stat().st_mode) == 0o644
    assert stat.S_IMODE(marker_dir.stat().st_mode) == 0o755
    assert list(marker_dir.glob(".vm-level.*")) == []


@pytest.mark.unit
def test_helper_accepts_the_lima_param_level(repo_root: Path, tmp_path: Path):
    marker_dir = tmp_path / "etc" / "devbox"
    env = os.environ | {
        "DEVBOX_VM_LEVEL_DIR": str(marker_dir),
        "PARAM_VmLevel": "L2",
    }

    result = subprocess.run(
        ["bash", str(_helper(repo_root))],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert (marker_dir / "vm-level").read_text() == "L2\n"


@pytest.mark.unit
@pytest.mark.parametrize("level", ["", "L0", "L01", "l1", "L1x", "L 1", "M1", "L1\nL2"])
def test_helper_rejects_invalid_levels(repo_root: Path, tmp_path: Path, level: str):
    marker_dir = tmp_path / "etc" / "devbox"

    result = _run_helper(repo_root, marker_dir, level)

    assert result.returncode != 0
    assert "must match L[1-9][0-9]*" in result.stderr
    assert not (marker_dir / "vm-level").exists()


@pytest.mark.unit
def test_helper_refuses_a_symlinked_marker(repo_root: Path, tmp_path: Path):
    marker_dir = tmp_path / "etc" / "devbox"
    marker_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_text("unchanged\n")
    (marker_dir / "vm-level").symlink_to(outside)

    result = _run_helper(repo_root, marker_dir, "L1")

    assert result.returncode != 0
    assert "symlinked VM level marker" in result.stderr
    assert outside.read_text() == "unchanged\n"


@pytest.mark.unit
def test_helper_refuses_a_symlinked_marker_directory(repo_root: Path, tmp_path: Path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    marker_dir = tmp_path / "linked"
    marker_dir.symlink_to(real_dir, target_is_directory=True)

    result = _run_helper(repo_root, marker_dir, "L1")

    assert result.returncode != 0
    assert "symlinked marker directory" in result.stderr
    assert not (real_dir / "vm-level").exists()


@pytest.mark.unit
def test_helper_refuses_a_non_regular_existing_marker(repo_root: Path, tmp_path: Path):
    marker_dir = tmp_path / "etc" / "devbox"
    (marker_dir / "vm-level").mkdir(parents=True)

    result = _run_helper(repo_root, marker_dir, "L2")

    assert result.returncode != 0
    assert "not a regular file" in result.stderr


@pytest.mark.unit
def test_helper_cleans_marker_temp_on_failure(repo_root: Path, tmp_path: Path):
    marker_dir = tmp_path / "etc" / "devbox"
    marker_dir.mkdir(parents=True)
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    real_chmod = shutil.which("chmod")
    assert real_chmod
    chmod_stub = stub_dir / "chmod"
    chmod_stub.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'if [[ "${1:-}" == 0644 ]]; then exit 1; fi\n'
        f'exec {shlex.quote(real_chmod)} "$@"\n'
    )
    chmod_stub.chmod(0o755)

    result = subprocess.run(
        ["bash", str(_helper(repo_root)), "L1"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=os.environ
        | {
            "DEVBOX_VM_LEVEL_DIR": str(marker_dir),
            "PATH": f"{stub_dir}:{os.environ['PATH']}",
        },
    )

    assert result.returncode != 0
    assert list(marker_dir.glob(".vm-level.*")) == []


@pytest.mark.unit
def test_helper_rejects_a_symlinked_parent(repo_root: Path, tmp_path: Path):
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    parent_link = tmp_path / "linked"
    parent_link.symlink_to(real_parent, target_is_directory=True)

    result = _run_helper(repo_root, parent_link / "devbox", "L1")

    assert result.returncode != 0
    assert "canonical and not pass through a symlink" in result.stderr
    assert not (real_parent / "devbox" / "vm-level").exists()


@pytest.mark.unit
@pytest.mark.skipif(os.geteuid() == 0, reason="the guard only applies to non-root use")
def test_helper_override_cannot_bypass_the_default_path_root_guard(
    repo_root: Path, tmp_path: Path
):
    link = tmp_path / "etc-devbox-link"
    link.symlink_to("/etc/devbox", target_is_directory=True)

    for marker_dir in ("/etc/devbox/", str(link)):
        result = subprocess.run(
            ["bash", str(_helper(repo_root)), "L1"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=os.environ | {"DEVBOX_VM_LEVEL_DIR": marker_dir},
        )
        assert result.returncode != 0
        assert (
            "requires root" in result.stderr
            or "symlinked marker directory" in result.stderr
        )


@pytest.mark.unit
def test_writer_and_reader_pin_the_same_marker_contract(repo_root: Path):
    helper = _helper(repo_root).read_text()
    probe = (repo_root / "lima" / "probe-readiness.sh").read_text()

    for source in (helper, probe):
        assert "L[1-9][0-9]*" in source
        assert "mode 0644" in source
        assert "mode-0755" in source
    assert "synchronized with probe-readiness.sh" in helper
    assert "synchronized with provision-vm-level.sh" in probe
    assert "chmod 0644" in helper
    assert "chmod 0755" in helper
    assert '"0:0:644"' in probe
    assert '"0:0:755"' in probe


@pytest.mark.unit
def test_helper_checks_for_host_mounts_and_root_guard(repo_root: Path):
    helper = _helper(repo_root).read_text()

    assert 'marker_dir_real="$(realpath -m -- "$marker_dir")"' in helper
    assert 'if [[ "$marker_dir_real" == /etc/devbox && "$(id -u)" != 0 ]]' in helper
    assert "findmnt -rn -T /etc -o TARGET" in helper
    assert 'findmnt -rn -T "$marker_dir" -o TARGET' in helper
    assert 'findmnt -rn -T "$marker_dir" -o FSTYPE' in helper
    assert "expected root:root mode 0755" in helper
    assert 'mktemp "$marker_dir/.vm-level.XXXXXX"' in helper
    assert 'mv -f -- "$marker_tmp" "$marker"' in helper
    assert "chown root:root" in helper
    assert "state_dir=/var/lib/devbox-vm" in helper
    assert 'stamp="$state_dir/vm-level-provision.sha256"' in helper
    assert 'sha256sum "$0"' in helper
    assert 'rm -f -- "$marker_tmp"' in helper
    assert 'rm -f -- "$stamp_tmp"' in helper
    assert 'marker_tmp=""' in helper
    assert 'stamp_tmp=""' in helper
    assert helper.count("trap - EXIT") == 1
    assert helper.index("trap cleanup_temp_files EXIT") < helper.index(
        'stamp_tmp="$(mktemp'
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("content", "expected_error"),
    (
        ("L1\n", None),
        ("L0\n", "invalid level"),
        ("L1\nL2\n", "invalid level"),
        ("L1\n\n", "exactly one line"),
        ("L1", "exactly one line"),
    ),
)
def test_readiness_validates_present_marker_content(
    repo_root: Path,
    tmp_path: Path,
    content: str,
    expected_error: str | None,
):
    marker_dir = tmp_path / "etc" / "devbox"
    marker_dir.mkdir(parents=True)
    marker = marker_dir / "vm-level"
    marker.write_text(content)
    env, harness = _probe_stub_environment(repo_root, tmp_path, marker)

    result = _run_probe_harness(env, harness)

    if expected_error is None:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0
        assert expected_error in result.stderr


@pytest.mark.unit
@pytest.mark.parametrize("target", ("directory", "file"))
def test_readiness_rejects_wrong_marker_metadata(
    repo_root: Path, tmp_path: Path, target: str
):
    marker_dir = tmp_path / "etc" / "devbox"
    marker_dir.mkdir(parents=True)
    marker = marker_dir / "vm-level"
    marker.write_text("L1\n")
    dir_metadata = "0:0:700" if target == "directory" else "0:0:755"
    file_metadata = "0:0:600" if target == "file" else "0:0:644"
    env, harness = _probe_stub_environment(
        repo_root,
        tmp_path,
        marker,
        dir_metadata=dir_metadata,
        file_metadata=file_metadata,
    )
    expected = (
        f"{marker_dir} has {dir_metadata}; expected root:root mode 0755"
        if target == "directory"
        else f"{marker} has {file_metadata}; expected root:root mode 0644"
    )

    result = _run_probe_harness(env, harness)

    assert result.returncode != 0
    assert result.stderr.strip() == f"devbox readiness: {expected}"


@pytest.mark.unit
@pytest.mark.parametrize("target", ("directory", "file"))
def test_readiness_fails_closed_when_stat_fails(
    repo_root: Path, tmp_path: Path, target: str
):
    marker_dir = tmp_path / "etc" / "devbox"
    marker_dir.mkdir(parents=True)
    marker = marker_dir / "vm-level"
    marker.write_text("L1\n")
    env, harness = _probe_stub_environment(
        repo_root, tmp_path, marker, stat_failure=target
    )
    inspected = marker_dir if target == "directory" else marker

    result = _run_probe_harness(env, harness)

    assert result.returncode != 0
    assert result.stderr.strip() == f"devbox readiness: cannot inspect {inspected}"


@pytest.mark.unit
@pytest.mark.parametrize("target", ("fstype", "etc", "marker_dir", "marker_file"))
def test_readiness_fails_closed_when_findmnt_fails(
    repo_root: Path, tmp_path: Path, target: str
):
    marker_dir = tmp_path / "etc" / "devbox"
    marker_dir.mkdir(parents=True)
    marker = marker_dir / "vm-level"
    marker.write_text("L1\n")
    env, harness = _probe_stub_environment(
        repo_root, tmp_path, marker, findmnt_failure=target
    )
    expected = {
        "fstype": f"cannot inspect the filesystem type for {marker_dir}",
        "etc": "cannot inspect the filesystem containing /etc",
        "marker_dir": f"cannot inspect the filesystem containing {marker_dir}",
        "marker_file": f"cannot inspect the filesystem containing {marker}",
    }[target]

    result = _run_probe_harness(env, harness)

    assert result.returncode != 0
    assert result.stderr.strip() == f"devbox readiness: {expected}"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("marker_fstype", "marker_dir_target", "marker_file_target", "expected_error"),
    (
        ("9p", None, None, "host-shared filesystem"),
        ("virtiofs", None, None, "host-shared filesystem"),
        ("ext4", "/different-filesystem", None, "different filesystem than /etc"),
        ("ext4", None, "/different-filesystem", "different filesystem than /etc"),
    ),
)
def test_readiness_rejects_unsafe_marker_filesystems(
    repo_root: Path,
    tmp_path: Path,
    marker_fstype: str,
    marker_dir_target: str | None,
    marker_file_target: str | None,
    expected_error: str,
):
    marker_dir = tmp_path / "etc" / "devbox"
    marker_dir.mkdir(parents=True)
    marker = marker_dir / "vm-level"
    marker.write_text("L1\n")
    env, harness = _probe_stub_environment(
        repo_root,
        tmp_path,
        marker,
        marker_fstype=marker_fstype,
        marker_dir_target=marker_dir_target,
        marker_file_target=marker_file_target,
    )

    result = _run_probe_harness(env, harness)

    assert result.returncode != 0
    assert expected_error in result.stderr


@pytest.mark.unit
def test_template_embeds_the_level_provisioner(repo_root: Path):
    template = yaml.safe_load((repo_root / "lima" / "devbox.yaml").read_text())
    system_script = (repo_root / "lima" / "provision-system.sh").read_text()

    assert template["provision"] == [
        {"mode": "system", "file": "provision-vm-level.sh"},
        {"mode": "system", "file": "provision-system.sh"},
        {"mode": "user", "file": "provision-user.sh"},
    ]
    assert 'DEVBOX_VM_LEVEL="${PARAM_VmLevel:-}"' in system_script
    assert '[[ ! "$DEVBOX_VM_LEVEL" =~ ^L[1-9][0-9]*$ ]]' in system_script
    # Startup must not read or execute the marker helper from the live checkout.
    assert "copy_repo_file lima/provision-vm-level.sh" not in system_script
    assert 'bash "$vm_level_script"' not in system_script


@pytest.mark.unit
def test_readiness_validates_the_level_marker(repo_root: Path):
    probe = (repo_root / "lima" / "probe-readiness.sh").read_text()

    for needle in (
        "VM_LEVEL_FILE=/etc/devbox/vm-level",
        "expected root:root mode 0755",
        "findmnt -rn -T /etc -o TARGET",
        'findmnt -rn -T "$marker_dir" -o TARGET',
        'findmnt -rn -T "$marker_dir" -o FSTYPE',
        "on host-shared filesystem",
        'findmnt -rn -T "$VM_LEVEL_FILE" -o TARGET',
        "is missing or not a regular file",
        '"$metadata" != "0:0:644"',
        "expected L[1-9][0-9]*",
        "must contain exactly one line",
    ):
        assert needle in probe


@pytest.mark.unit
def test_legacy_provisioning_artifacts_alone_do_not_assign_a_level(
    repo_root: Path, tmp_path: Path
):
    marker = tmp_path / "vm-level"
    (tmp_path / "tool-versions.json").write_text("{}")
    (tmp_path / "devbox-vm").mkdir()
    env, harness = _probe_stub_environment(repo_root, tmp_path, marker)

    result = _run_probe_harness(env, harness)

    assert result.returncode != 0
    assert "missing or not a regular file" in result.stderr
    assert "/etc/devbox/tool-versions.json" not in harness
    assert "/var/lib/devbox-vm" not in harness


@pytest.mark.unit
def test_l1_creation_paths_pass_the_l1_level(repo_root: Path):
    assert '"VmLevel=L1"' in (repo_root / "lima" / "devbox").read_text()
    assert "--param VmLevel=L1" in (repo_root / "scripts" / "run-vm-ci.sh").read_text()
    assert '"VmLevel=L1"' in (repo_root / "tests" / "conftest.py").read_text()


@pytest.mark.unit
def test_recursive_templates_stage_and_embed_the_l2_provisioner(repo_root: Path):
    source = (repo_root / "tests" / "vm" / "test_recursive_vm.py").read_text()

    assert "file: provision-vm-level.sh" in source
    assert '"file": "provision-vm-level.sh"' in source
    assert source.count("--param VmLevel=L2") == 4
    assert 'install -m 0644 "$repo_copy/lima/provision-vm-level.sh"' in source
    assert 'mountPoint": "/opt/devbox-lima"' not in source
    assert '[[ "$(cat "$marker")" == L2 ]]' in source
