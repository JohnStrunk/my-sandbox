import hashlib
import os
import shutil
import subprocess
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
    "OPENROUTER_API_KEY",
    "GOOGLE_CLOUD_PROJECT",
    "VERTEX_LOCATION",
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
    for name in CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def host_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate a host machine that has every optional credential set.

    Used to verify that isolated tests/launchers don't accidentally forward
    credentials they weren't explicitly given.
    """
    for name in CREDENTIAL_ENV_VARS:
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


@pytest.fixture
def isolated_env(host_credentials: None, isolated_home: Path) -> dict[str, str]:
    """A deterministic environment for launching `devbox` (or similar
    scripts) as a subprocess: no provider/integration credentials and no
    host home-directory state, unless a test adds them explicitly.
    """
    env = os.environ.copy()
    for name in CREDENTIAL_ENV_VARS:
        env.pop(name, None)
    env["HOME"] = str(isolated_home)
    for xdg_var, subdir in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_DATA_HOME", "share"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_CACHE_HOME", "cache"),
    ):
        env[xdg_var] = str(isolated_home / subdir)
    return env


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def devbox_path(repo_root: Path) -> Path:
    return repo_root / "devbox"


@pytest.fixture(scope="session")
def opencode_json_path(repo_root: Path) -> Path:
    return repo_root / "opencode.json"


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
