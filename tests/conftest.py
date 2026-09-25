import hashlib
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Credential-isolated test runner (issue #81)
# ---------------------------------------------------------------------------
# Provider and integration credentials must never influence the outcome of an
# isolated test, and mock command logs/diagnostics must never contain host
# credential values. `CREDENTIAL_ENV_VARS` is the maintained scrub list of
# provider/integration environment variables that `devbox` and its supporting
# scripts read. The `_isolated_test_environment` autouse fixture below
# removes these from `os.environ` for every test by default, so unit,
# container, and integration tests are deterministic whether or not the host
# happens to have any of these set.
#
# Tests that need to exercise credential passthrough behavior opt in
# explicitly (e.g. via `monkeypatch.setenv(...)`, or the `host_credentials`
# and `isolated_env` fixtures below).
#
# `tests/e2e_inference` is the intentional exception: those tests are
# end-to-end checks that require real provider credentials, so they are
# exempt from the automatic scrub (see the `e2e_inference` marker check in
# `_isolated_test_environment`).
CREDENTIAL_ENV_VARS = (
    "GEMINI_API_KEY",
    "GOOGLE_GENERATIVE_AI_API_KEY",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "CONTEXT7_API_KEY",
    "TAVILY_API_KEY",
    "IGLOO_MCP_COMMUNITY",
    "IGLOO_MCP_COMMUNITY_KEY",
    "IGLOO_MCP_APP_PASS",
    "IGLOO_MCP_APP_ID",
    "IGLOO_MCP_USERNAME",
    "IGLOO_MCP_PASSWORD",
    "GITLAB_HOST",
    "GITLAB_TOKEN",
    "LITEMAAS_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "OCTO_OPEN_URL",
    "OCTO_OPEN_KEY",
    "PRICETAG_ANTHROPIC_URL",
    "PRICETAG_HOSTED_URL",
    "PRICETAG_OPENAI_URL",
    "PRICETAG_API_KEY",
    "OPENROUTER_API_KEY",
    "GOOGLE_CLOUD_PROJECT",
    "VERTEX_LOCATION",
)

# Environment overrides that can make a CLI read configuration or credentials
# from a host path even when `$HOME` is isolated. Tests may set these
# explicitly when that behavior is what they are verifying.
HOST_CONFIG_ENV_VARS = (
    "AWS_CONFIG_FILE",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AZURE_CONFIG_DIR",
    "CLOUDSDK_CONFIG",
    "CONTAINERS_CONF",
    "CONTAINERS_REGISTRIES_CONF",
    "CONTAINERS_STORAGE_CONF",
    "DOCKER_CONFIG",
    "GH_CONFIG_DIR",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_SSH_COMMAND",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GLAB_CONFIG_DIR",
    "KUBECONFIG",
    "NETRC",
    "NPM_CONFIG_USERCONFIG",
    "OPENCODE_CONFIG",
    "OPENCODE_CONFIG_DIR",
    "PIP_CONFIG_FILE",
    "SSH_AUTH_SOCK",
)

ISOLATION_ENV_VARS = CREDENTIAL_ENV_VARS + HOST_CONFIG_ENV_VARS
PODMAN_RUNTIME_CONFIG_FILES = (
    "containers.conf",
    "storage.conf",
    "registries.conf",
    "policy.json",
)
SANITIZED_TEST_WRAPPER_ACTIVE = "MY_SANDBOX_SANITIZED_TEST_WRAPPER_ACTIVE"
SAFE_TEST_ENV_VARS = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "CI",
    "MY_SANDBOX_PODMAN_RUNTIME_LOCK_FILE",
    "MY_SANDBOX_PODMAN_RUNTIME_LOCK_HELD",
)
UNLISTED_SENSITIVE_ENV_VARS = (
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "PYTHONPATH",
    "BASH_ENV",
)


