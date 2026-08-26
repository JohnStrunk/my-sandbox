import json
import os
import stat
from pathlib import Path

import pytest

from tests.conftest import run_bash_script


@pytest.fixture
def mock_proxy_env(tmp_path: Path):
    bin_dir = tmp_path / "mock_bin"
    bin_dir.mkdir()
    log_file = tmp_path / "calls.jsonl"

    mock_podman = bin_dir / "podman"
    mock_podman.write_text(f"""#!/usr/bin/env bash
python3 -c '
import sys, json
with open(sys.argv[1], "a") as f:
    f.write(json.dumps(["podman"] + sys.argv[2:]) + "\\n")
' "{log_file}" "$@"

if [ "$1" = "container" ] && [ "$2" = "exists" ]; then
    if [ "${{MOCK_CONTAINER_EXISTS:-1}}" = "1" ]; then
        exit 0
    else
        exit 1
    fi
fi

exit 0
""")
    mock_podman.chmod(mock_podman.stat().st_mode | stat.S_IEXEC)

    mock_curl = bin_dir / "curl"
    mock_curl.write_text(f"""#!/usr/bin/env bash
python3 -c '
import sys, json
with open(sys.argv[1], "a") as f:
    f.write(json.dumps(["curl"] + sys.argv[2:]) + "\\n")
' "{log_file}" "$@"
echo '{{"status":"healthy","data":[]}}'
exit 0
""")
    mock_curl.chmod(mock_curl.stat().st_mode | stat.S_IEXEC)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    return env, log_file


def parse_calls(log_file: Path) -> list[list[str]]:
    if not log_file.exists():
        return []
    calls = []
    for line in log_file.read_text().splitlines():
        if line.strip():
            calls.append(json.loads(line))
    return calls


@pytest.mark.unit
def test_start_proxy_server(litellm_proxy_dir: Path, mock_proxy_env):
    env, log_file = mock_proxy_env
    script = litellm_proxy_dir / "start-proxy-server.sh"

    res = run_bash_script(script, env=env)
    assert res.returncode == 0
    assert "LiteLLM autorouting proxy is up" in res.stdout

    calls = parse_calls(log_file)
    run_call = next(
        (c for c in calls if c[0] == "podman" and len(c) > 1 and c[1] == "run"),
        None,
    )
    assert run_call is not None
    assert "--publish" in run_call
    assert "127.0.0.1:4000:4000" in run_call
    assert "--network" in run_call
    assert "slirp4netns:allow_host_loopback=true" in run_call


@pytest.mark.unit
def test_start_proxy_server_missing_config(litellm_proxy_dir: Path, mock_proxy_env):
    env, _ = mock_proxy_env
    env["PROXY_CONFIG_FILE"] = "/nonexistent/config.yaml"
    script = litellm_proxy_dir / "start-proxy-server.sh"

    res = run_bash_script(script, env=env)
    assert res.returncode == 1
    assert "does not exist" in res.stderr


@pytest.mark.unit
def test_start_proxy_server_env_passthrough(litellm_proxy_dir: Path, mock_proxy_env):
    env, log_file = mock_proxy_env
    env["GEMINI_API_KEY"] = "mock-gemini-key"  # pragma: allowlist secret
    env["LITEMAAS_API_KEY"] = "mock-litemaas-key"  # pragma: allowlist secret
    script = litellm_proxy_dir / "start-proxy-server.sh"

    res = run_bash_script(script, env=env)
    assert res.returncode == 0

    calls = parse_calls(log_file)
    run_call = next(
        (c for c in calls if c[0] == "podman" and len(c) > 1 and c[1] == "run"),
        None,
    )
    assert run_call is not None
    assert "--env" in run_call
    assert any("GEMINI_API_KEY=mock-gemini-key" in arg for arg in run_call)
    assert any("LITEMAAS_API_KEY=mock-litemaas-key" in arg for arg in run_call)


@pytest.mark.unit
def test_status_proxy_server_running(litellm_proxy_dir: Path, mock_proxy_env):
    env, log_file = mock_proxy_env
    env["MOCK_CONTAINER_EXISTS"] = "1"
    script = litellm_proxy_dir / "status-proxy-server.sh"

    res = run_bash_script(script, env=env)
    assert res.returncode == 0
    assert "API readiness" in res.stdout
    assert "Available models" in res.stdout


@pytest.mark.unit
def test_status_proxy_server_not_running(litellm_proxy_dir: Path, mock_proxy_env):
    env, log_file = mock_proxy_env
    env["MOCK_CONTAINER_EXISTS"] = "0"
    script = litellm_proxy_dir / "status-proxy-server.sh"

    res = run_bash_script(script, env=env)
    assert res.returncode == 1
    assert "does not exist" in res.stderr


@pytest.mark.unit
def test_stop_proxy_server(litellm_proxy_dir: Path, mock_proxy_env):
    env, log_file = mock_proxy_env
    script = litellm_proxy_dir / "stop-proxy-server.sh"

    res = run_bash_script(script, env=env)
    assert res.returncode == 0
    assert "Stopping and removing container" in res.stdout

    calls = parse_calls(log_file)
    assert any(c[0] == "podman" and c[1:3] == ["rm", "-f"] for c in calls)
