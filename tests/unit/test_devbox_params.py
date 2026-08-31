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
    if [ "$3" = "-i" ] && [ "$4" = "devbox:latest" ] && [ "$5" = "jq" ]; then
        python3 -c '
import json
import sys

def merge(left, right):
    result = left.copy()
    for key in right:
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(right[key], dict)
        ):
            result[key] = merge(result[key], right[key])
        else:
            result[key] = right[key]
    return result

result = dict()
text = sys.stdin.read()
index = 0
decoder = json.JSONDecoder()
while index < len(text):
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text):
        break
    value, index = decoder.raw_decode(text, index)
    result = merge(result, value)
print(json.dumps(result, separators=(",", ":")))
'
        exit 0
    fi
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
    case "$3" in
        mock-llm-running|mock-proxy-running|mock-switchyard-running)
            exit 0
            ;;
    esac
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
@pytest.mark.parametrize("token_name", ["GH_TOKEN", "GITHUB_TOKEN"])
def test_devbox_github_mcp_config_from_token(
    devbox_path: Path, mock_podman_env, tmp_path: Path, token_name: str
):
    env, log_file = mock_podman_env
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    env[token_name] = "mock-github-token"  # pragma: allowlist secret

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    assert "GH_TOKEN=mock-github-token" in run_call
    assert "GITHUB_TOKEN=mock-github-token" in run_call  # pragma: allowlist secret

    env_values = [
        run_call[index + 1] for index, arg in enumerate(run_call[:-1]) if arg == "--env"
    ]
    config_value = next(
        value for value in env_values if value.startswith("OPENCODE_CONFIG_CONTENT=")
    )
    config = json.loads(config_value.split("=", 1)[1])
    assert config == {
        "$schema": "https://opencode.ai/config.json",
        "mcp": {
            "devbox-github": {
                "type": "remote",
                "url": "https://api.githubcopilot.com/mcp/",
                "enabled": True,
                "oauth": False,
                "headers": {
                    "Authorization": "Bearer {env:GH_TOKEN}",
                },
            },
        },
    }
    assert "mock-github-token" not in config_value


@pytest.mark.unit
def test_devbox_does_not_add_github_mcp_without_credentials(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    for name in (
        "IGLOO_MCP_COMMUNITY",
        "IGLOO_MCP_COMMUNITY_KEY",
        "IGLOO_MCP_APP_PASS",
        "IGLOO_MCP_APP_ID",
        "IGLOO_MCP_USERNAME",
        "IGLOO_MCP_PASSWORD",
    ):
        env.pop(name, None)

    # Keep the test independent from any host gh login.
    fake_gh = Path(env["PATH"].split(":", 1)[0]) / "gh"
    fake_gh.write_text("#!/usr/bin/env bash\nexit 1\n")
    fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IEXEC)

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    env_values = [
        run_call[index + 1] for index, arg in enumerate(run_call[:-1]) if arg == "--env"
    ]
    assert not any(value.startswith("OPENCODE_CONFIG_CONTENT=") for value in env_values)


