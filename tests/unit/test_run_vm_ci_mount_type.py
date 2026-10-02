import subprocess
from pathlib import Path

import pytest


def _run_runner(
    repo_root: Path,
    tmp_path: Path,
    tier: str,
    mount_type: str | None,
    *,
    effective_mount_types: tuple[str, str] | None = None,
    findmnt_failure_on: int | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    limactl_log = tmp_path / "limactl.log"
    limactl_environment_log = tmp_path / "limactl-environment.log"
    mktemp_log = tmp_path / "mktemp.log"

    limactl = fake_bin / "limactl"
    limactl.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\\0\' "$@" >>"$LIMACTL_LOG"\n'
        "printf '\\0' >>\"$LIMACTL_LOG\"\n"
        "if [[ ${DEVBOX_VM_TEST_MOUNT_TYPE+x} == x ]]; then\n"
        '  printf "set\\n" >>"$LIMACTL_ENVIRONMENT_LOG"\n'
        "fi\n"
        'if [[ "${1:-}" == shell && "$*" == *\'findmnt -rn -T\'* ]]; then\n'
        "  findmnt_count=0\n"
        '  if [[ -r "$MOCK_FINDMNT_COUNT" ]]; then\n'
        '    read -r findmnt_count <"$MOCK_FINDMNT_COUNT"\n'
        "  fi\n"
        "  findmnt_count=$((findmnt_count + 1))\n"
        '  printf \'%s\\n\' "$findmnt_count" >"$MOCK_FINDMNT_COUNT"\n'
        '  if [[ "${MOCK_FINDMNT_FAIL_ON:-}" == "$findmnt_count" ]]; then\n'
        "    printf 'mock findmnt failed\\n' >&2\n"
        "    exit 42\n"
        "  fi\n"
        '  if [[ "$findmnt_count" == 1 ]]; then\n'
        "    printf '%s\\n' \"$MOCK_FSTYPE_FIRST\"\n"
        "  else\n"
        "    printf '%s\\n' \"$MOCK_FSTYPE_RESTART\"\n"
        "  fi\n"
        "  exit 0\n"
        "fi\n"
        'if [[ "${1:-}" == shell ]]; then\n'
        '  outbound_dir="$HOME/.local/share/opencode"\n'
        "  printf 'L1-to-host\\n' >\"$outbound_dir/issue-287-mount-outbound\"\n"
        "fi\n"
        'if [[ "${1:-}" == start ]]; then\n'
        '  [[ "${MOCK_LIMACTL_START_SUCCEEDS:-}" == true ]] || exit 97\n'
        "fi\n"
    )
    limactl.chmod(0o755)

    if effective_mount_types is not None:
        fake_python3 = fake_bin / "python3"
        fake_python3.write_text(
            "#!/usr/bin/env bash\n"
            'printf \'%s\\0\' "$@" >>"$MOCK_PYTHON_LOG"\n'
            "printf '\\0' >>\"$MOCK_PYTHON_LOG\"\n"
            'if [[ " $* " == *" --prepare "* ]]; then\n'
            "  printf 'issue-287-test-only-00000000000000000000000000000000\\n'\n"
            "  exit 0\n"
            "fi\n"
            'if [[ " $* " == *" --scratch-name "* ]]; then\n'
            '  "$FAKE_LIMACTL" stop devbox\n'
            "  exit $?\n"
            "fi\n"
            'exec /usr/bin/python3 "$@"\n'
        )
        fake_python3.chmod(0o755)

    mktemp = fake_bin / "mktemp"
    mktemp.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\\0\' "$@" >>"$MKTEMP_LOG"\n'
        "printf '\\0' >>\"$MKTEMP_LOG\"\n"
        'exec /usr/bin/mktemp "$@"\n'
    )
    mktemp.chmod(0o755)

    env = {
        "PATH": f"{fake_bin}:/usr/local/bin:/usr/bin:/bin",
        "RUNNER_TEMP": str(runner_temp),
        "LIMACTL_LOG": str(limactl_log),
        "LIMACTL_ENVIRONMENT_LOG": str(limactl_environment_log),
        "MKTEMP_LOG": str(mktemp_log),
    }
    if effective_mount_types is not None:
        env.update(
            {
                "FAKE_LIMACTL": str(limactl),
                "MOCK_FINDMNT_COUNT": str(tmp_path / "findmnt-count"),
                "MOCK_FINDMNT_FAIL_ON": str(findmnt_failure_on or ""),
                "MOCK_FSTYPE_FIRST": effective_mount_types[0],
                "MOCK_FSTYPE_RESTART": effective_mount_types[1],
                "MOCK_LIMACTL_START_SUCCEEDS": "true",
                "MOCK_PYTHON_LOG": str(tmp_path / "python3.log"),
            }
        )
    if mount_type is not None:
        env["DEVBOX_VM_TEST_MOUNT_TYPE"] = mount_type

    result = subprocess.run(
        ["bash", str(repo_root / "scripts/run-vm-ci.sh"), tier],
        check=False,
        capture_output=True,
        text=True,
        cwd=repo_root,
        env=env,
        timeout=30,
    )
    return result, runner_temp, limactl_log, limactl_environment_log, mktemp_log


