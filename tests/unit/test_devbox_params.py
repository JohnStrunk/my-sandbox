import json
import os
import stat
from pathlib import Path

import pytest

from tests.conftest import run_bash_script


@pytest.fixture
def mock_podman_env(tmp_path: Path):
    bin_dir = tmp_path / "mock_bin"
    bin_dir.mkdir()
    log_file = tmp_path / "podman_calls.jsonl"

    mock_script = bin_dir / "podman"
    mock_script.write_text(f"""#!/usr/bin/env bash
python3 -c '
import sys, json
with open(sys.argv[1], "a") as f:
    f.write(json.dumps(sys.argv[2:]) + "\\n")
' "{log_file}" "$@"

if [ "$1" = "run" ] && [ "$2" = "--rm" ]; then
    if [ "$4" = "id" ] && [ "$5" = "-u" ]; then
        echo "1000"
        exit 0
    fi
    if [ "$4" = "id" ] && [ "$5" = "-g" ]; then
        echo "1000"
        exit 0
    fi
fi

if [ "$1" = "container" ] && [ "$2" = "exists" ]; then
    if [ "$3" = "mock-llm-running" ]; then
        exit 0
    fi
    # Default container does not exist
    exit 1
fi

if [ "$1" = "inspect" ]; then
    echo "true"
    exit 0
fi

if [ "$1" = "exec" ]; then
    # Mock /proc/self/uid_map and gid_map query
    if echo "$*" | grep -q "uid_map"; then
        echo "65535"
        exit 0
    fi
    if echo "$*" | grep -q "gid_map"; then
        echo "65535"
        exit 0
    fi
    exit 0
fi

exit 0
""")
    mock_script.chmod(mock_script.stat().st_mode | stat.S_IEXEC)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    return env, log_file


def parse_podman_calls(log_file: Path) -> list[list[str]]:
    if not log_file.exists():
        return []
    calls = []
    for line in log_file.read_text().splitlines():
        if line.strip():
            calls.append(json.loads(line))
    return calls


@pytest.mark.unit
def test_devbox_gemini_env(devbox_path: Path, mock_podman_env, tmp_path: Path):
    env, log_file = mock_podman_env
    env["GEMINI_API_KEY"] = "mock-gemini-token"  # pragma: allowlist secret

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next(
        (c for c in calls if c and c[0] == "run" and "-d" in c),
        None,
    )
    assert run_call is not None
    assert "--env" in run_call
    assert "GEMINI_API_KEY=mock-gemini-token" in run_call
    assert "GOOGLE_GENERATIVE_AI_API_KEY=mock-gemini-token" in run_call


@pytest.mark.unit
def test_devbox_litemaas_env(devbox_path: Path, mock_podman_env, tmp_path: Path):
    env, log_file = mock_podman_env
    env["LITEMAAS_API_KEY"] = "mock-litemaas-token"  # pragma: allowlist secret

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    assert "LITEMAAS_API_KEY=mock-litemaas-token" in run_call


@pytest.mark.unit
def test_devbox_vertex_env(devbox_path: Path, mock_podman_env, tmp_path: Path):
    env, log_file = mock_podman_env
    env["GOOGLE_CLOUD_PROJECT"] = "my-gcp-project"
    env["VERTEX_LOCATION"] = "us-central1"

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    assert "GOOGLE_CLOUD_PROJECT=my-gcp-project" in run_call
    assert "VERTEX_LOCATION=us-central1" in run_call  # pragma: allowlist secret


@pytest.mark.unit
def test_devbox_llm_loopback_network(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env["LLM_CONTAINER_NAME"] = "mock-llm-running"

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    assert "--network" in run_call
    assert "slirp4netns:allow_host_loopback=true" in run_call


@pytest.mark.unit
def test_devbox_config_volume_mounts(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    env["HOME"] = str(fake_home)

    (fake_home / ".config" / "acli").mkdir(parents=True)
    (fake_home / ".config" / "gws").mkdir(parents=True)
    (fake_home / ".config" / "opencode").mkdir(parents=True)

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    volumes = [run_call[i + 1] for i, arg in enumerate(run_call) if arg == "--volume"]
    assert any(":/sandbox/.config/acli" in v for v in volumes)
    assert any(":/sandbox/.config/gws" in v for v in volumes)
    assert any(":/sandbox/.config/opencode" in v for v in volumes)
