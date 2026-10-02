import getpass
import json
import os
import signal
import stat
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.conftest import expected_lima_provisioning_fingerprint, run_bash_script


def _install_lima_shim(
    tmp_path: Path,
    env: dict[str, str],
    *,
    status: str = "Running",
    fingerprint: str = "stale-fingerprint",
    protected: bool = True,
) -> tuple[Path, Path]:
    bin_dir = tmp_path / "lima-bin"
    bin_dir.mkdir()
    calls = tmp_path / "limactl-calls.log"
    status_file = tmp_path / "lima-status"
    status_file.write_text(status)
    protection_file = tmp_path / "lima-protected"
    protection_file.write_text("true" if protected else "false")
    fingerprint_file = tmp_path / "vm-fingerprint"
    fingerprint_file.write_text(fingerprint)
    shell_capture = tmp_path / "shell-capture.json"

    limactl = bin_dir / "limactl"
    limactl.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
trap 'exit 131' QUIT
trap 'exit 141' PIPE
python3 - "$MOCK_LIMACTL_CALLS" "$@" <<'PY'
import json
import sys
with open(sys.argv[1], "a") as output:
    output.write(json.dumps(sys.argv[2:]) + chr(10))
PY
fail_if_requested() {
  local operation="$1" failure_status=""
  if [[ -n "${MOCK_LIMA_FAIL_COMMAND:-}" \
    && "$operation" == "$MOCK_LIMA_FAIL_COMMAND" ]]; then
    failure_status="${MOCK_LIMA_FAIL_STATUS:-1}"
  elif [[ -n "${MOCK_LIMA_FAIL_COMMAND_2:-}" \
    && "$operation" == "$MOCK_LIMA_FAIL_COMMAND_2" ]]; then
    failure_status="${MOCK_LIMA_FAIL_STATUS_2:-1}"
  fi
  if [[ -n "$failure_status" ]]; then
    echo "mock limactl failure: $operation" >&2
    exit "$failure_status"
  fi
}
if [[ "${1:-}" == list && -n "${MOCK_LIMA_FAIL_FORMAT:-}" \
  && "${3:-}" == "$MOCK_LIMA_FAIL_FORMAT" ]]; then
  echo "mock limactl list failure" >&2
  exit "${MOCK_LIMA_FAIL_FORMAT_STATUS:-1}"
fi
case "${1:-}" in
  list)
    status="$(cat "$MOCK_LIMA_STATUS_FILE")"
    case "${3:-}" in
      '{{.Name}} {{.Protected}}')
        if [[ -n "$status" ]]; then
          printf '%s %s\n' "$MOCK_LIMA_INSTANCE" "$(cat "$MOCK_LIMA_PROTECTED_FILE")"
        fi
        ;;
      '{{.Name}}')
        if [[ -n "$status" ]]; then
          printf '%s\n' "$MOCK_LIMA_INSTANCE"
        fi
        ;;
      *)
        if [[ -n "$status" ]]; then
          printf '%s %s\n' "$MOCK_LIMA_INSTANCE" "$status"
        fi
        ;;
    esac
    ;;
  start)
    fail_if_requested "$1"
    printf 'Running' >"$MOCK_LIMA_STATUS_FILE"
    if [[ -n "${MOCK_VM_FINGERPRINT_AFTER_START:-}" ]]; then
      printf '%s' "$MOCK_VM_FINGERPRINT_AFTER_START" >"$MOCK_VM_FINGERPRINT_FILE"
    fi
    ;;
  stop|factory-reset)
    fail_if_requested "$1"
    printf 'Stopped' >"$MOCK_LIMA_STATUS_FILE"
    ;;
  unprotect)
    printf 'false' >"$MOCK_LIMA_PROTECTED_FILE"
    fail_if_requested "$1"
    ;;
  protect)
    fail_if_requested "$1"
    printf 'true' >"$MOCK_LIMA_PROTECTED_FILE"
    ;;
  delete)
    fail_if_requested "$1"
    case "${MOCK_LIMA_DELETE_MODE:-}" in
      before)
        : >"$MOCK_LIMA_DELETE_STARTED_FILE"
        while [[ ! -e "$MOCK_LIMA_DELETE_CONTINUE_FILE" ]]; do
          sleep 0.02
        done
        : >"$MOCK_LIMA_STATUS_FILE"
        ;;
      after)
        : >"$MOCK_LIMA_STATUS_FILE"
        : >"$MOCK_LIMA_DELETE_STARTED_FILE"
        while [[ ! -e "$MOCK_LIMA_DELETE_CONTINUE_FILE" ]]; do
          sleep 0.02
        done
        ;;
      *)
        : >"$MOCK_LIMA_STATUS_FILE"
        ;;
    esac
    ;;
  shell)
    fail_if_requested "$1"
    if [[ "$*" == *provisioning.fingerprint* ]]; then
      cat "$MOCK_VM_FINGERPRINT_FILE"
    else
      python3 - "$MOCK_SHELL_CAPTURE" "$@" <<'PY'