def _logged_calls(path: Path) -> list[tuple[str, ...]]:
    if not path.exists():
        return []
    return [
        tuple(argument.decode() for argument in call.split(b"\0"))
        for call in path.read_bytes().split(b"\0\0")
        if call
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    "mount_type",
    ["", "nfs", "virtiofs --mount-type 9p"],
    ids=["empty", "unsupported", "not-shell-split"],
)
def test_invalid_mount_type_fails_before_temp_root_or_vm_creation(
    repo_root: Path, tmp_path: Path, mount_type: str
):
    result, runner_temp, limactl_log, _environment_log, mktemp_log = _run_runner(
        repo_root, tmp_path, "vm", mount_type
    )

    assert result.returncode == 2
    assert "DEVBOX_VM_TEST_MOUNT_TYPE" in result.stderr
    assert not mktemp_log.exists()
    assert not limactl_log.exists()
    assert list(runner_temp.iterdir()) == []


@pytest.mark.unit
@pytest.mark.parametrize("mount_type", ["", "9p", "virtiofs"])
def test_recursive_tier_rejects_any_set_mount_type_before_temp_root_or_vm_creation(
    repo_root: Path, tmp_path: Path, mount_type: str
):
    result, runner_temp, limactl_log, _environment_log, mktemp_log = _run_runner(
        repo_root, tmp_path, "recursive", mount_type
    )

    assert result.returncode == 2
    assert "only valid for the vm tier" in result.stderr
    assert not mktemp_log.exists()
    assert not limactl_log.exists()
    assert list(runner_temp.iterdir()) == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mount_type", "expected_mount_type"),
    [(None, None), ("9p", "9p"), ("virtiofs", "virtiofs")],
    ids=["template-default", "9p-override", "virtiofs-experiment"],
)
def test_vm_runner_encodes_mount_type_in_first_start_argv(
    repo_root: Path,
    tmp_path: Path,
    mount_type: str | None,
    expected_mount_type: str | None,
):
    result, runner_temp, limactl_log, environment_log, mktemp_log = _run_runner(
        repo_root, tmp_path, "vm", mount_type
    )

    # The fake limactl deliberately fails the first start after recording argv.
    # This prevents any VM from being created while exercising the actual runner.
    assert result.returncode == 97, result.stderr
    assert mktemp_log.exists()
    assert list(runner_temp.iterdir()) == []
    calls = _logged_calls(limactl_log)
    start_calls = [call for call in calls if call[0] == "start"]
    assert len(start_calls) == 1
    start_argv = start_calls[0]
    assert start_argv[:2] == ("start", "--yes")
    mount_type_positions = [
        index for index, argument in enumerate(start_argv) if argument == "--mount-type"
    ]
    if expected_mount_type is None:
        assert mount_type_positions == []
    else:
        assert len(mount_type_positions) == 1
        mount_type_index = mount_type_positions[0]
        assert start_argv[mount_type_index : mount_type_index + 2] == (
            "--mount-type",
            expected_mount_type,
        )
        assert start_argv[mount_type_index + 2] == str(repo_root / "lima/devbox.yaml")

    # The runner-only setting is converted to argv, then removed from the
    # environment so later host and guest commands cannot inherit it.
    assert not environment_log.exists()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("effective_mount_types", "expected_status", "expected_start_count"),
    [
        (("virtiofs", "virtiofs"), 0, 2),
        (("9p", "virtiofs"), 1, 1),
        (("virtiofs", "9p"), 1, 2),
    ],
    ids=["match-through-restart", "first-start-mismatch", "restart-mismatch"],
)
def test_vm_runner_checks_requested_and_effective_mount_types(
    repo_root: Path,
    tmp_path: Path,
    effective_mount_types: tuple[str, str],
    expected_status: int,
    expected_start_count: int,
):
    result, runner_temp, limactl_log, environment_log, _mktemp_log = _run_runner(
        repo_root,
        tmp_path,
        "vm",
        "virtiofs",
        effective_mount_types=effective_mount_types,
    )

    assert result.returncode == expected_status, result.stderr
    assert "OpenCode data mount (first start)" in result.stderr
    assert f"requested=virtiofs effective={effective_mount_types[0]}" in result.stderr
    assert list(runner_temp.iterdir()) == []
    assert not environment_log.exists()

    calls = _logged_calls(limactl_log)
    start_calls = [call for call in calls if call[0] == "start"]
    mount_checks = [
        call
        for call in calls
        if call[0] == "shell" and "findmnt -rn -T" in " ".join(call)
    ]
    assert len(start_calls) == expected_start_count
    assert len(mount_checks) == expected_start_count
    assert "--mount-type" in start_calls[0]
    assert "virtiofs" in start_calls[0]
    if expected_start_count == 2:
        assert "--mount-type" not in start_calls[1]
        assert "OpenCode data mount (SQLite probe stop/restart)" in result.stderr
        assert f"requested=virtiofs effective={effective_mount_types[1]}" in (
            result.stderr
        )