@pytest.mark.unit
def test_devbox_the_source_mcp_config(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    source_credentials = {
        "IGLOO_MCP_COMMUNITY": "mock-community",
        "IGLOO_MCP_COMMUNITY_KEY": "mock-community-key",
        "IGLOO_MCP_APP_PASS": "mock-app-pass",
        "IGLOO_MCP_APP_ID": "mock-app-id",
        "IGLOO_MCP_USERNAME": "mock-username",
        "IGLOO_MCP_PASSWORD": "mock-password",  # pragma: allowlist secret
    }
    env.update(source_credentials)

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    for name, value in source_credentials.items():
        assert f"{name}={value}" in run_call  # pragma: allowlist secret

    env_values = [
        run_call[index + 1] for index, arg in enumerate(run_call[:-1]) if arg == "--env"
    ]
    config_value = next(
        value for value in env_values if value.startswith("OPENCODE_CONFIG_CONTENT=")
    )
    config = json.loads(config_value.split("=", 1)[1])
    assert config == {
        "$schema": "https://opencode.ai/config.json",
        "mcp": {
            "the-source": {
                "enabled": True,
                "type": "local",
                "command": [
                    "uvx",
                    "--from",
                    "git+https://github.com/johnstrunk/igloo-mcp",
                    "igloo-mcp",
                ],
                "environment": {
                    "IGLOO_MCP_COMMUNITY": "{env:IGLOO_MCP_COMMUNITY}",
                    "IGLOO_MCP_COMMUNITY_KEY": "{env:IGLOO_MCP_COMMUNITY_KEY}",
                    "IGLOO_MCP_APP_PASS": "{env:IGLOO_MCP_APP_PASS}",
                    "IGLOO_MCP_APP_ID": "{env:IGLOO_MCP_APP_ID}",
                    "IGLOO_MCP_USERNAME": "{env:IGLOO_MCP_USERNAME}",
                    "IGLOO_MCP_PASSWORD": "{env:IGLOO_MCP_PASSWORD}",
                    "IGLOO_MCP_SERVER_NAME": "The Source",
                    "IGLOO_MCP_SERVER_INSTRUCTIONS": (
                        "This server provides search and fetch capabilities for The "
                        "Source, Red Hat's intranet, containing articles with guides, "
                        "instructions, and useful information that helps team members "
                        "do their jobs and contribute to Red Hat."
                    ),
                },
            },
        },
    }
    for value in source_credentials.values():
        assert value not in config_value


@pytest.mark.unit
def test_devbox_merges_mcp_configurations(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env["GH_TOKEN"] = "mock-github-token"  # pragma: allowlist secret
    env.update(
        {
            "IGLOO_MCP_COMMUNITY": "mock-community",
            "IGLOO_MCP_COMMUNITY_KEY": "mock-community-key",
            "IGLOO_MCP_APP_PASS": "mock-app-pass",
            "IGLOO_MCP_APP_ID": "mock-app-id",
            "IGLOO_MCP_USERNAME": "mock-username",
            "IGLOO_MCP_PASSWORD": "mock-password",  # pragma: allowlist secret
        }
    )

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    env_values = [
        run_call[index + 1] for index, arg in enumerate(run_call[:-1]) if arg == "--env"
    ]
    config_value = next(
        value for value in env_values if value.startswith("OPENCODE_CONFIG_CONTENT=")
    )
    config = json.loads(config_value.split("=", 1)[1])
    assert set(config["mcp"]) == {"devbox-github", "the-source"}
    assert config["mcp"]["devbox-github"]["headers"] == {
        "Authorization": "Bearer {env:GH_TOKEN}"
    }
    assert config["mcp"]["the-source"]["environment"]["IGLOO_MCP_APP_ID"] == (
        "{env:IGLOO_MCP_APP_ID}"
    )
    assert "mock-github-token" not in config_value


@pytest.mark.unit
def test_devbox_gitlab_env(devbox_path: Path, mock_podman_env, tmp_path: Path):
    env, log_file = mock_podman_env
    env["GITLAB_HOST"] = "gitlab.example.com"
    env["GITLAB_TOKEN"] = "mock-gitlab-token"  # pragma: allowlist secret

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    assert "GITLAB_HOST=gitlab.example.com" in run_call
    assert "GITLAB_TOKEN=mock-gitlab-token" in run_call  # pragma: allowlist secret


@pytest.mark.unit
def test_devbox_litellm_env(devbox_path: Path, mock_podman_env, tmp_path: Path):
    env, log_file = mock_podman_env
    env["LITELLM_API_KEY"] = "mock-litellm-token"  # pragma: allowlist secret

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    assert "LITELLM_API_KEY=mock-litellm-token" in run_call


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
def test_devbox_proxy_loopback_network(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env["PROXY_CONTAINER_NAME"] = "mock-proxy-running"

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
def test_devbox_switchyard_loopback_network(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env["SWITCHYARD_CONTAINER_NAME"] = "mock-switchyard-running"

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
    env.pop("XDG_DATA_HOME", None)

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
    expected_data_dir = fake_home / ".local" / "share" / "opencode"
    assert expected_data_dir.is_dir()
    assert any(
        f"{expected_data_dir}:/sandbox/.local/share/opencode" in v for v in volumes
    )


@pytest.mark.unit
def test_devbox_opencode_data_volume_honors_xdg_data_home(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    xdg_data_home = tmp_path / "xdg-data"
    env["HOME"] = str(fake_home)
    env["XDG_DATA_HOME"] = str(xdg_data_home)

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    expected_data_dir = xdg_data_home / "opencode"
    assert expected_data_dir.is_dir()
    assert any(
        f"{expected_data_dir}:/sandbox/.local/share/opencode" in v for v in run_call
    )