import json
import os
import sys
names = os.environ.get("LIMA_SHELLENV_ALLOW", "").split(",")
argv = sys.argv[2:]
payload = {
    "args": " ".join(argv),
    "argv": argv,
    "allow": names,
    "block": os.environ.get("LIMA_SHELLENV_BLOCK"),
    "provider_env": {name: os.environ[name] for name in names if name in os.environ},
}
with open(sys.argv[1], "w") as output:
    json.dump(payload, output)
PY
      exit "${MOCK_LIMA_COMMAND_EXIT_STATUS:-0}"
    fi
    ;;
  *)
    echo "unexpected limactl command: $*" >&2
    exit 1
    ;;
esac
"""
    )
    limactl.chmod(limactl.stat().st_mode | stat.S_IEXEC)

    gh = bin_dir / "gh"
    gh.write_text("#!/usr/bin/env bash\nexit 1\n")
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)

    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["MOCK_LIMACTL_CALLS"] = str(calls)
    env["MOCK_LIMA_STATUS_FILE"] = str(status_file)
    env["MOCK_LIMA_PROTECTED_FILE"] = str(protection_file)
    env["MOCK_LIMA_INSTANCE"] = env.get("DEVBOX_LIMA_INSTANCE", "devbox")
    env["MOCK_VM_FINGERPRINT_FILE"] = str(fingerprint_file)
    env["MOCK_SHELL_CAPTURE"] = str(shell_capture)
    return calls, shell_capture


def _read_lima_calls(calls: Path) -> list[list[str]]:
    return [json.loads(line) for line in calls.read_text().splitlines()]


def _interrupt_blocked_delete(
    devbox_path: Path,
    project_dir: Path,
    env: dict[str, str],
    sig: signal.Signals,
) -> subprocess.CompletedProcess[str]:
    command = [str(devbox_path), "--delete"]
    process = subprocess.Popen(
        command,
        cwd=project_dir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    started_file = Path(env["MOCK_LIMA_DELETE_STARTED_FILE"])
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not started_file.exists():
            if process.poll() is not None:
                break
            time.sleep(0.01)
        if not started_file.exists():
            stdout, stderr = process.communicate(timeout=2)
            raise AssertionError(
                f"delete shim did not block; exit={process.returncode}, "
                f"stdout={stdout!r}, stderr={stderr!r}"
            )
        os.killpg(process.pid, sig)
        stdout, stderr = process.communicate(timeout=15)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)

    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


@pytest.fixture
def project_dir(isolated_env: dict[str, str]) -> Path:
    project = Path(isolated_env["HOME"]) / "src" / "sample-project"
    project.mkdir(parents=True)
    return project


@pytest.mark.unit
def test_default_launcher_starts_vm_and_enters_same_project_path(
    devbox_path: Path,
    isolated_env: dict[str, str],
    project_dir: Path,
    tmp_path: Path,
):
    calls, capture = _install_lima_shim(
        tmp_path, isolated_env, status="Stopped", fingerprint="unused"
    )

    result = run_bash_script(devbox_path, cwd=project_dir, env=isolated_env, timeout=15)

    assert result.returncode == 0, result.stderr
    logged = _read_lima_calls(calls)
    assert ["start", "devbox"] in logged
    shell_call = next(call for call in logged if "--workdir" in call)
    workdir_index = shell_call.index("--workdir")
    assert shell_call[workdir_index + 1] == str(project_dir)
    assert shell_call[workdir_index + 2] == "devbox"
    payload = json.loads(capture.read_text())
    assert "--start" not in shell_call
    assert "--start" not in payload["args"]
    assert "--preserve-env" in payload["args"]


@pytest.mark.unit
def test_default_launcher_creates_missing_vm_from_checkout_template(
    devbox_path: Path,
    repo_root: Path,
    isolated_env: dict[str, str],
    tmp_path: Path,
):
    src_root = repo_root.parent
    (Path(isolated_env["HOME"]) / "src").symlink_to(src_root, target_is_directory=True)
    calls, _ = _install_lima_shim(
        tmp_path, isolated_env, status="", fingerprint="unused"
    )

    result = run_bash_script(devbox_path, cwd=repo_root, env=isolated_env, timeout=15)

    assert result.returncode == 0, result.stderr
    logged = _read_lima_calls(calls)
    create_call = next(
        call for call in logged if call[:3] == ["start", "--yes", "--name"]
    )
    assert f"{repo_root}/lima/devbox.yaml" in create_call
    assert f"SrcPath={src_root}" in create_call
    assert f"RepoPath={repo_root}" in create_call
    assert f"KbPath={Path(isolated_env['HOME']) / 'kb'}" in create_call
    assert create_call.count("--param") == 5
    assert any("--workdir" in call for call in logged)


@pytest.mark.unit
def test_running_vm_is_fast_and_opencode_uses_allowlisted_environment(
    devbox_path: Path,
    repo_root: Path,
    isolated_env: dict[str, str],
    project_dir: Path,
    tmp_path: Path,
):
    calls, capture = _install_lima_shim(
        tmp_path,
        isolated_env,
        fingerprint=expected_lima_provisioning_fingerprint(repo_root),
    )
    gemini_alias = "GOOGLE_GENERATIVE_AI_API_KEY"
    isolated_env["GEMINI_API_KEY"] = "mock-gemini-token"  # pragma: allowlist secret
    isolated_env[gemini_alias] = "wrong-alias"  # pragma: allowlist secret
    isolated_env["AWS_SECRET_ACCESS_KEY"] = (
        "must-not-forward"  # pragma: allowlist secret
    )

    result = run_bash_script(
        devbox_path, ["opencode"], cwd=project_dir, env=isolated_env, timeout=15
    )

    assert result.returncode == 0, result.stderr
    logged = _read_lima_calls(calls)
    assert not any(call[0] == "start" for call in logged)
    shell_call = next(call for call in logged if "--workdir" in call)
    assert any("opencode" in arg for arg in shell_call)
    payload = json.loads(capture.read_text())
    assert payload["block"] == "*"
    gemini_env_name = "GEMINI_API_KEY"  # pragma: allowlist secret
    assert payload["provider_env"][gemini_env_name] == "mock-gemini-token"
    assert payload["provider_env"][gemini_alias] == "mock-gemini-token"
    unlisted_key = "AWS_" + "SECRET_ACCESS_KEY"
    assert unlisted_key not in payload["allow"]
    assert "provisioning is stale" not in result.stderr


@pytest.mark.unit
def test_opencode_launch_builds_runtime_config_inside_the_vm(
    devbox_path: Path,
    repo_root: Path,
    isolated_env: dict[str, str],
    project_dir: Path,
    tmp_path: Path,
):
    calls, capture = _install_lima_shim(
        tmp_path,
        isolated_env,
        fingerprint=expected_lima_provisioning_fingerprint(repo_root),
    )

    result = run_bash_script(
        devbox_path,
        ["opencode", "run", "--agent", "build", "--model", "octo-open/test"],
        cwd=project_dir,
        env=isolated_env,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    shell_call = next(
        call
        for call in _read_lima_calls(calls)
        if call[0] == "shell" and "--workdir" in call
    )
    shell_command = " ".join(shell_call)
    assert "bash -c" in shell_command
    assert f"{repo_root}/lima/opencode_config.py" in shell_command
    assert "OPENCODE_CONFIG_CONTENT" in shell_command
    assert 'exec opencode "$@"' in shell_command
    assert "run --agent build --model octo-open/test" in shell_command
    payload = json.loads(capture.read_text())
    assert payload["block"] == "*"
    assert "OPENCODE_CONFIG_CONTENT" not in payload["allow"]


@pytest.mark.unit
def test_do_one_issue_runs_git_and_headless_opencode_through_vm_launcher(
    repo_root: Path, isolated_env: dict[str, str], tmp_path: Path
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls_file = tmp_path / "devbox-calls.jsonl"
    devbox = bin_dir / "devbox"
    devbox.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['MOCK_DEVBOX_CALLS'], 'a') as calls:\n"
        "    calls.write(json.dumps(sys.argv[1:]) + '\\n')\n"
    )
    devbox.chmod(devbox.stat().st_mode | stat.S_IEXEC)
    env = isolated_env | {
        "PATH": f"{bin_dir}:{isolated_env['PATH']}",
        "MOCK_DEVBOX_CALLS": str(calls_file),
    }

    result = run_bash_script(repo_root / "do-one-issue", cwd=repo_root, env=env)

    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
    assert calls == [
        ["bash", "-c", "git switch main && git pull --ff-only"],
        [
            "opencode",
            "run",
            "--agent",
            "build",
            "--model",
            "pricetag-hosted/Inferact/Qwen3.8-Flash-Next-NVFP4#xhigh",
            "--file",
            ".opencode/commands/grab-issue.md",
            "Execute the task list in the attached file.",
        ],
        ["bash", "-c", "git switch main && git pull --ff-only"],
    ]


@pytest.mark.unit
def test_running_vm_warns_when_provisioning_fingerprint_is_stale(
    devbox_path: Path,
    isolated_env: dict[str, str],
    project_dir: Path,
    tmp_path: Path,
):
    _install_lima_shim(tmp_path, isolated_env, fingerprint="old-stamp")

    result = run_bash_script(
        devbox_path, ["true"], cwd=project_dir, env=isolated_env, timeout=15
    )

    assert result.returncode == 0, result.stderr
    assert "provisioning is stale" in result.stderr
    assert "devbox --reprovision" in result.stderr


@pytest.mark.unit
def test_reprovision_clears_manifest_fingerprint_warning(
    devbox_path: Path,
    repo_root: Path,
    isolated_env: dict[str, str],
    project_dir: Path,
    tmp_path: Path,
):
    calls, _ = _install_lima_shim(tmp_path, isolated_env)
    isolated_env["MOCK_VM_FINGERPRINT_AFTER_START"] = (
        expected_lima_provisioning_fingerprint(repo_root)
    )

    result = run_bash_script(
        devbox_path, ["--reprovision"], cwd=project_dir, env=isolated_env, timeout=15
    )

    assert result.returncode == 0, result.stderr
    assert "provisioning is stale" not in result.stderr
    assert _read_lima_calls(calls)[-1][0] == "shell"


@pytest.mark.unit
def test_launcher_rejects_a_directory_outside_lima_mounts(
    devbox_path: Path,
    isolated_env: dict[str, str],
    tmp_path: Path,
):
    outside = tmp_path / "outside"
    outside.mkdir()
    calls, _ = _install_lima_shim(tmp_path, isolated_env)

    result = run_bash_script(devbox_path, cwd=outside, env=isolated_env, timeout=15)

    assert result.returncode != 0
    assert "outside the paths mounted in Lima" in result.stderr
    assert not calls.exists()


@pytest.mark.unit
@pytest.mark.parametrize("action", ["--reset", "--reprovision"])
def test_lifecycle_command_rejects_unmapped_workdir_before_action(
    devbox_path: Path,
    isolated_env: dict[str, str],
    tmp_path: Path,
    action: str,
):
    outside = tmp_path / "outside"
    outside.mkdir()
    calls, _ = _install_lima_shim(tmp_path, isolated_env)

    result = run_bash_script(
        devbox_path,
        [action, "--", "true"],
        cwd=outside,
        env=isolated_env,
        timeout=15,
    )

    assert result.returncode != 0
    assert "outside the paths mounted in Lima" in result.stderr
    assert not calls.exists()


@pytest.mark.unit
def test_reset_without_command_does_not_validate_workdir_or_launch_a_shell(
    devbox_path: Path,
    isolated_env: dict[str, str],
    tmp_path: Path,
):
    outside = tmp_path / "outside"
    outside.mkdir()
    calls, _ = _install_lima_shim(tmp_path, isolated_env)

    result = run_bash_script(
        devbox_path, ["--reset"], cwd=outside, env=isolated_env, timeout=15
    )

    assert result.returncode == 0, result.stderr
    logged = _read_lima_calls(calls)
    assert tuple(call[0] for call in logged) == (
        "list",
        "factory-reset",
        "list",
        "start",
    )


@pytest.mark.unit
def test_agents_mount_maps_to_its_read_only_guest_mount(
    devbox_path: Path,
    isolated_env: dict[str, str],
    tmp_path: Path,
):
    agents_dir = Path(isolated_env["HOME"]) / ".agents" / "skills"
    agents_dir.mkdir(parents=True)
    calls, _ = _install_lima_shim(tmp_path, isolated_env)

    result = run_bash_script(devbox_path, cwd=agents_dir, env=isolated_env, timeout=15)

    assert result.returncode == 0, result.stderr
    expected_guest_path = f"/home/{getpass.getuser()}.guest/.host-config/agents/skills"
    shell_call = next(call for call in _read_lima_calls(calls) if "--workdir" in call)
    assert expected_guest_path in shell_call


@pytest.mark.unit
@pytest.mark.parametrize(
    ("host_relative", "guest_relative"),
    (
        (
            ".local/state/opencode",
            ".host-config/local/state/opencode-seed",
        ),
        (
            ".local/state/devbox-opencode",
            ".host-config/local/state/devbox-opencode",
        ),
    ),
)
def test_opencode_state_mounts_map_to_their_guest_mounts(
    devbox_path: Path,
    isolated_env: dict[str, str],
    tmp_path: Path,
    host_relative: str,
    guest_relative: str,
):
    host_mount = Path(isolated_env["HOME"]) / host_relative
    workdir = host_mount / "workdir"
    workdir.mkdir(parents=True)
    calls, _ = _install_lima_shim(tmp_path, isolated_env)

    result = run_bash_script(devbox_path, ["true"], cwd=workdir, env=isolated_env)

    assert result.returncode == 0, result.stderr
    shell_call = next(call for call in _read_lima_calls(calls) if "--workdir" in call)
    expected_guest_path = f"/home/{getpass.getuser()}.guest/{guest_relative}/workdir"
    assert expected_guest_path in shell_call


@pytest.mark.unit
@pytest.mark.parametrize(
    ("args", "expected_calls"),
    [
        (["--stop"], ("list", "stop")),
        (["--reprovision"], ("list", "stop", "start", "shell")),
        (["--reset"], ("list", "factory-reset", "list", "start")),
    ],
)
def test_lifecycle_flags_run_expected_lima_operations(
    devbox_path: Path,
    isolated_env: dict[str, str],
    project_dir: Path,
    tmp_path: Path,
    args: list[str],
    expected_calls: tuple[str, ...],
):
    calls, _ = _install_lima_shim(tmp_path, isolated_env)

    result = run_bash_script(
        devbox_path, args, cwd=project_dir, env=isolated_env, timeout=15
    )

    assert result.returncode == 0, result.stderr
    operations = tuple(call[0] for call in _read_lima_calls(calls))
    assert operations == expected_calls
    assert not any("--preserve-env" in call for call in _read_lima_calls(calls))


@pytest.mark.unit
@pytest.mark.parametrize(
    ("action", "expected_operations"),
    [
        ("--reset", ("list", "factory-reset", "list", "start", "shell")),
        ("--reprovision", ("list", "stop", "start", "shell", "shell")),
    ],
)
def test_lifecycle_command_runs_after_action_with_exact_argv_and_filtered_env(
    devbox_path: Path,
    repo_root: Path,
    isolated_env: dict[str, str],
    project_dir: Path,
    tmp_path: Path,
    action: str,
    expected_operations: tuple[str, ...],
):
    calls, capture = _install_lima_shim(tmp_path, isolated_env)
    isolated_env["MOCK_VM_FINGERPRINT_AFTER_START"] = (
        expected_lima_provisioning_fingerprint(repo_root)
    )
    isolated_env["GEMINI_API_KEY"] = "mock-gemini-token"  # pragma: allowlist secret
    unlisted_key = "AWS_" + "SECRET_ACCESS_KEY"
    isolated_env[unlisted_key] = "must-not-forward"  # pragma: allowlist secret
    command = [
        "--workdir",
        "not a helper path",
        "argument with spaces",
        "",
        "--tail",
    ]

    result = run_bash_script(
        devbox_path,
        [action, "--", *command],
        cwd=project_dir,
        env=isolated_env,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    logged = _read_lima_calls(calls)
    assert tuple(call[0] for call in logged) == expected_operations
    command_shell = next(
        call
        for call in reversed(logged)
        if call[0] == "shell" and "--preserve-env" in call
    )
    workdir_index = command_shell.index("--workdir")
    assert command_shell[workdir_index + 1] == str(project_dir)
    assert command_shell[-len(command) :] == command
    payload = json.loads(capture.read_text())
    assert payload["argv"] == command_shell
    assert payload["provider_env"]["GEMINI_API_KEY"] == (
        "mock-gemini-token"  # pragma: allowlist secret
    )
    assert unlisted_key not in payload["allow"]
    assert unlisted_key not in payload["provider_env"]


@pytest.mark.unit
def test_reprovision_opencode_command_uses_runtime_config_wrapper(
    devbox_path: Path,
    repo_root: Path,
    isolated_env: dict[str, str],
    project_dir: Path,
    tmp_path: Path,
):
    calls, capture = _install_lima_shim(tmp_path, isolated_env)
    isolated_env["MOCK_VM_FINGERPRINT_AFTER_START"] = (
        expected_lima_provisioning_fingerprint(repo_root)
    )
    command = ["opencode", "run", "--agent", "build", "--model", "octo-open/test"]

    result = run_bash_script(
        devbox_path,
        ["--reprovision", "--", *command],
        cwd=project_dir,
        env=isolated_env,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    logged = _read_lima_calls(calls)
    shell_call = next(
        call
        for call in reversed(logged)
        if call[0] == "shell" and "--preserve-env" in call
    )
    assert "bash" in shell_call
    assert "-c" in shell_call
    assert f"{repo_root}/lima/opencode_config.py" in shell_call
    assert 'exec opencode "$@"' in " ".join(shell_call)
    assert shell_call[-len(command[1:]) :] == command[1:]
    payload = json.loads(capture.read_text())
    assert payload["block"] == "*"
    assert "OPENCODE_CONFIG_CONTENT" not in payload["allow"]


@pytest.mark.unit
def test_lifecycle_command_returns_its_exit_status(
    devbox_path: Path,
    isolated_env: dict[str, str],
    project_dir: Path,
    tmp_path: Path,
):
    calls, _ = _install_lima_shim(tmp_path, isolated_env)
    isolated_env["MOCK_LIMA_COMMAND_EXIT_STATUS"] = "37"

    result = run_bash_script(
        devbox_path,
        ["--reset", "--", "false"],
        cwd=project_dir,
        env=isolated_env,
        timeout=15,
    )

    assert result.returncode == 37
    assert _read_lima_calls(calls)[-1][0] == "shell"


@pytest.mark.unit
def test_lifecycle_command_is_not_run_when_action_fails(
    devbox_path: Path,
    isolated_env: dict[str, str],
    project_dir: Path,
    tmp_path: Path,
):
    calls, _ = _install_lima_shim(tmp_path, isolated_env)
    isolated_env["MOCK_LIMA_FAIL_COMMAND"] = "factory-reset"
    isolated_env["MOCK_LIMA_FAIL_STATUS"] = "42"

    result = run_bash_script(
        devbox_path,
        ["--reset", "--", "true"],
        cwd=project_dir,
        env=isolated_env,
        timeout=15,
    )

    assert result.returncode == 42
    assert tuple(call[0] for call in _read_lima_calls(calls)) == (
        "list",
        "factory-reset",
    )


@pytest.mark.unit
@pytest.mark.parametrize("delete_flag", ["-d", "--delete"])
@pytest.mark.parametrize("protected", [True, False])
def test_delete_preserves_original_protection_state_and_is_idempotent(
    devbox_path: Path,
    isolated_env: dict[str, str],
    project_dir: Path,
    tmp_path: Path,
    delete_flag: str,
    protected: bool,
):
    instance = "configured-devbox"
    isolated_env["DEVBOX_LIMA_INSTANCE"] = instance
    calls, _ = _install_lima_shim(tmp_path, isolated_env, protected=protected)

    result = run_bash_script(
        devbox_path, [delete_flag], cwd=project_dir, env=isolated_env, timeout=15
    )

    assert result.returncode == 0, result.stderr
    first_calls = _read_lima_calls(calls)
    expected_calls = [
        ["list", "--format", "{{.Name}} {{.Status}}"],
        ["list", "--format", "{{.Name}} {{.Protected}}"],
    ]
    if protected:
        expected_calls.append(["unprotect", "--", instance])
    expected_calls.append(["delete", "--force", "--", instance])
    assert first_calls == expected_calls

    repeat_flag = "--delete" if delete_flag == "-d" else "-d"
    repeated = run_bash_script(
        devbox_path, [repeat_flag], cwd=project_dir, env=isolated_env, timeout=15
    )

    assert repeated.returncode == 0, repeated.stderr
    assert "nothing to delete" in repeated.stdout
    assert _read_lima_calls(calls) == first_calls + [
        ["list", "--format", "{{.Name}} {{.Status}}"]
    ]


@pytest.mark.unit
def test_delete_failure_restores_protection_and_returns_delete_status(
    devbox_path: Path,
    isolated_env: dict[str, str],
    project_dir: Path,
    tmp_path: Path,
):
    instance = "configured-devbox"
    isolated_env["DEVBOX_LIMA_INSTANCE"] = instance
    calls, _ = _install_lima_shim(tmp_path, isolated_env, protected=True)
    isolated_env["MOCK_LIMA_FAIL_COMMAND"] = "delete"
    isolated_env["MOCK_LIMA_FAIL_STATUS"] = "47"

    result = run_bash_script(
        devbox_path, ["--delete"], cwd=project_dir, env=isolated_env, timeout=15
    )

    assert result.returncode == 47
    assert _read_lima_calls(calls) == [
        ["list", "--format", "{{.Name}} {{.Status}}"],
        ["list", "--format", "{{.Name}} {{.Protected}}"],
        ["unprotect", "--", instance],
        ["delete", "--force", "--", instance],
        ["protect", "--", instance],
    ]
    assert "protection is in place" in result.stderr
    assert Path(isolated_env["MOCK_LIMA_PROTECTED_FILE"]).read_text() == "true"


@pytest.mark.unit
def test_delete_failure_does_not_protect_an_originally_unprotected_instance(
    devbox_path: Path,
    isolated_env: dict[str, str],
    project_dir: Path,
    tmp_path: Path,
):
    instance = "configured-devbox"
    isolated_env["DEVBOX_LIMA_INSTANCE"] = instance
    calls, _ = _install_lima_shim(tmp_path, isolated_env, protected=False)
    isolated_env["MOCK_LIMA_FAIL_COMMAND"] = "delete"
    isolated_env["MOCK_LIMA_FAIL_STATUS"] = "47"

    result = run_bash_script(
        devbox_path, ["--delete"], cwd=project_dir, env=isolated_env, timeout=15
    )

    assert result.returncode == 47
    assert _read_lima_calls(calls) == [
        ["list", "--format", "{{.Name}} {{.Status}}"],
        ["list", "--format", "{{.Name}} {{.Protected}}"],
        ["delete", "--force", "--", instance],
    ]
    assert "could not delete" in result.stderr
    assert "protection is in place" not in result.stderr
    assert Path(isolated_env["MOCK_LIMA_PROTECTED_FILE"]).read_text() == "false"


@pytest.mark.unit
def test_delete_fails_closed_when_protection_state_cannot_be_verified(
    devbox_path: Path,
    isolated_env: dict[str, str],
    project_dir: Path,
    tmp_path: Path,
):
    calls, _ = _install_lima_shim(tmp_path, isolated_env)
    isolated_env["MOCK_LIMA_FAIL_FORMAT"] = "{{.Name}} {{.Protected}}"
    isolated_env["MOCK_LIMA_FAIL_FORMAT_STATUS"] = "49"

    result = run_bash_script(
        devbox_path, ["--delete"], cwd=project_dir, env=isolated_env, timeout=15
    )

    assert result.returncode != 0
    assert _read_lima_calls(calls) == [
        ["list", "--format", "{{.Name}} {{.Status}}"],
        ["list", "--format", "{{.Name}} {{.Protected}}"],
    ]
    assert "could not verify protection state" in result.stderr


@pytest.mark.unit
def test_delete_failure_warns_with_recovery_command_if_protection_cannot_be_restored(
    devbox_path: Path,
    isolated_env: dict[str, str],
    project_dir: Path,
    tmp_path: Path,
):
    instance = "configured-devbox"
    isolated_env["DEVBOX_LIMA_INSTANCE"] = instance
    calls, _ = _install_lima_shim(tmp_path, isolated_env, protected=True)
    isolated_env["MOCK_LIMA_FAIL_COMMAND"] = "delete"
    isolated_env["MOCK_LIMA_FAIL_STATUS"] = "47"
    isolated_env["MOCK_LIMA_FAIL_COMMAND_2"] = "protect"
    isolated_env["MOCK_LIMA_FAIL_STATUS_2"] = "53"

    result = run_bash_script(
        devbox_path, ["--delete"], cwd=project_dir, env=isolated_env, timeout=15
    )

    assert result.returncode == 47
    assert _read_lima_calls(calls) == [
        ["list", "--format", "{{.Name}} {{.Status}}"],
        ["list", "--format", "{{.Name}} {{.Protected}}"],
        ["unprotect", "--", instance],
        ["delete", "--force", "--", instance],
        ["protect", "--", instance],
    ]
    assert "WARNING" in result.stderr
    assert "limactl protect configured-devbox" in result.stderr
    assert "If the instance still exists" in result.stderr
    assert Path(isolated_env["MOCK_LIMA_PROTECTED_FILE"]).read_text() == "false"


@pytest.mark.unit
def test_delete_stops_after_unprotect_failure(
    devbox_path: Path,
    isolated_env: dict[str, str],
    project_dir: Path,
    tmp_path: Path,
):
    instance = "configured-devbox"
    isolated_env["DEVBOX_LIMA_INSTANCE"] = instance
    calls, _ = _install_lima_shim(tmp_path, isolated_env, protected=True)
    isolated_env["MOCK_LIMA_FAIL_COMMAND"] = "unprotect"
    isolated_env["MOCK_LIMA_FAIL_STATUS"] = "43"

    result = run_bash_script(
        devbox_path, ["--delete"], cwd=project_dir, env=isolated_env, timeout=15
    )

    assert result.returncode == 43
    assert _read_lima_calls(calls) == [
        ["list", "--format", "{{.Name}} {{.Status}}"],
        ["list", "--format", "{{.Name}} {{.Protected}}"],
        ["unprotect", "--", instance],
        ["protect", "--", instance],
    ]
    assert "unprotect failed" in result.stderr
    assert Path(isolated_env["MOCK_LIMA_PROTECTED_FILE"]).read_text() == "true"


@pytest.mark.unit
@pytest.mark.unit_serial
@pytest.mark.parametrize(
    "sig",
    [signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT, signal.SIGPIPE],
    ids=["SIGINT", "SIGTERM", "SIGHUP", "SIGQUIT", "SIGPIPE"],
)
def test_signal_during_delete_restores_protection_and_returns_signal_status(
    devbox_path: Path,
    isolated_env: dict[str, str],
    project_dir: Path,
    tmp_path: Path,
    shared_process_signal_test_lock: None,
    sig: signal.Signals,
):
    instance = "configured-devbox"
    isolated_env["DEVBOX_LIMA_INSTANCE"] = instance
    calls, _ = _install_lima_shim(tmp_path, isolated_env, protected=True)
    isolated_env["MOCK_LIMA_DELETE_MODE"] = "before"
    isolated_env["MOCK_LIMA_DELETE_STARTED_FILE"] = str(tmp_path / "delete-started")
    isolated_env["MOCK_LIMA_DELETE_CONTINUE_FILE"] = str(tmp_path / "delete-continue")

    result = _interrupt_blocked_delete(devbox_path, project_dir, isolated_env, sig)

    assert result.returncode == 128 + sig
    assert _read_lima_calls(calls) == [
        ["list", "--format", "{{.Name}} {{.Status}}"],
        ["list", "--format", "{{.Name}} {{.Protected}}"],
        ["unprotect", "--", instance],
        ["delete", "--force", "--", instance],
        ["list", "--format", "{{.Name}}"],
        ["protect", "--", instance],
    ]
    assert "protection is in place" in result.stderr
    assert Path(isolated_env["MOCK_LIMA_PROTECTED_FILE"]).read_text() == "true"


@pytest.mark.unit
@pytest.mark.unit_serial
def test_signal_after_instance_is_deleted_does_not_attempt_protection_rollback(
    devbox_path: Path,
    isolated_env: dict[str, str],
    project_dir: Path,
    tmp_path: Path,
    shared_process_signal_test_lock: None,
):
    instance = "configured-devbox"
    isolated_env["DEVBOX_LIMA_INSTANCE"] = instance
    calls, _ = _install_lima_shim(tmp_path, isolated_env, protected=True)
    isolated_env["MOCK_LIMA_DELETE_MODE"] = "after"
    isolated_env["MOCK_LIMA_DELETE_STARTED_FILE"] = str(tmp_path / "delete-started")
    isolated_env["MOCK_LIMA_DELETE_CONTINUE_FILE"] = str(tmp_path / "delete-continue")

    result = _interrupt_blocked_delete(
        devbox_path, project_dir, isolated_env, signal.SIGTERM
    )

    assert result.returncode == 128 + signal.SIGTERM
    assert _read_lima_calls(calls) == [
        ["list", "--format", "{{.Name}} {{.Status}}"],
        ["list", "--format", "{{.Name}} {{.Protected}}"],
        ["unprotect", "--", instance],
        ["delete", "--force", "--", instance],
        ["list", "--format", "{{.Name}}"],
    ]
    assert "WARNING" not in result.stderr
    assert not any(call[0] == "protect" for call in _read_lima_calls(calls))


@pytest.mark.unit
@pytest.mark.unit_serial
def test_signal_delete_rollback_failure_warns_but_returns_signal_status(
    devbox_path: Path,
    isolated_env: dict[str, str],
    project_dir: Path,
    tmp_path: Path,
    shared_process_signal_test_lock: None,
):
    instance = "configured-devbox"
    isolated_env["DEVBOX_LIMA_INSTANCE"] = instance
    calls, _ = _install_lima_shim(tmp_path, isolated_env, protected=True)
    isolated_env["MOCK_LIMA_DELETE_MODE"] = "before"
    isolated_env["MOCK_LIMA_DELETE_STARTED_FILE"] = str(tmp_path / "delete-started")
    isolated_env["MOCK_LIMA_DELETE_CONTINUE_FILE"] = str(tmp_path / "delete-continue")
    isolated_env["MOCK_LIMA_FAIL_COMMAND"] = "protect"
    isolated_env["MOCK_LIMA_FAIL_STATUS"] = "53"

    result = _interrupt_blocked_delete(
        devbox_path, project_dir, isolated_env, signal.SIGTERM
    )

    assert result.returncode == 128 + signal.SIGTERM
    assert _read_lima_calls(calls) == [
        ["list", "--format", "{{.Name}} {{.Status}}"],
        ["list", "--format", "{{.Name}} {{.Protected}}"],
        ["unprotect", "--", instance],
        ["delete", "--force", "--", instance],
        ["list", "--format", "{{.Name}}"],
        ["protect", "--", instance],
    ]
    assert "WARNING" in result.stderr
    assert "limactl protect configured-devbox" in result.stderr
    assert Path(isolated_env["MOCK_LIMA_PROTECTED_FILE"]).read_text() == "false"


@pytest.mark.unit
@pytest.mark.parametrize("args", [["--delete", "true"], ["-d", "--", "--option"]])
def test_delete_rejects_extra_command_args_before_inspecting_instance(
    devbox_path: Path,
    isolated_env: dict[str, str],
    project_dir: Path,
    tmp_path: Path,
    args: list[str],
):
    calls, _ = _install_lima_shim(tmp_path, isolated_env)

    result = run_bash_script(
        devbox_path, args, cwd=project_dir, env=isolated_env, timeout=15
    )

    assert result.returncode == 2
    assert "--delete" in result.stderr
    assert "does not accept a command" in result.stderr
    assert not calls.exists()


@pytest.mark.unit
def test_concurrent_reprovision_operations_do_not_interleave(
    devbox_path: Path,
    isolated_env: dict[str, str],
    project_dir: Path,
    tmp_path: Path,
):
    calls, _ = _install_lima_shim(tmp_path, isolated_env)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _: run_bash_script(
                    devbox_path,
                    ["--reprovision"],
                    cwd=project_dir,
                    env=isolated_env.copy(),
                    timeout=15,
                ),
                range(2),
            )
        )

    assert all(result.returncode == 0 for result in results), [
        result.stderr for result in results
    ]
    operations = tuple(call[0] for call in _read_lima_calls(calls))
    assert operations == (
        "list",
        "stop",
        "start",
        "shell",
        "list",
        "stop",
        "start",
        "shell",
    )
