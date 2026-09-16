import hashlib
import os
import shlex
import shutil
import subprocess
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
SAFE_TEST_ENV_VARS = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "CI")
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


@pytest.fixture(scope="session")
def devbox_image(podman_probe_result: PodmanProbeResult, dockerfile_path: Path) -> str:
    if not podman_probe_result.available:
        pytest.skip(
            f"Podman is not available in the environment: {podman_probe_result.reason}"
        )

    image_tag = (
        f"localhost/devbox:test-{devbox_context_fingerprint(dockerfile_path.parent)}"
    )
    res = subprocess.run(
        ["podman", "image", "exists", image_tag],
        capture_output=True,
        check=False,
    )
    if res.returncode == 0:
        return image_tag

    build_res = subprocess.run(
        [
            "podman",
            "build",
            "--file",
            str(dockerfile_path),
            "--tag",
            image_tag,
            str(dockerfile_path.parent),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if build_res.returncode == 0:
        return image_tag
    pytest.fail(f"Failed to build devbox image: {build_res.stderr}")
    return image_tag


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
    else:
        launcher_error = (
            f"launcher cleanup exited with status {result.returncode}"
            if result.returncode != 0
            else ""
        )
    if result is not None and result.returncode == 0:
        return

    try:
        fallback = subprocess.run(
            ["podman", "rm", "-f", devbox_container_name(test_dir)],
            env=env,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AssertionError(f"{launcher_error}; host cleanup failed: {exc}") from exc
    if fallback.returncode != 0:
        detail = fallback.stderr.strip()
        raise AssertionError(
            f"{launcher_error}; host cleanup exited with status "
            f"{fallback.returncode}{f': {detail}' if detail else ''}"
        )


def run_bash_script(
    script_path: Path,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    cmd = [str(script_path)] + (args or [])
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd) if cwd else None,
        timeout=timeout,
        check=False,
    )


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
    return subprocess.run(
        exec_cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
