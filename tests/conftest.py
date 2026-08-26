import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def devbox_path(repo_root: Path) -> Path:
    return repo_root / "devbox"


@pytest.fixture(scope="session")
def local_llm_dir(repo_root: Path) -> Path:
    return repo_root / "local-llm"


@pytest.fixture(scope="session")
def litellm_proxy_dir(repo_root: Path) -> Path:
    return repo_root / "litellm-proxy"


@pytest.fixture(scope="session")
def switchyard_proxy_dir(repo_root: Path) -> Path:
    return repo_root / "switchyard-proxy"


@pytest.fixture(scope="session")
def opencode_json_path(repo_root: Path) -> Path:
    return repo_root / "opencode.json"


@pytest.fixture(scope="session")
def dockerfile_path(repo_root: Path) -> Path:
    return repo_root / "container" / "Dockerfile"


@pytest.fixture(scope="session")
def is_podman_available() -> bool:
    if not shutil.which("podman"):
        return False
    try:
        res = subprocess.run(
            ["podman", "info"],
            capture_output=True,
            timeout=5,
            check=False,
        )
        return res.returncode == 0
    except Exception:
        return False


@pytest.fixture(scope="session")
def devbox_image(
    is_podman_available: bool, dockerfile_path: Path, repo_root: Path
) -> str:
    if not is_podman_available:
        pytest.skip("Podman is not available in the environment")

    for tag in ("devbox:latest", "localhost/devbox:latest"):
        res = subprocess.run(
            ["podman", "image", "exists", tag],
            capture_output=True,
            check=False,
        )
        if res.returncode == 0:
            return tag

    build_res = subprocess.run(
        [
            "podman",
            "build",
            "--file",
            str(dockerfile_path),
            "--tag",
            "devbox:latest",
            str(dockerfile_path.parent),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if build_res.returncode == 0:
        return "devbox:latest"
    pytest.skip(f"Failed to find or build devbox image: {build_res.stderr}")
    return "devbox:latest"


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
