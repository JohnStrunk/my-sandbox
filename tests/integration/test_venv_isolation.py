import os
import subprocess
import time
from pathlib import Path

import pytest

from tests.conftest import run_bash_script

HOST_POISONED_PYVENV_CFG = "home = /nonexistent/host/python\n"


@pytest.mark.integration
def test_host_venv_is_shadowed_inside_devbox(
    devbox_path: Path,
    devbox_image: str,
    tmp_path: Path,
    isolated_env: dict[str, str],
):
    # Issue #154: a host worktree's .venv contains interpreter paths and
    # shebangs that only exist on the host. The launcher must shadow it with
    # a container-local volume so `uv run` inside devbox never executes those
    # host paths, while leaving the host .venv byte-for-byte untouched.
    test_dir = tmp_path / f"venv_project_{os.getpid()}"
    (test_dir / ".venv" / "bin").mkdir(parents=True)
    (test_dir / ".venv" / "pyvenv.cfg").write_text(HOST_POISONED_PYVENV_CFG)
    (test_dir / ".venv" / "bin" / "python").write_text("#!/nonexistent/host/python\n")
    (test_dir / ".venv" / "bin" / "pytest").write_text(
        "#!/nonexistent/host/python\nraise SystemExit(3)\n"
    )
    (test_dir / "pyproject.toml").write_text(
        '[project]\nname = "venv-probe"\nversion = "0.0.0"\n'
        'requires-python = ">=3.9"\ndependencies = []\n'
    )
    container_name = f"devbox-{test_dir.name}"
    volume_name = f"devbox-venv-{test_dir.name}"

    try:
        res = run_bash_script(
            devbox_path,
            ["--recreate", "bash", "-c", "echo entries=$(ls -A .venv | wc -l)"],
            env=isolated_env,
            cwd=test_dir,
            timeout=300,
        )
        assert res.returncode == 0, f"{res.stdout}\n{res.stderr}"
        assert "Shadowing host .venv" in res.stdout
        assert "entries=0" in res.stdout

        uv_run = run_bash_script(
            devbox_path,
            ["uv", "run", "python", "-c", "print('uv-run-ok')"],
            env=isolated_env,
            cwd=test_dir,
            timeout=300,
        )
        assert uv_run.returncode == 0, f"{uv_run.stdout}\n{uv_run.stderr}"
        assert "uv-run-ok" in uv_run.stdout

        # The exact reported symptom: uv must be able to spawn `pytest`
        # rather than reusing the host .venv's broken shebang.
        pytest_run = run_bash_script(
            devbox_path,
            ["uv", "run", "--with", "pytest", "pytest", "--version"],
            env=isolated_env,
            cwd=test_dir,
            timeout=300,
        )
        assert pytest_run.returncode == 0, f"{pytest_run.stdout}\n{pytest_run.stderr}"
        assert "pytest" in pytest_run.stdout

        # The host .venv was masked, never read or written: its poisoned
        # interpreter metadata is still exactly what this test planted.
        planted_cfg = (test_dir / ".venv" / "pyvenv.cfg").read_text()
        assert planted_cfg == HOST_POISONED_PYVENV_CFG
        assert (
            (test_dir / ".venv" / "bin" / "pytest")
            .read_text()
            .startswith("#!/nonexistent/host/python")
        )
    finally:
        subprocess.run(
            [str(devbox_path), "--remove"],
            capture_output=True,
            text=True,
            env=isolated_env,
            cwd=test_dir,
            check=False,
            timeout=60,
        )
        subprocess.run(
            ["podman", "rm", "-f", container_name],
            capture_output=True,
            check=False,
            env=isolated_env,
            timeout=30,
        )
        # Retry: the volume detaches only once the container is fully gone.
        rm_volume = None
        for _ in range(5):
            rm_volume = subprocess.run(
                ["podman", "volume", "rm", volume_name],
                capture_output=True,
                check=False,
                env=isolated_env,
                timeout=30,
            )
            if rm_volume.returncode == 0:
                break
            time.sleep(1)
        assert rm_volume is not None and rm_volume.returncode == 0, (
            rm_volume.stderr.decode() if rm_volume else "volume rm never ran"
        )
