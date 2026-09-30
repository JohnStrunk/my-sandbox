import getpass
import json
import stat
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
) -> tuple[Path, Path]:
    bin_dir = tmp_path / "lima-bin"
    bin_dir.mkdir()
    calls = tmp_path / "limactl-calls.log"
    status_file = tmp_path / "lima-status"
    status_file.write_text(status)
    fingerprint_file = tmp_path / "vm-fingerprint"
    fingerprint_file.write_text(fingerprint)
    shell_capture = tmp_path / "shell-capture.json"

    limactl = bin_dir / "limactl"
    limactl.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >>"$MOCK_LIMACTL_CALLS"
case "${1:-}" in
  list)
    status="$(cat "$MOCK_LIMA_STATUS_FILE")"
    if [[ -n "$status" ]]; then
      printf '%s %s\n' "$MOCK_LIMA_INSTANCE" "$status"
    fi
    ;;
  start)
    printf 'Running' >"$MOCK_LIMA_STATUS_FILE"
    if [[ -n "${MOCK_VM_FINGERPRINT_AFTER_START:-}" ]]; then
      printf '%s' "$MOCK_VM_FINGERPRINT_AFTER_START" >"$MOCK_VM_FINGERPRINT_FILE"
    fi
    ;;
  stop|factory-reset)
    printf 'Stopped' >"$MOCK_LIMA_STATUS_FILE"
    ;;
  shell)
    if [[ "$*" == *provisioning.fingerprint* ]]; then
      cat "$MOCK_VM_FINGERPRINT_FILE"
    else
      python3 - "$MOCK_SHELL_CAPTURE" "$*" <<'PY'
import json
import os
import sys
names = os.environ.get("LIMA_SHELLENV_ALLOW", "").split(",")
payload = {
    "args": sys.argv[2],
    "allow": names,
    "block": os.environ.get("LIMA_SHELLENV_BLOCK"),
    "provider_env": {name: os.environ[name] for name in names if name in os.environ},
}
with open(sys.argv[1], "w") as output:
    json.dump(payload, output)
PY
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
    env["MOCK_LIMA_INSTANCE"] = env.get("DEVBOX_LIMA_INSTANCE", "devbox")
    env["MOCK_VM_FINGERPRINT_FILE"] = str(fingerprint_file)
    env["MOCK_SHELL_CAPTURE"] = str(shell_capture)
    return calls, shell_capture


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
    logged = calls.read_text().splitlines()
    assert "start devbox" in logged
    shell_call = next(call for call in logged if "--workdir" in call)
    assert f"--workdir {project_dir} devbox" in shell_call
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
    logged = calls.read_text().splitlines()
    create_call = next(call for call in logged if call.startswith("start --yes --name"))
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
    logged = calls.read_text().splitlines()
    assert not any(call.startswith("start ") for call in logged)
    shell_call = next(call for call in logged if "--workdir" in call)
    assert "opencode" in shell_call
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
        for call in calls.read_text().splitlines()
        if call.startswith("shell ") and "--workdir" in call
    )
    assert "bash -c" in shell_call
    assert f"{repo_root}/lima/opencode_config.py" in shell_call
    assert "OPENCODE_CONFIG_CONTENT" in shell_call
    assert 'exec opencode "$@"' in shell_call
    assert "run --agent build --model octo-open/test" in shell_call
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
    assert calls.read_text().splitlines()[-1].startswith("shell ")


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
    shell_call = next(
        call for call in calls.read_text().splitlines() if "--workdir" in call
    )
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
    operations = tuple(call.split()[0] for call in calls.read_text().splitlines())
    assert operations == expected_calls


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
    operations = tuple(call.split()[0] for call in calls.read_text().splitlines())
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
