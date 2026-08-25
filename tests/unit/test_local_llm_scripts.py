import json
import os
import stat
from pathlib import Path

import pytest

from tests.conftest import run_bash_script


@pytest.fixture
def mock_llm_env(tmp_path: Path):
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

if [ "$1" = "volume" ] && [ "$2" = "exists" ]; then
    exit 0
fi

if [ "$1" = "exec" ]; then
    # Return 0 for ollama list / pull
    exit 0
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
echo '{{"object":"list","data":[]}}'
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
def test_start_llm_server(local_llm_dir: Path, mock_llm_env):
    env, log_file = mock_llm_env
    script = local_llm_dir / "start-llm-server.sh"

    res = run_bash_script(script, env=env)
    assert res.returncode == 0

    calls = parse_calls(log_file)
    run_call = next(
        (c for c in calls if c[0] == "podman" and len(c) > 1 and c[1] == "run"),
        None,
    )
    assert run_call is not None
    assert "--publish" in run_call
    assert "127.0.0.1:11434:11434" in run_call
    assert "--env" in run_call
    assert "OLLAMA_FLASH_ATTENTION=1" in run_call
    assert "OLLAMA_KV_CACHE_TYPE=q8_0" in run_call

    # Verify model pulls
    pull_calls = [
        c
        for c in calls
        if c[0] == "podman" and len(c) > 4 and c[1] == "exec" and c[4] == "pull"
    ]
    pulled_models = [c[5] for c in pull_calls]
    assert "gpt-oss:20b" in pulled_models
    assert "qwen3.8:27b" in pulled_models


@pytest.mark.unit
def test_status_llm_server_running(local_llm_dir: Path, mock_llm_env):
    env, log_file = mock_llm_env
    env["MOCK_CONTAINER_EXISTS"] = "1"
    script = local_llm_dir / "status-llm-server.sh"

    res = run_bash_script(script, env=env)
    assert res.returncode == 0
    assert "Models:" in res.stdout
    assert "API health" in res.stdout


@pytest.mark.unit
def test_status_llm_server_not_running(local_llm_dir: Path, mock_llm_env):
    env, log_file = mock_llm_env
    env["MOCK_CONTAINER_EXISTS"] = "0"
    script = local_llm_dir / "status-llm-server.sh"

    res = run_bash_script(script, env=env)
    assert res.returncode == 1
    assert "does not exist" in res.stderr


@pytest.mark.unit
def test_stop_llm_server_no_purge(local_llm_dir: Path, mock_llm_env):
    env, log_file = mock_llm_env
    script = local_llm_dir / "stop-llm-server.sh"

    res = run_bash_script(script, env=env)
    assert res.returncode == 0
    assert "Stopping and removing container" in res.stdout
    assert "model weights kept in volume" in res.stdout

    calls = parse_calls(log_file)
    assert any(c[0] == "podman" and c[1:3] == ["rm", "-f"] for c in calls)
    assert not any(c[0] == "podman" and c[1:3] == ["volume", "rm"] for c in calls)


@pytest.mark.unit
def test_stop_llm_server_with_purge(local_llm_dir: Path, mock_llm_env):
    env, log_file = mock_llm_env
    script = local_llm_dir / "stop-llm-server.sh"

    res = run_bash_script(script, ["--purge"], env=env)
    assert res.returncode == 0
    assert "Removing volume" in res.stdout

    calls = parse_calls(log_file)
    assert any(c[0] == "podman" and c[1:3] == ["volume", "rm"] for c in calls)
