import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

LAUNCHER_OPTIONAL_ENV_VARS = (
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
    "OPENROUTER_API_KEY",
    "GOOGLE_CLOUD_PROJECT",
    "VERTEX_LOCATION",
)


@pytest.fixture
def host_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in LAUNCHER_OPTIONAL_ENV_VARS:
        monkeypatch.setenv(name, f"host-{name.lower()}")


@pytest.fixture
def isolated_env(host_credentials) -> dict[str, str]:
    env = os.environ.copy()
    for name in LAUNCHER_OPTIONAL_ENV_VARS:
        env.pop(name, None)
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