@pytest.fixture(autouse=True)
def _isolated_test_environment(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scrub provider/integration credentials from every test by default.

    This is the standard, reusable isolation applied to the whole suite
    (issue #81): unit, container, and integration tests must produce the
    same result whether or not the host happens to have credentials set.
    Tests under `tests/e2e_inference` are intentional end-to-end tests that
    need real credentials, so they're exempt via the `e2e_inference` marker.
    """
    if request.node.get_closest_marker("e2e_inference") is not None:
        return
    for name in ISOLATION_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def host_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate a host machine with credentials and config overrides set.

    Used to verify that isolated tests/launchers don't accidentally forward
    host state they weren't explicitly given.
    """
    for name in ISOLATION_ENV_VARS:
        monkeypatch.setenv(name, f"host-{name.lower()}")
    for name in UNLISTED_SENSITIVE_ENV_VARS:
        monkeypatch.setenv(name, f"host-{name.lower()}")


@pytest.fixture
def isolated_home(tmp_path: Path) -> Path:
    """A fresh, empty directory to use as `$HOME` for a subprocess.

    Prevents host CLI configuration and credential files (e.g. `gh`'s auth
    config, gcloud application-default credentials, or OpenCode state) from
    being discovered by scripts under test unless a test explicitly
    populates this directory.
    """
    home = tmp_path / "isolated-home"
    home.mkdir(exist_ok=True)
    return home


def _configure_isolated_podman(env: dict[str, str], isolated_home: Path) -> None:
    """Expose only non-secret Podman runtime state to launcher subprocesses."""
    podman_path = shutil.which("podman")
    if not podman_path or os.environ.get(SANITIZED_TEST_WRAPPER_ACTIVE) == "1":
        return

    isolated_bin = isolated_home.parent / "isolated-bin"
    isolated_bin.mkdir(exist_ok=True)
    podman_wrapper = isolated_bin / "podman"
    runtime_root = isolated_home.parent / "podman-runtime"
    podman_home = runtime_root / "home"
    podman_config_home = runtime_root / "config"
    podman_config_dir = podman_config_home / "containers"
    podman_data_home = runtime_root / "data"
    podman_runtime_dir = runtime_root / "runtime"
    podman_tmp = runtime_root / "tmp"
    podman_docker_config = runtime_root / "docker-config"
    registry_auth_file = runtime_root / "registry-auth.json"
    for path in (
        podman_home,
        podman_config_dir,
        podman_data_home,
        podman_runtime_dir,
        podman_tmp,
        podman_docker_config,
    ):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
    registry_auth_file.write_text("{}\n")
    registry_auth_file.chmod(0o600)

    host_home = Path(os.environ["HOME"]) if os.environ.get("HOME") else None
    host_config_home_value = os.environ.get("XDG_CONFIG_HOME")
    host_config_home = (
        Path(host_config_home_value)
        if host_config_home_value
        else host_home / ".config"
        if host_home
        else None
    )
    if host_config_home:
        host_config_dir = host_config_home / "containers"
        for name in PODMAN_RUNTIME_CONFIG_FILES:
            source = host_config_dir / name
            if source.is_file():
                shutil.copyfile(source, podman_config_dir / name)
        dropins = host_config_dir / "containers.conf.d"
        if dropins.is_dir():
            shutil.copytree(
                dropins,
                podman_config_dir / "containers.conf.d",
                dirs_exist_ok=True,
            )

    host_data_home_value = os.environ.get("XDG_DATA_HOME")
    host_data_home = (
        Path(host_data_home_value)
        if host_data_home_value
        else host_home / ".local" / "share"
        if host_home
        else None
    )
    host_runtime_dir_value = os.environ.get("XDG_RUNTIME_DIR")
    podman_args: list[str] = []
    if host_data_home:
        podman_args.extend(["--root", str(host_data_home / "containers" / "storage")])
    if host_runtime_dir_value:
        podman_args.extend(
            ["--runroot", str(Path(host_runtime_dir_value) / "containers")]
        )

    wrapper_lines = [
        "#!/usr/bin/env bash\n",
        "set -euo pipefail\n",
        f"export PATH={shlex.quote(os.environ.get('PATH', ''))}\n",
        f"export HOME={shlex.quote(str(podman_home))}\n",
        f"export XDG_CONFIG_HOME={shlex.quote(str(podman_config_home))}\n",
        f"export XDG_DATA_HOME={shlex.quote(str(podman_data_home))}\n",
        f"export XDG_RUNTIME_DIR={shlex.quote(str(podman_runtime_dir))}\n",
        f"export TMPDIR={shlex.quote(str(podman_tmp))}\n",
        f"export REGISTRY_AUTH_FILE={shlex.quote(str(registry_auth_file))}\n",
        f"export DOCKER_CONFIG={shlex.quote(str(podman_docker_config))}\n",
    ]
    for name in (*ISOLATION_ENV_VARS, "DOCKER_AUTH_CONFIG"):
        wrapper_lines.append(f"unset {name}\n")
    wrapper_lines.append(f"exec {shlex.quote(podman_path)}")
    for argument in podman_args:
        wrapper_lines.append(f" {shlex.quote(argument)}")
    wrapper_lines.append(' "$@"\n')
    podman_wrapper.write_text("".join(wrapper_lines))
    podman_wrapper.chmod(podman_wrapper.stat().st_mode | 0o111)
    env["PATH"] = f"{isolated_bin}:{env.get('PATH', '')}"


@pytest.fixture
def isolated_env(host_credentials: None, isolated_home: Path) -> dict[str, str]:
    """A deterministic environment for launching `devbox` (or similar
    scripts) as a subprocess: no provider/integration credentials and no
    host home-directory state, unless a test adds them explicitly.
    """
    env = {name: os.environ[name] for name in SAFE_TEST_ENV_VARS if name in os.environ}
    env.setdefault("PATH", os.defpath)
    env["HOME"] = str(isolated_home)
    isolated_xdg = isolated_home.parent / "isolated-xdg"
    for xdg_var, subdir in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_DATA_HOME", "share"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_CACHE_HOME", "cache"),
    ):
        env[xdg_var] = str(isolated_xdg / subdir)

    _configure_isolated_podman(env, isolated_home)
    return env