@pytest.mark.unit
def test_vm_runner_stops_before_sqlite_coordinator_when_findmnt_fails(
    repo_root: Path, tmp_path: Path
):
    result, runner_temp, limactl_log, environment_log, _mktemp_log = _run_runner(
        repo_root,
        tmp_path,
        "vm",
        "virtiofs",
        effective_mount_types=("virtiofs", "virtiofs"),
        findmnt_failure_on=1,
    )

    assert result.returncode == 1
    assert "requested=virtiofs effective=unavailable (findmnt failed)" in (
        result.stderr
    )
    assert "mock findmnt failed" in result.stderr
    assert list(runner_temp.iterdir()) == []
    assert not environment_log.exists()

    calls = _logged_calls(limactl_log)
    start_calls = [call for call in calls if call[0] == "start"]
    shell_calls = [call for call in calls if call[0] == "shell"]
    mount_checks = [call for call in shell_calls if "findmnt -rn -T" in " ".join(call)]
    assert len(start_calls) == 1
    assert len(mount_checks) == 1
    assert shell_calls == mount_checks

    # Preparing the disposable database happens before L1 starts; the actual
    # coordinator and its guest worker must not start after a failed mount check.
    python_calls = _logged_calls(tmp_path / "python3.log")
    assert len(python_calls) == 1
    assert "--prepare" in python_calls[0]
    assert all("--scratch-name" not in call for call in python_calls)


@pytest.mark.unit
def test_vm_runner_does_not_assert_mount_type_when_override_is_unset(
    repo_root: Path, tmp_path: Path
):
    result, runner_temp, limactl_log, environment_log, _mktemp_log = _run_runner(
        repo_root,
        tmp_path,
        "vm",
        None,
        effective_mount_types=("nfs", "nfs"),
    )

    assert result.returncode == 0, result.stderr
    assert "OpenCode data mount" not in result.stderr
    assert list(runner_temp.iterdir()) == []
    assert not environment_log.exists()
    calls = _logged_calls(limactl_log)
    start_calls = [call for call in calls if call[0] == "start"]
    mount_checks = [
        call
        for call in calls
        if call[0] == "shell" and "findmnt -rn -T" in " ".join(call)
    ]
    assert len(start_calls) == 2
    assert all("--mount-type" not in call for call in start_calls)
    assert mount_checks == []


@pytest.mark.unit
def test_recursive_tier_without_mount_override_starts_with_template_default(
    repo_root: Path, tmp_path: Path
):
    result, runner_temp, limactl_log, environment_log, mktemp_log = _run_runner(
        repo_root, tmp_path, "recursive", None
    )

    # The fake limactl fails the initial start after recording its arguments.
    assert result.returncode == 97, result.stderr
    assert mktemp_log.exists()
    assert list(runner_temp.iterdir()) == []
    start_calls = [call for call in _logged_calls(limactl_log) if call[0] == "start"]
    assert len(start_calls) == 1
    assert "--mount-type" not in start_calls[0]
    assert not environment_log.exists()


@pytest.mark.unit
def test_mount_type_uses_a_quoted_argv_array_and_is_not_guest_environment(
    repo_root: Path,
):
    runner = (repo_root / "scripts/run-vm-ci.sh").read_text(encoding="utf-8")

    assert "mount_type_args=()" in runner
    assert 'requested_mount_type="${DEVBOX_VM_TEST_MOUNT_TYPE-}"' in runner
    assert 'mount_type_args=(--mount-type "$requested_mount_type")' in runner
    assert runner.count('"${mount_type_args[@]}"') == 1
    assert "limactl start --yes --timeout 60m devbox" in runner
    assert 'findmnt -rn -T "$HOME/.local/share/opencode" -o FSTYPE' in runner
    assert runner.count('verify_requested_mount_type "') == 2
    guest_command = runner.split(
        "# Run the test wrapper directly in the guest.", maxsplit=1
    )[1]
    assert "DEVBOX_VM_TEST_MOUNT_TYPE" not in guest_command
