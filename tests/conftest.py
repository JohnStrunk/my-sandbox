import hashlib
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
def devbox_image(is_podman_available: bool, dockerfile_path: Path) -> str:
    if not is_podman_available:
        pytest.skip("Podman is not available in the environment")

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
