"""Verify the command-line sanitized test wrapper (issue #150)."""

import json
import os
import shlex
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.conftest import _wait_pid_gone, run_in_process_group


def _run_wrapper(
    repo_root: Path, args: list[str], env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return run_in_process_group(
        [str(repo_root / "scripts" / "sanitized-test.sh"), *args],
        timeout=30,
        env=env,
        cwd=repo_root,
    )


def _make_fake_podman(
    bin_dir: Path,
    log_path: Path,
    *,
    status: int = 0,
    message: str = "",
    run_status: int = 0,
    run_message: str = "",
    rm_status: int = 0,
    exists_status: int = 0,
) -> None:
    log = shlex.quote(str(log_path))
    fake_podman = bin_dir / "podman"
    fake_podman.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
printf 'HOME=%s\\n' "${{HOME-}}" > {log}
printf 'XDG_CONFIG_HOME=%s\\n' "${{XDG_CONFIG_HOME-}}" >> {log}
printf 'XDG_DATA_HOME=%s\\n' "${{XDG_DATA_HOME-}}" >> {log}
printf 'XDG_RUNTIME_DIR=%s\\n' "${{XDG_RUNTIME_DIR-}}" >> {log}
printf 'REGISTRY_AUTH_FILE=%s\\n' "${{REGISTRY_AUTH_FILE-}}" >> {log}
printf 'GH_TOKEN=%s\\n' "${{GH_TOKEN-<unset>}}" >> {log}
printf 'CONTAINERS_CONF=%s\\n' "${{CONTAINERS_CONF-<unset>}}" >> {log}
printf 'DOCKER_CONFIG=%s\\n' "${{DOCKER_CONFIG-<unset>}}" >> {log}
printf 'DOCKER_AUTH_CONFIG=%s\\n' "${{DOCKER_AUTH_CONFIG-<unset>}}" >> {log}
printf 'ARGS=%s\\n' "$*" >> {log}
if [[ -f "${{XDG_CONFIG_HOME-}}/containers/containers.conf" ]]; then
  printf 'CONFIG_COPY=present\\n' >> {log}
else
  printf 'CONFIG_COPY=absent\\n' >> {log}
fi
if [[ -f "${{XDG_CONFIG_HOME-}}/containers/auth.json" ]]; then
  printf 'AUTH_COPY=present\\n' >> {log}
else
  printf 'AUTH_COPY=absent\\n' >> {log}
fi
while [[ "${{1-}}" == --root || "${{1-}}" == --runroot ]]; do
  shift 2
done
if [[ "${{1-}}" == run && {run_status} -ne 0 ]]; then
  printf '%s\\n' {shlex.quote(run_message)} >&2
  exit {run_status}
fi
if [[ "${{1-}}" == rm && {rm_status} -ne 0 ]]; then
  printf 'simulated probe removal failure\\n' >&2
  exit {rm_status}
fi
if [[ "${{1-}}" == container && "${{2-}}" == exists && {exists_status} -ne 0 ]]; then
  printf 'simulated container status failure\\n' >&2
  exit {exists_status}
fi
if [[ "${{1-}}" == info ]]; then
  if [[ {status} -ne 0 ]]; then
    printf '%s\\n' {shlex.quote(message)} >&2
    exit {status}
  fi
  printf 'true\\n'
fi
"""
    )
    fake_podman.chmod(0o700)


def _podman_environment(
    tmp_path: Path, fake_bin: Path, host_home: Path
) -> dict[str, str]:
    host_config = tmp_path / "host-config"
    host_data = tmp_path / "host-data"
    host_runtime = tmp_path / "host-runtime"
    host_cache = tmp_path / "host-cache"
    host_config.mkdir()
    host_data.mkdir()
    host_runtime.mkdir()
    host_cache.mkdir()
    host_containers = host_config / "containers"
    host_containers.mkdir()
    (host_containers / "containers.conf").write_text(
        "[containers]\ndefault_sysctls = []\n"
    )
    (host_containers / "auth.json").write_text('{"auth":"fixture-auth-sentinel"}\n')
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env.get('PATH', '')}",
            "HOME": str(host_home),
            "XDG_CONFIG_HOME": str(host_config),
            "XDG_DATA_HOME": str(host_data),
            "XDG_RUNTIME_DIR": str(host_runtime),
            "XDG_CACHE_HOME": str(host_cache),
            "GH_TOKEN": "host-secret-token",  # pragma: allowlist secret
            "TAVILY_API_KEY": "host-tavily-token",  # pragma: allowlist secret
            "CONTAINERS_CONF": str(tmp_path / "host-secret.conf"),
            "DOCKER_AUTH_CONFIG": '{"auths":{"registry.example":"secret"}}',
        }
    )
    return env


@pytest.mark.unit
def test_wrapper_scrubs_host_environment(repo_root: Path, tmp_path: Path) -> None:
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path / "host-home"),
            "GH_TOKEN": "host-secret-token",  # pragma: allowlist secret
            "AWS_CONFIG_FILE": str(tmp_path / "credentials"),
            "UNSAFE_TEST_VARIABLE": "must-not-cross-boundary",
        }
    )
    result = _run_wrapper(
        repo_root,
        [
            "--",
            sys.executable,
            "-c",
            "import json, os; print(json.dumps(dict(os.environ)))",
        ],
        env,
    )

    assert result.returncode == 0, result.stderr
    child_env = json.loads(result.stdout)
    assert child_env["HOME"] != env["HOME"]
    assert child_env["XDG_CONFIG_HOME"] != env.get("XDG_CONFIG_HOME")
    assert "GH_TOKEN" not in child_env
    assert "TAVILY_API_KEY" not in child_env
    assert "AWS_CONFIG_FILE" not in child_env
    assert "UNSAFE_TEST_VARIABLE" not in child_env


@pytest.mark.unit
def test_wrapper_restores_only_podman_runtime_allowlist(
    repo_root: Path, tmp_path: Path
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    log_path = host_home / "podman.log"
    _make_fake_podman(fake_bin, log_path)
    env = _podman_environment(tmp_path, fake_bin, host_home)

    result = _run_wrapper(repo_root, ["--require-podman", "--", "podman", "info"], env)

    assert result.returncode == 0, result.stderr
    logged = log_path.read_text()
    assert f"HOME={host_home}" not in logged
    assert f"XDG_CONFIG_HOME={tmp_path / 'host-config'}" not in logged
    assert f"XDG_DATA_HOME={tmp_path / 'host-data'}" not in logged
    assert f"XDG_RUNTIME_DIR={tmp_path / 'host-runtime'}" not in logged
    assert "GH_TOKEN=<unset>" in logged
    assert "CONTAINERS_CONF=<unset>" in logged
    assert "DOCKER_CONFIG=" in logged
    assert "DOCKER_AUTH_CONFIG=<unset>" in logged
    assert "CONFIG_COPY=present" in logged
    assert "AUTH_COPY=absent" in logged
    assert f"DOCKER_CONFIG={host_home}" not in logged
    assert "REGISTRY_AUTH_FILE=" in logged
    assert str(host_home) not in logged.split("REGISTRY_AUTH_FILE=", 1)[1]
    assert f"--root {tmp_path / 'host-data' / 'containers' / 'storage'}" in logged
    assert f"--runroot {tmp_path / 'host-runtime' / 'containers'}" in logged


@pytest.mark.unit
def test_wrapper_distinguishes_podman_preflight_failure(
    repo_root: Path, tmp_path: Path
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    log_path = host_home / "podman.log"
    _make_fake_podman(
        fake_bin,
        log_path,
        status=42,
        message="ping_group_range runtime configuration unavailable",
    )
    env = _podman_environment(tmp_path, fake_bin, host_home)
    marker = tmp_path / "product-command-ran"
    command = [sys.executable, "-c", f"Path({str(marker)!r}).touch()"]

    result = _run_wrapper(repo_root, ["--require-podman", "--", *command], env)

    assert result.returncode == 125
    assert "Podman preflight failed" in result.stderr
    assert "infrastructure/runtime configuration failure" in result.stderr
    assert "ping_group_range runtime configuration unavailable" in result.stderr
    assert not marker.exists()


@pytest.mark.unit
def test_wrapper_distinguishes_container_preflight_failure(
    repo_root: Path, tmp_path: Path
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    log_path = host_home / "podman.log"
    _make_fake_podman(
        fake_bin,
        log_path,
        run_status=42,
        run_message="probe registry configuration unavailable",
    )
    env = _podman_environment(tmp_path, fake_bin, host_home)
    marker = tmp_path / "product-command-ran"
    command = [sys.executable, "-c", f"Path({str(marker)!r}).touch()"]

    result = _run_wrapper(repo_root, ["--require-podman", "--", *command], env)

    assert result.returncode == 125
    assert "Podman container preflight failed" in result.stderr
    assert "probe registry configuration unavailable" in result.stderr
    assert not marker.exists()
    assert "rm -f my-sandbox-podman-probe-" in log_path.read_text()


@pytest.mark.unit
def test_wrapper_reports_unknown_probe_cleanup_status(
    repo_root: Path, tmp_path: Path
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    log_path = host_home / "podman.log"
    _make_fake_podman(
        fake_bin,
        log_path,
        run_status=42,
        run_message="probe runtime unavailable",
        rm_status=17,
        exists_status=125,
    )
    env = _podman_environment(tmp_path, fake_bin, host_home)
    marker = tmp_path / "product-command-ran"

    result = _run_wrapper(
        repo_root,
        [
            "--require-podman",
            "--",
            sys.executable,
            "-c",
            f"Path({str(marker)!r}).touch()",
        ],
        env,
    )

    assert result.returncode == 125
    assert "could not confirm removal of Podman probe container" in result.stderr
    assert "container exists check exited 125" in result.stderr
    assert not marker.exists()


@pytest.mark.unit
def test_wrapper_reports_probe_container_left_after_cleanup_failure(
    repo_root: Path, tmp_path: Path
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    log_path = host_home / "podman.log"
    _make_fake_podman(
        fake_bin,
        log_path,
        run_status=42,
        run_message="probe runtime unavailable",
        rm_status=17,
        exists_status=0,
    )
    env = _podman_environment(tmp_path, fake_bin, host_home)
    marker = tmp_path / "product-command-ran"

    result = _run_wrapper(
        repo_root,
        [
            "--require-podman",
            "--",
            sys.executable,
            "-c",
            f"Path({str(marker)!r}).touch()",
        ],
        env,
    )

    assert result.returncode == 125
    assert "WARNING: could not remove Podman probe container" in result.stderr
    assert not marker.exists()


@pytest.mark.unit
def test_wrapper_serializes_podman_runtime_sessions(
    repo_root: Path, tmp_path: Path
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    log_path = host_home / "podman.log"
    _make_fake_podman(fake_bin, log_path)
    env = _podman_environment(tmp_path, fake_bin, host_home)

    active = tmp_path / "test-session-active"
    overlap = tmp_path / "test-session-overlap"
    command = [
        sys.executable,
        "-c",
        """
import os
import pathlib
import sys
import time

active, overlap = map(pathlib.Path, sys.argv[1:])
lock_file = pathlib.Path(os.environ["MY_SANDBOX_PODMAN_RUNTIME_LOCK_FILE"])
if os.environ.get("MY_SANDBOX_PODMAN_RUNTIME_LOCK_HELD") != "1":
    raise SystemExit("runtime lock marker was not passed to the test command")
if not lock_file.is_file():
    raise SystemExit("runtime lock file was not created")
try:
    active.mkdir()
except FileExistsError:
    overlap.touch()
time.sleep(0.2)
try:
    active.rmdir()
except OSError:
    pass
""",
        str(active),
        str(overlap),
    ]

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                _run_wrapper,
                repo_root,
                ["--require-podman", "--", *command],
                env,
            )
            for _ in range(2)
        ]
        results = [future.result(timeout=45) for future in futures]

    assert all(result.returncode == 0 for result in results), [
        result.stderr for result in results
    ]
    assert not overlap.exists()


@pytest.mark.unit
def test_wrapper_propagates_command_status(repo_root: Path) -> None:
    result = _run_wrapper(
        repo_root,
        ["--", sys.executable, "-c", "raise SystemExit(23)"],
        os.environ.copy(),
    )

    assert result.returncode == 23
    # The background-job launch must not add job-control noise on the
    # success path.
    assert result.stderr == ""


@pytest.mark.unit
def test_wrapper_passes_suite_tuning_knobs(repo_root: Path) -> None:
    """The documented suite runs through the wrapper's env allowlist, so the
    build-timeout knobs must pass through it (issue #252): a knob the wrapper
    scrubs is a no-op in exactly the environments that need it.
    """
    env = os.environ.copy()
    env["DEVBOX_IMAGE_BUILD_TIMEOUT"] = "123.5"
    env["DEVBOX_PODMAN_PROBE_TIMEOUT"] = "45.5"

    result = _run_wrapper(
        repo_root,
        [
            "--",
            sys.executable,
            "-c",
            "import json, os; print(json.dumps(dict(os.environ)))",
        ],
        env,
    )

    assert result.returncode == 0, result.stderr
    child_env = json.loads(result.stdout)
    assert child_env["DEVBOX_IMAGE_BUILD_TIMEOUT"] == "123.5"
    assert child_env["DEVBOX_PODMAN_PROBE_TIMEOUT"] == "45.5"


@pytest.mark.unit
def test_wrapper_sigint_exits_fast_with_command_cleanup(
    repo_root: Path, tmp_path: Path
) -> None:
    """A graceful command must shut down on the forwarded SIGINT without the
    wrapper waiting out its whole escalation grace.
    """
    cleanup_marker = tmp_path / "int-cleanup-ran"
    ready_marker = tmp_path / "int-command-ready"
    # Foreground sleep: an *async* sleep would ignore SIGINT (POSIX: async
    # commands in non-interactive shells inherit SIG_IGN for it) and turn
    # this into the escalation path instead of the graceful one.
    command = (
        "trap 'touch " + shlex.quote(str(cleanup_marker)) + "' INT\n"
        "touch " + shlex.quote(str(ready_marker)) + "\n"
        "sleep 30\n"
    )
    with (tmp_path / "out").open("w") as out, (tmp_path / "err").open("w") as err:
        proc = subprocess.Popen(
            [
                str(repo_root / "scripts" / "sanitized-test.sh"),
                "--",
                "bash",
                "-c",
                command,
            ],
            stdout=out,
            stderr=err,
            start_new_session=True,
            cwd=repo_root,
            env=os.environ.copy(),
        )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not ready_marker.exists():
            assert proc.poll() is None, "command exited before the signal was sent"
            time.sleep(0.05)
        assert ready_marker.exists(), "command never became ready"

        start = time.monotonic()
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=30)
        elapsed = time.monotonic() - start
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    assert proc.returncode == 130, (tmp_path / "err").read_text()  # 128 + SIGINT
    assert cleanup_marker.exists(), "command INT trap never ran"
    assert elapsed < 5, f"graceful INT took {elapsed:.1f}s to shut down"


@pytest.mark.unit
def test_wrapper_sigterm_terminates_command_tree(
    repo_root: Path, tmp_path: Path
) -> None:
    """Interrupting the wrapper must terminate the whole command tree (issue #252).

    The command models a wedged nested `podman build`: both the command and
    its child ignore SIGTERM, so only the wrapper's escalated group SIGKILL
    can stop them. Before the fix, killing the wrapper orphaned exactly this
    kind of tree, which kept burning CPU after the run was over.
    """
    pidfile = tmp_path / "command-pids"
    # SIG_IGN survives fork and exec, so the ignore is set explicitly in the
    # child as well as the parent: both processes genuinely ignore SIGTERM
    # and only the wrapper's escalated group SIGKILL can stop them.
    command = (
        "trap '' TERM INT\n"
        "(trap '' TERM INT; exec sleep 600) &\n"
        f"printf '%s\\n%s\\n' $$ $! > {shlex.quote(str(pidfile))}\n"
        "wait\n"
    )
    stdout_path = tmp_path / "wrapper.stdout"
    stderr_path = tmp_path / "wrapper.stderr"
    with stdout_path.open("w") as out, stderr_path.open("w") as err:
        proc = subprocess.Popen(
            [
                str(repo_root / "scripts" / "sanitized-test.sh"),
                "--",
                "bash",
                "-c",
                command,
            ],
            stdout=out,
            stderr=err,
            start_new_session=True,
            cwd=repo_root,
            env=os.environ.copy(),
        )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if pidfile.exists():
                break
            assert proc.poll() is None, "wrapper exited before the command started"
            time.sleep(0.05)
        assert pidfile.exists(), "command never started before the signal"
        command_pid, child_pid = (int(v) for v in pidfile.read_text().split())

        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    stderr = stderr_path.read_text()

    assert proc.returncode == 143, stderr  # 128 + SIGTERM
    assert "received SIGTERM" in stderr
    assert "terminating the command process group" in stderr
    assert _wait_pid_gone(command_pid), (
        f"command pid {command_pid} survived the wrapper interruption"
    )
    assert _wait_pid_gone(child_pid), (
        f"SIGTERM-ignoring child {child_pid} survived the wrapper interruption"
    )


@pytest.mark.unit
def test_wrapper_sigterm_terminates_detached_session_builds(
    repo_root: Path, tmp_path: Path
) -> None:
    """Interrupting the wrapper must not orphan detached-session builds (issue #252).

    This is the full chain from the issue: the suite launches builds via
    `run_in_process_group`, which gives each one its own session, so a
    wedged build survives any group-wide signal aimed at the suite itself.
    The suite's SIGTERM handler must kill its tracked process groups before
    the wrapper's group kill lands, or an interrupted run leaves a
    CPU-spinning `podman build` behind.
    """
    build_pidfile = tmp_path / "detached-build.pid"
    # A stand-in for the pytest process running under the wrapper: it
    # installs the suite's termination handlers and starts a build through
    # the suite's runner (own session, ignores SIGTERM).
    suite_mock = tmp_path / "suite-mock.py"
    suite_mock.write_text(
        "import sys\n"
        "import threading\n"
        "import time\n"
        f"sys.path.insert(0, {str(repo_root)!r})\n"
        "from tests.conftest import (\n"
        "    install_termination_handlers,\n"
        "    run_in_process_group,\n"
        ")\n"
        "\n"
        "install_termination_handlers()\n"
        "\n"
        "\n"
        "def start_wedge() -> None:\n"
        "    run_in_process_group(\n"
        "        [\n"
        "            'bash',\n"
        "            '-c',\n"
        "            \"trap '' TERM; printf '%s\\\\n' $$ > "
        f'{str(build_pidfile)!r}; sleep 600 & wait",\n'
        "        ],\n"
        "        timeout=600,\n"
        "    )\n"
        "\n"
        "\n"
        "worker = threading.Thread(target=start_wedge, daemon=True)\n"
        "worker.start()\n"
        "while not __import__('os').path.exists("
        f"{str(build_pidfile)!r}):\n"
        "    time.sleep(0.05)\n"
        "print('READY', flush=True)\n"
        "time.sleep(600)\n"
    )
    with (tmp_path / "out").open("w") as out, (tmp_path / "err").open("w") as err:
        proc = subprocess.Popen(
            [
                str(repo_root / "scripts" / "sanitized-test.sh"),
                "--",
                sys.executable,
                str(suite_mock),
            ],
            stdout=out,
            stderr=err,
            start_new_session=True,
            cwd=repo_root,
            env=os.environ.copy(),
        )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not build_pidfile.exists():
            assert proc.poll() is None, "wrapper exited before the build started"
            time.sleep(0.05)
        assert build_pidfile.exists(), "detached build never started"
        build_pid = int(build_pidfile.read_text().strip())

        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    # The wedged build ignored SIGTERM by construction: only the suite's
    # tracked-group handler (SIGKILL) or the wrapper's escalation can have
    # killed it. Either way, it must be dead -- no orphan left spinning.
    stderr = (tmp_path / "err").read_text()
    assert proc.returncode == 143, stderr  # 128 + SIGTERM
    assert "received SIGTERM" in stderr
    assert "terminating the command process group" in stderr
    assert _wait_pid_gone(build_pid), (
        f"detached-session build {build_pid} survived the wrapper interruption"
    )