# Isolation-aware Podman cleanup (issue #178)
# ---------------------------------------------------------------------------
# `isolated_env` reaches its Podman runtime through a `podman` wrapper on the
# PATH it injects, while `host_credentials` (its dependency) leaves fake
# `CONTAINERS_*` config paths in `os.environ`. A raw
# `subprocess.run(["podman", ...])` that omits `env=isolated_env` therefore
# fails with a configuration error instead of cleaning anything up, and
# `check=False` + `capture_output=True` hides that failure: tests pass while
# leaking containers and volumes. All Podman cleanup in tests must go through
# `run_podman_isolated()`, which always passes the isolated env, retries a
# bounded number of times (volume detachment races container removal), and
# raises on any real failure.
PODMAN_CLEANUP_RETRIES = 5
PODMAN_CLEANUP_RETRY_DELAY_SECONDS = 1.0
PODMAN_ABSENT_MARKERS = ("no such container", "no such volume", "no such image")


def _podman_reports_absent(result: subprocess.CompletedProcess[str]) -> bool:
    stderr = (result.stderr or "").lower()
    return any(marker in stderr for marker in PODMAN_ABSENT_MARKERS)


def run_podman_isolated(
    env: dict[str, str],
    args: list[str],
    *,
    timeout: float = 30.0,
    retries: int = 0,
    retry_delay: float = PODMAN_CLEANUP_RETRY_DELAY_SECONDS,
    allow_absent: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run ``podman`` through the ``isolated_env`` runtime and fail loudly.

    ``retries`` bounds retries for failures other than success/absence (e.g.
    ``volume rm`` racing container removal). With ``allow_absent``, a
    "no such container/volume/image" error is treated as an already-clean
    state instead of a failure; every other non-zero exit raises
    ``AssertionError`` with the exit status and captured output so a leaky
    cleanup can never pass silently.
    """
    cmd = ["podman", *args]
    result: subprocess.CompletedProcess[str] | None = None
    for attempt in range(retries + 1):
        # Plain subprocess.run (not run_in_process_group): the cleanup verbs
        # this helper takes (rm, volume rm, ps) are single leaf CLI
        # executions with no descendants to orphan, unlike the
        # launcher -> `podman build` chains issue #211 had to reap.
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=timeout,
        )
        if result.returncode == 0:
            return result
        if allow_absent and _podman_reports_absent(result):
            return result
        if attempt < retries:
            time.sleep(retry_delay)
    assert result is not None
    detail = f"{result.stdout or ''}{result.stderr or ''}".strip()
    raise AssertionError(
        f"podman {' '.join(args)} exited with status {result.returncode} "
        f"after {retries + 1} attempt(s) under the isolated env"
        f"{f': {detail}' if detail else ''}"
    )


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def devbox_path(repo_root: Path) -> Path:
    return repo_root / "devbox"


@pytest.fixture(scope="session")
def dockerfile_path(repo_root: Path) -> Path:
    return repo_root / "container" / "Dockerfile"


def devbox_context_fingerprint(context_dir: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(
        (path for path in context_dir.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(context_dir).as_posix(),
    )
    for path in files:
        digest.update(path.relative_to(context_dir).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


# Rootless Podman can take substantially longer than a few seconds to
# initialize storage and the runtime service, especially in devbox/CI
# environments. A short timeout causes healthy runtimes to be misreported
# as unavailable, silently skipping container/integration tests. This
# default is intentionally generous; it only bounds how long a *single*,
# session-cached probe may take, not the runtime of individual tests.
DEFAULT_PODMAN_PROBE_TIMEOUT = 60.0

# Allows environments (e.g. CI) to tune the probe timeout without editing
# source.
PODMAN_PROBE_TIMEOUT_ENV_VAR = "DEVBOX_PODMAN_PROBE_TIMEOUT"


@dataclass(frozen=True)
class PodmanProbeResult:
    """Outcome of checking whether a usable Podman runtime is available."""

    available: bool
    reason: str


def podman_probe_timeout() -> float:
    """Resolve the probe timeout, honoring an environment override."""
    raw_value = os.environ.get(PODMAN_PROBE_TIMEOUT_ENV_VAR)
    if not raw_value:
        return DEFAULT_PODMAN_PROBE_TIMEOUT
    try:
        value = float(raw_value)
    except ValueError:
        return DEFAULT_PODMAN_PROBE_TIMEOUT
    return value if value > 0 else DEFAULT_PODMAN_PROBE_TIMEOUT


def probe_podman_availability(timeout: float | None = None) -> PodmanProbeResult:
    """Check whether ``podman info`` succeeds within ``timeout`` seconds.

    Distinguishes three outcomes so callers can produce a clear diagnostic:
    * the ``podman`` executable is missing entirely,
    * the runtime is still initializing and exceeded the bounded timeout,
    * the command ran but failed (a genuine runtime error).
    """
    if not shutil.which("podman"):
        return PodmanProbeResult(
            available=False,
            reason="'podman' executable was not found on PATH",
        )

    effective_timeout = podman_probe_timeout() if timeout is None else timeout
    try:
        res = subprocess.run(
            ["podman", "info"],
            capture_output=True,
            timeout=effective_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return PodmanProbeResult(
            available=False,
            reason=(
                f"'podman info' did not complete within {effective_timeout:g}s "
                "(the runtime may still be initializing)"
            ),
        )
    except OSError as exc:
        return PodmanProbeResult(
            available=False,
            reason=f"failed to execute 'podman info': {exc}",
        )

    if res.returncode != 0:
        stderr = (
            res.stderr.decode(errors="replace").strip()
            if isinstance(res.stderr, bytes)
            else str(res.stderr or "").strip()
        )
        detail = f": {stderr}" if stderr else ""
        return PodmanProbeResult(
            available=False,
            reason=f"'podman info' exited with status {res.returncode}{detail}",
        )

    return PodmanProbeResult(available=True, reason="podman is available")


@pytest.fixture(scope="session")
def podman_probe_result() -> PodmanProbeResult:
    # Session-scoped so the (potentially slow) probe runs at most once per
    # test session, regardless of how many tests/fixtures depend on it.
    return probe_podman_availability()


@pytest.fixture(scope="session")
def is_podman_available(podman_probe_result: PodmanProbeResult) -> bool:
    return podman_probe_result.available


# The devbox image build must never hang the test session (issue #252): a
# wedged nested `podman build` -- observed inside devboxes as a
# fuse-overlayfs spin near the `useradd` layer, burning kernel CPU with no
# layer or network progress and ignoring SIGTERM -- otherwise spins
# forever. This bound is deliberately generous next to a healthy build
# (minutes); environments with legitimately slower cold builds can raise it.
DEFAULT_IMAGE_BUILD_TIMEOUT = 1800.0
IMAGE_BUILD_TIMEOUT_ENV_VAR = "DEVBOX_IMAGE_BUILD_TIMEOUT"

# `podman image exists` is a storage lookup once `podman info` has already
# succeeded (the session probe), so it only needs a short bound; like the
# build bound, it exists so a wedged runtime fails loudly instead of
# hanging the session.
IMAGE_EXISTS_TIMEOUT = 60.0


def image_build_timeout() -> float:
    """Resolve the devbox image build timeout, honoring an env override."""
    raw_value = os.environ.get(IMAGE_BUILD_TIMEOUT_ENV_VAR)
    if not raw_value:
        return DEFAULT_IMAGE_BUILD_TIMEOUT
    try:
        value = float(raw_value)
    except ValueError:
        return DEFAULT_IMAGE_BUILD_TIMEOUT
    return value if value > 0 else DEFAULT_IMAGE_BUILD_TIMEOUT


def ensure_devbox_image(dockerfile_path: Path) -> str:
    """Return the context-fingerprinted devbox image tag, building if needed.

    Both Podman invocations run through ``run_in_process_group`` (issue
    #211) rather than plain ``subprocess.run``: the build gets its own
    process group and a timeout, so a wedged nested `podman build` (issue
    #252) is terminated -- SIGTERM escalating to SIGKILL on the whole group
    -- and reported as a loud failure instead of hanging the session and
    leaving an orphaned CPU-spinning build behind.
    """
    image_tag = (
        f"localhost/devbox:test-{devbox_context_fingerprint(dockerfile_path.parent)}"
    )
    try:
        exists_res = run_in_process_group(
            ["podman", "image", "exists", image_tag],
            timeout=IMAGE_EXISTS_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        cleanup = getattr(exc, "process_group_cleanup", "")
        pytest.fail(
            f"'podman image exists {image_tag}' did not complete within "
            f"{IMAGE_EXISTS_TIMEOUT:g}s even though the session probe "
            "succeeded; the Podman runtime appears wedged, so this is an "
            "infrastructure failure, not a product test failure "
            f"(process-group cleanup: {cleanup or 'clean'})."
        )
    if exists_res.returncode == 0:
        return image_tag

    build_timeout = image_build_timeout()
    try:
        build_res = run_in_process_group(
            [
                "podman",
                "build",
                "--file",
                str(dockerfile_path),
                "--tag",
                image_tag,
                str(dockerfile_path.parent),
            ],
            timeout=build_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        cleanup = getattr(exc, "process_group_cleanup", "")
        pytest.fail(
            f"Timed out after {build_timeout:g}s building the devbox image "
            f"({image_tag}); the nested 'podman build' made no progress and "
            "its process group was terminated (SIGTERM escalated to SIGKILL; "
            f"cleanup: {cleanup or 'clean'}). Sustained kernel CPU with no "
            "layer or network progress matches the fuse-overlayfs wedge seen "
            "inside devboxes (issue #252). Raise "
            f"{IMAGE_BUILD_TIMEOUT_ENV_VAR} if this environment legitimately "
            "needs a longer build."
        )
    if build_res.returncode == 0:
        return image_tag
    pytest.fail(f"Failed to build devbox image: {build_res.stderr}")
    return image_tag


@pytest.fixture(scope="session")
def devbox_image(podman_probe_result: PodmanProbeResult, dockerfile_path: Path) -> str:
    if not podman_probe_result.available:
        pytest.skip(
            f"Podman is not available in the environment: {podman_probe_result.reason}"
        )
    return ensure_devbox_image(dockerfile_path)


def unique_workspace_dir(tmp_path: Path, label: str) -> Path:
    """Create a per-run unique launcher workspace directory under ``tmp_path``.

    The `devbox` launcher names its container after the workspace directory
    basename (``devbox-<dirname>``), so a fixed workspace name reuses the same
    container across runs: a container left behind by an interrupted run then
    collides with the next run, and parallel sessions collide on one container
    and the launcher's per-name lock (issue #152). A random per-run suffix
    gives every run its own container name, which the test's own cleanup
    removes.
    """
    test_dir = tmp_path / f"{label}-{uuid.uuid4().hex[:8]}"
    test_dir.mkdir()
    return test_dir


def devbox_container_name(test_dir: Path) -> str:
    """The container name the launcher derives for a workspace directory."""
    return f"devbox-{test_dir.name}"


def remove_devbox(
    devbox_path: Path, test_dir: Path, env: dict[str, str], timeout: int = 60
) -> None:
    """Remove a test devbox, falling back to host-side cleanup on failure."""
    try:
        result = run_bash_script(
            devbox_path, ["--remove"], env=env, cwd=test_dir, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        result = None
        launcher_error = f"launcher cleanup timed out: {exc}"
        cleanup = getattr(exc, "process_group_cleanup", "")
        if cleanup:
            launcher_error += f"; process-group cleanup failed: {cleanup}"
    else:
        launcher_error = (
            f"launcher cleanup exited with status {result.returncode}"
            if result.returncode != 0
            else ""
        )
    if result is not None and result.returncode == 0:
        return

    try:
        fallback = run_in_process_group(
            ["podman", "rm", "-f", devbox_container_name(test_dir)],
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AssertionError(f"{launcher_error}; host cleanup failed: {exc}") from exc
    if fallback.returncode != 0:
        detail = fallback.stderr.strip()
        raise AssertionError(
            f"{launcher_error}; host cleanup exited with status "
            f"{fallback.returncode}{f': {detail}' if detail else ''}"
        )


# Process-group bounded command runner (issue #211)
# ---------------------------------------------------------------------------
# `subprocess.run(timeout=...)` kills only the direct child on timeout, so a
# timed-out launcher/test command leaks its descendants (e.g. `devbox` ->
# `podman build` -> buildah) which keep consuming CPU, storage, and nested
# containers after the command "returned". These runners launch each bounded
# command in a dedicated process group (`start_new_session=True`) and, on
# timeout, terminate/reap the entire group, reporting any cleanup failure.
TERM_GRACE_SECONDS = 2.0
KILL_GRACE_SECONDS = 2.0


def _process_group_alive(pgid: int) -> bool:
    """True if any live member of process group ``pgid`` still exists.

    Zombies count as gone: a killed member whose parent dies without
    reaping it (e.g. under an init-less PID 1) can linger as a zombie
    forever, and killpg(2) treats zombies as live members. /proc is used
    where available to see through zombies; otherwise fall back to
    killpg(2) semantics.
    """
    try:
        entries = os.listdir("/proc")
    except OSError:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            # PermissionError or anything unexpected: assume alive, escalate.
            return True
        return True

    for name in entries:
        if not name.isdigit():
            continue
        try:
            stat = Path("/proc", name, "stat").read_text()
        except OSError:
            continue  # exited mid-scan
        # Fields after comm (which may contain spaces/parens):
        # state, ppid, pgrp, session, ...
        fields = stat[stat.rfind(")") + 1 :].split()
        if len(fields) >= 3 and fields[2] == str(pgid) and fields[0] != "Z":
            return True
    return False


def _wait_process_group_gone(
    pgid: int, proc: "subprocess.Popen[str]", grace: float
) -> bool:
    deadline = time.monotonic() + grace
    while True:
        # Reap the leader as soon as it dies: a zombie leader still counts
        # as a live group member for killpg(2), which would mask cleanup.
        proc.poll()
        if not _process_group_alive(pgid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def _process_gone(pid: int) -> bool:
    """True once ``pid`` no longer runs. A zombie counts as gone: the killed
    child is reparented once its parent dies, and hosts without a reaping
    init (e.g. pytest as container PID 1) keep zombies whose
    ``kill(pid, 0)`` still succeeds.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    # The state field follows the comm field's last closing paren.
    state = stat[stat.rfind(")") + 1 :].split()[0]
    return state == "Z"


def _wait_pid_gone(pid: int, deadline_seconds: float = 15.0) -> bool:
    """Poll until ``pid`` no longer runs (zombies count as gone)."""
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        if _process_gone(pid):
            return True
        time.sleep(0.05)
    return False


def _terminate_process_group(
    proc: "subprocess.Popen[str]",
    term_grace: float = TERM_GRACE_SECONDS,
    kill_grace: float = KILL_GRACE_SECONDS,
) -> str:
    """Terminate process group ``proc.pid`` (a session leader).

    Escalates SIGTERM to SIGKILL and reaps the leader. Returns an empty
    string on clean cleanup, or a description of what could not be
    terminated/reaped.
    """
    pgid = proc.pid  # start_new_session=True makes the child a group leader
    if os.getpgid(pgid) != pgid:
        return f"pid {pgid} is not a process group leader; refusing to signal it"
    failures: list[str] = []

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError as exc:
        failures.append(f"SIGTERM to process group {pgid} failed: {exc}")

    if not _wait_process_group_gone(pgid, proc, term_grace):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            failures.append(f"SIGKILL to process group {pgid} failed: {exc}")
        if not _wait_process_group_gone(pgid, proc, kill_grace):
            failures.append(
                f"process group {pgid} still has live members after SIGKILL"
            )

    if proc.poll() is None:
        try:
            proc.wait(timeout=kill_grace)
        except subprocess.TimeoutExpired:
            failures.append(f"command process {proc.pid} could not be reaped")

    return "; ".join(failures)


def run_in_process_group(
    cmd: list[str],
    timeout: float,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``cmd`` in a dedicated process group, bounded by ``timeout``.

    Mirrors ``subprocess.run(..., capture_output=True, text=True,
    timeout=..., check=False)`` for the success path, but on timeout the
    *entire* process group is terminated (SIGTERM, escalating to SIGKILL)
    so no descendants survive. The raised ``TimeoutExpired`` carries a
    ``process_group_cleanup`` attribute: an empty string when cleanup was
    clean, otherwise a description of the failure (also reported to
    stderr).
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=str(cwd) if cwd else None,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        cleanup_error = _terminate_process_group(proc)
        try:
            proc.communicate(timeout=KILL_GRACE_SECONDS)  # reap + drain pipes
        except subprocess.TimeoutExpired:
            # A descendant escaped the group and holds the pipes open; do
            # not turn the bounded timeout into an unbounded hang.
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
            cleanup_error = cleanup_error or (
                "output pipes stayed open after group termination "
                "(a descendant escaped the process group)"
            )
        exc.process_group_cleanup = cleanup_error  # type: ignore[attr-defined]
        if cleanup_error:
            print(
                f"WARNING: process-group cleanup for {cmd[0]!r} failed: "
                f"{cleanup_error}",
                file=sys.stderr,
            )
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def run_bash_script(
    script_path: Path,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    cmd = [str(script_path)] + (args or [])
    return run_in_process_group(cmd, timeout=timeout, env=env, cwd=cwd)


def run_in_devbox(
    image: str,
    cmd: list[str],
    user: str | None = None,
    volumes: list[str] | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    exec_cmd = ["podman", "run", "--rm"]
    if user:
        exec_cmd.extend(["--user", user])
    if volumes:
        for v in volumes:
            exec_cmd.extend(["--volume", v])
    exec_cmd.append(image)
    exec_cmd.extend(cmd)
    return run_in_process_group(exec_cmd, timeout=timeout)
