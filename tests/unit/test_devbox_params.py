import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from tests.conftest import CREDENTIAL_ENV_VARS, run_bash_script


@pytest.fixture
def mock_podman_env(tmp_path: Path, isolated_env):
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
        if echo "$*" | grep -q 'map(select(.id'; then
            python3 -c '
import json
import sys

value = json.load(sys.stdin)
print(json.dumps({{
    item["id"]: {{"name": item["id"]}}
    for item in value.get("data", [])
    if isinstance(item.get("id"), str) and item["id"]
}}))
'
            exit 0
        fi
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

if [ "$1" = "run" ] && [ "$2" = "-d" ] \
    && [ "${{MOCK_REJECT_NESTED_SYSCTLS:-}}" = "1" ]; then
    case "$*" in
        *--sysctl*)
            echo "Error: OCI runtime error: crun: open" \
                "/proc/sys/net/ipv4/conf/default/route_localnet:" \
                "Read-only file system" >&2
            exit 126
            ;;
    esac
fi

if [ "$1" = "run" ] && [ "$2" = "-d" ] \
    && [ "${{MOCK_FAIL_CONTAINER_RUN:-}}" = "1" ]; then
    echo "Error: image create failed" >&2
    exit 125
fi

if [ "$1" = "run" ] && [ "$2" = "-d" ] \
    && [ "${{MOCK_FAIL_UNRELATED_SYSCTL:-}}" = "1" ]; then
    echo "Error: storage setup failed at /proc/sys/net/ipv4/ping_group_range" >&2
    exit 125
fi

if [ "$1" = "run" ] && [ "$2" = "-d" ] \
    && [ "${{MOCK_REJECT_IPV6_SYSCTL:-}}" = "1" ]; then
    case "$*" in
        *--sysctl*)
            echo "Error: crun: open /proc/sys/net/ipv6/conf/default/accept_ra" >&2
            exit 126
            ;;
    esac
fi

if [ "$1" = "run" ] && [ "$2" = "-d" ] \
    && [ "${{MOCK_REJECT_IPV6_FORWARDING:-}}" = "1" ]; then
    case "$*" in
        *--sysctl*)
            echo "Error: crun: open /proc/sys/net/ipv6/conf/all/forwarding" >&2
            exit 126
            ;;
    esac
fi

if [ "$1" = "container" ] && [ "$2" = "exists" ]; then
    # Default container does not exist
    [ "${{MOCK_CONTAINER_EXISTS:-}}" = "1" ] && exit 0
    if [ "${{MOCK_CONTAINER_EXISTS_ERROR:-}}" = "1" ]; then
        echo "Error: storage unavailable" >&2
        exit 125
    fi
    exit 1
fi

if [ "$1" = "rm" ] && [ "${{MOCK_RM_FAIL:-}}" = "1" ]; then
    echo "Error: removal failed" >&2
    exit 1
fi

if [ "$1" = "inspect" ]; then
    echo "true"
    exit 0
fi

if [ "$1" = "exec" ]; then
    # Mock /proc/self/uid_map and gid_map query
    if echo "$*" | grep -q "uid_map"; then
        [ "${{MOCK_UID_MAP_FAIL:-}}" = "1" ] && exit 1
        echo "65535"
        exit 0
    fi
    if echo "$*" | grep -q "gid_map"; then
        echo "65535"
        exit 0
    fi
    # Mock the container's own `git config --global <key> [value]`, so
    # tests can simulate an identity already configured inside the
    # (persistent) container via MOCK_CONTAINER_GIT_NAME/_EMAIL, without
    # a real container.
    if [ "$3" = "git" ] && [ "$4" = "config" ] && [ "$5" = "--global" ]; then
        if [ -n "${{7:-}}" ]; then
            # Simulated `git config --global <key> <value>` (set)
            if [ "${{MOCK_GIT_CONFIG_SET_FAILS:-}}" = "1" ]; then
                exit 1
            fi
            exit 0
        fi
        case "$6" in
            user.name)
                name="${{MOCK_CONTAINER_GIT_NAME:-}}"
                [ -n "$name" ] && echo "$name" && exit 0
                exit 1
                ;;
            user.email)
                email="${{MOCK_CONTAINER_GIT_EMAIL:-}}"
                [ -n "$email" ] && echo "$email" && exit 0
                exit 1
                ;;
        esac
    fi
    if [ "$3" = "gh" ] && [ "$4" = "auth" ] && [ "$5" = "setup-git" ]; then
        if [ "${{MOCK_GH_AUTH_SETUP_GIT_FAILS:-}}" = "1" ]; then
            exit 1
        fi
    fi
    if [ "$3" = "podman" ] && [ "$4" = "create" ]; then
        echo "mock-preflight-container"
        exit 0
    fi
    if echo "$*" | grep -q "docker.sock"; then
        # Docker API readiness probe (curl --unix-socket .../_ping)
        [ "${{MOCK_DOCKER_API_NEVER_READY:-}}" = "1" ] && exit 1
        exit 0
    fi
    exit 0
fi

exit 0
""")
    mock_script.chmod(mock_script.stat().st_mode | stat.S_IEXEC)

    # Prevent the launcher from finding credentials in the host's gh config.
    fake_gh = bin_dir / "gh"
    fake_gh.write_text("#!/usr/bin/env bash\nexit 1\n")
    fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IEXEC)

    fake_curl = bin_dir / "curl"
    fake_curl.write_text(
        "#!/usr/bin/env bash\n"
        '[ "${MOCK_PRICETAG_DISCOVERY_FAIL:-}" = 1 ] && exit 1\n'
        "printf '%s\\n' \"$MOCK_PRICETAG_MODELS\"\n"
    )
    fake_curl.chmod(fake_curl.stat().st_mode | stat.S_IEXEC)

    env = isolated_env
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["MOCK_PRICETAG_MODELS"] = json.dumps(
        {
            "object": "list",
            "data": [
                {"id": "gpt-5.6-luna"},
                {"id": "gpt-5.4"},
                {"id": "gpt-5.4-mini"},
                {"id": "gpt-5.3-codex"},
            ],
        }
    )
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
def test_devbox_records_context_fingerprint_on_container(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None

    run_labels = [
        value
        for index, arg in enumerate(run_call[:-1])
        if arg == "--label"
        for value in [run_call[index + 1]]
    ]
    assert len(run_labels) == 1
    assert run_labels[0].startswith(
        "io.github.johnstrunk.my-sandbox.devbox-context-fingerprint="
    )


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
def test_devbox_does_not_forward_host_credentials(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    logged = log_file.read_text()
    for name in CREDENTIAL_ENV_VARS:
        assert f"{name}=host-{name.lower()}" not in logged


@pytest.mark.unit
def test_devbox_launcher_uses_isolated_home(
    devbox_path: Path, mock_podman_env, tmp_path: Path, isolated_home: Path
):
    # The launcher must never discover the real host's CLI configuration or
    # credential files (e.g. gh, gcloud, OpenCode state) through $HOME.
    env, log_file = mock_podman_env
    assert env["HOME"] == str(isolated_home)
    assert env["HOME"] != os.environ.get("HOME")
    assert not any(isolated_home.iterdir())

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    volumes = [run_call[i + 1] for i, arg in enumerate(run_call) if arg == "--volume"]
    volume_destinations = {volume.rsplit(":", 1)[-1] for volume in volumes}
    # Project data, cache volumes, and repo-local config are expected; no
    # host config/credential directories should be mounted.
    assert any(f"{run_dir}:/sandbox/" in v for v in volumes)
    assert {"/sandbox/.uv_cache", "/sandbox/.cache/pre-commit"} <= (volume_destinations)
    assert "/sandbox/.local/share/containers/storage" in volume_destinations
    assert any(v.endswith(":/sandbox/opencode.json") for v in volumes)
    assert not any(str(isolated_home) in volume for volume in volumes)


@pytest.mark.unit
@pytest.mark.parametrize("token_name", ["GH_TOKEN", "GITHUB_TOKEN"])
def test_devbox_github_mcp_config_from_token(
    devbox_path: Path, mock_podman_env, tmp_path: Path, token_name: str
):
    env, log_file = mock_podman_env
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    env.pop("CONTEXT7_API_KEY", None)
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
        "permission": {
            "external_directory": {
                "/tmp/**": "allow",
                "/sandbox/**": "allow",
                "/sandbox/.cache/pre-commit/**": "allow",
                "/sandbox/.*": "ask",
            },
        },
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
def test_devbox_context7_mcp_config_from_api_key(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    env.pop("CONTEXT7_API_KEY", None)
    env["CONTEXT7_API_KEY"] = "mock-context7-token"  # pragma: allowlist secret

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
    assert (
        "CONTEXT7_API_KEY=mock-context7-token" in run_call
    )  # pragma: allowlist secret

    env_values = [
        run_call[index + 1] for index, arg in enumerate(run_call[:-1]) if arg == "--env"
    ]
    config_value = next(
        value for value in env_values if value.startswith("OPENCODE_CONFIG_CONTENT=")
    )
    config = json.loads(config_value.split("=", 1)[1])
    assert config == {
        "$schema": "https://opencode.ai/config.json",
        "permission": {
            "external_directory": {
                "/tmp/**": "allow",
                "/sandbox/**": "allow",
                "/sandbox/.cache/pre-commit/**": "allow",
                "/sandbox/.*": "ask",
            },
        },
        "mcp": {
            "context7": {
                "type": "remote",
                "url": "https://mcp.context7.com/mcp",
                "headers": {
                    "Authorization": "Bearer {env:CONTEXT7_API_KEY}",
                },
                "enabled": True,
            },
        },
    }
    assert "mock-context7-token" not in config_value


@pytest.mark.unit
def test_devbox_does_not_add_github_mcp_without_credentials(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    env.pop("CONTEXT7_API_KEY", None)
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
    config_value = next(
        value for value in env_values if value.startswith("OPENCODE_CONFIG_CONTENT=")
    )
    config = json.loads(config_value.split("=", 1)[1])
    assert config == {
        "$schema": "https://opencode.ai/config.json",
        "permission": {
            "external_directory": {
                "/tmp/**": "allow",
                "/sandbox/**": "allow",
                "/sandbox/.cache/pre-commit/**": "allow",
                "/sandbox/.*": "ask",
            },
        },
    }


@pytest.mark.unit
def test_devbox_the_source_mcp_config(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    env.pop("CONTEXT7_API_KEY", None)
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
        "permission": {
            "external_directory": {
                "/tmp/**": "allow",
                "/sandbox/**": "allow",
                "/sandbox/.cache/pre-commit/**": "allow",
                "/sandbox/.*": "ask",
            },
        },
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
    env.pop("CONTEXT7_API_KEY", None)
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
@pytest.mark.parametrize(
    "anthropic_env",
    [
        {"ANTHROPIC_API_KEY": "mock-anthropic-token"},  # pragma: allowlist secret
        {"ANTHROPIC_BASE_URL": "https://anthropic.example/v1"},
        {
            "ANTHROPIC_API_KEY": "mock-anthropic-token",  # pragma: allowlist secret
            "ANTHROPIC_BASE_URL": "https://anthropic.example/v1",
        },
    ],
)
def test_devbox_anthropic_env(
    devbox_path: Path,
    mock_podman_env,
    tmp_path: Path,
    anthropic_env: dict[str, str],
):
    env, log_file = mock_podman_env
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_BASE_URL", None)
    env.update(anthropic_env)

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    for name, value in anthropic_env.items():
        assert f"{name}={value}" in run_call
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
        if name not in anthropic_env:
            assert not any(arg.startswith(f"{name}=") for arg in run_call)


@pytest.mark.unit
def test_devbox_pricetag_env_and_provider_config(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env["PRICETAG_ANTHROPIC_URL"] = "https://pricetag-anthropic.example/v1"
    env["PRICETAG_HOSTED_URL"] = "https://pricetag-hosted.example/v1"
    env["PRICETAG_OPENAI_URL"] = "https://pricetag-openai.example/v1"
    env["PRICETAG_API_KEY"] = "mock-pricetag-token"  # pragma: allowlist secret

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    assert "PRICETAG_ANTHROPIC_URL=https://pricetag-anthropic.example/v1" in run_call
    assert "PRICETAG_HOSTED_URL=https://pricetag-hosted.example/v1" in run_call
    assert "PRICETAG_OPENAI_URL=https://pricetag-openai.example/v1" in run_call
    pricetag_api_key_arg = (
        "PRICETAG_API_KEY=mock-pricetag-token"  # pragma: allowlist secret
    )
    assert pricetag_api_key_arg in run_call

    env_values = [
        run_call[index + 1] for index, arg in enumerate(run_call[:-1]) if arg == "--env"
    ]
    config_value = next(
        value for value in env_values if value.startswith("OPENCODE_CONFIG_CONTENT=")
    )
    config = json.loads(config_value.split("=", 1)[1])
    assert config["provider"] == {
        "anthropic": {
            "options": {
                "baseURL": "{env:PRICETAG_ANTHROPIC_URL}",
                "apiKey": "{env:PRICETAG_API_KEY}",
            },
        },
        "pricetag-hosted": {
            "npm": "@ai-sdk/anthropic",
            "name": "PriceTag (Hosted)",
            "options": {
                "baseURL": "{env:PRICETAG_HOSTED_URL}",
                "apiKey": "{env:PRICETAG_API_KEY}",
            },
            "models": {
                "Inferact/Qwen3.8-Flash-Next-NVFP4": {
                    "name": "Qwen 3.8 Flash Next (free)",
                },
            },
        },
        "openai": {
            "options": {
                "baseURL": "{env:PRICETAG_OPENAI_URL}",
                "apiKey": "{env:PRICETAG_API_KEY}",
            },
        },
    }
    assert "mock-pricetag-token" not in config_value


@pytest.mark.unit
def test_devbox_pricetag_builtin_provider_override_does_not_discover_models(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env["PRICETAG_OPENAI_URL"] = "https://pricetag-openai.example/v1"
    env["PRICETAG_API_KEY"] = "mock-pricetag-token"  # pragma: allowlist secret

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0
    assert "could not discover PriceTag" not in res.stderr

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
    assert config["provider"]["openai"] == {
        "options": {
            "baseURL": "{env:PRICETAG_OPENAI_URL}",
            "apiKey": "{env:PRICETAG_API_KEY}",
        }
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    ("url_name", "missing"),
    [
        ("PRICETAG_ANTHROPIC_URL", "PRICETAG_ANTHROPIC_URL"),
        ("PRICETAG_HOSTED_URL", "PRICETAG_HOSTED_URL"),
        ("PRICETAG_OPENAI_URL", "PRICETAG_OPENAI_URL"),
        ("PRICETAG_OPENAI_URL", "PRICETAG_API_KEY"),
    ],
)
def test_devbox_does_not_add_pricetag_without_both_credentials(
    devbox_path: Path,
    mock_podman_env,
    tmp_path: Path,
    url_name: str,
    missing: str,
):
    env, log_file = mock_podman_env
    env[url_name] = "https://pricetag.example/v1"
    env["PRICETAG_API_KEY"] = "mock-pricetag-token"  # pragma: allowlist secret
    env.pop(missing)

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    assert not any(arg.startswith("PRICETAG_") for arg in run_call)

    env_values = [
        run_call[index + 1] for index, arg in enumerate(run_call[:-1]) if arg == "--env"
    ]
    config_value = next(
        value for value in env_values if value.startswith("OPENCODE_CONFIG_CONTENT=")
    )
    config = json.loads(config_value.split("=", 1)[1])
    assert "provider" not in config


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
    (fake_home / ".agents").mkdir()
    expected_data_dir = fake_home / ".local" / "share" / "opencode"
    expected_data_dir.mkdir(parents=True)

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
    assert any(f"{fake_home / '.agents'}:/sandbox/.agents" in v for v in volumes)
    assert any(
        f"{expected_data_dir}:/sandbox/.local/share/opencode" in v for v in volumes
    )


@pytest.mark.unit
def test_devbox_does_not_mount_missing_global_agents_directory(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    env["HOME"] = str(fake_home)

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    volumes = [run_call[i + 1] for i, arg in enumerate(run_call) if arg == "--volume"]
    assert not any(":/sandbox/.agents" in v for v in volumes)
    assert not (fake_home / ".agents").exists()


@pytest.mark.unit
def test_devbox_opencode_data_volume_ignores_xdg_data_home(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    xdg_data_home = tmp_path / "xdg-data"
    env["HOME"] = str(fake_home)
    env["XDG_DATA_HOME"] = str(xdg_data_home)
    expected_data_dir = fake_home / ".local" / "share" / "opencode"
    expected_data_dir.mkdir(parents=True)

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    assert not xdg_data_home.exists()
    assert any(
        f"{expected_data_dir}:/sandbox/.local/share/opencode" in v for v in run_call
    )


@pytest.mark.unit
def test_devbox_persistent_cache_volumes(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    env["HOME"] = str(fake_home)
    env.pop("XDG_CACHE_HOME", None)

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    volumes = [run_call[i + 1] for i, arg in enumerate(run_call) if arg == "--volume"]

    # The knowledge base and uv/pre-commit caches are shared (not
    # per-directory) named volumes.
    assert "devbox-kb:/sandbox/kb" in volumes
    assert "devbox-uv-cache:/sandbox/.uv_cache" in volumes
    assert "devbox-precommit-cache:/sandbox/.cache/pre-commit" in volumes

    # Nested Podman/Buildah storage is host-backed so its size is easy to
    # manage, and the host directory is created ahead of time.
    expected_storage_dir = fake_home / ".cache" / "devbox" / "containers-storage"
    assert expected_storage_dir.is_dir()
    assert any(
        f"{expected_storage_dir}:/sandbox/.local/share/containers/storage" in v
        for v in volumes
    )


@pytest.mark.unit
def test_devbox_persistent_cache_volume_respects_xdg_cache_home(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    xdg_cache_home = tmp_path / "xdg-cache"
    env["HOME"] = str(fake_home)
    env["XDG_CACHE_HOME"] = str(xdg_cache_home)

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None

    expected_storage_dir = xdg_cache_home / "devbox" / "containers-storage"
    assert expected_storage_dir.is_dir()
    assert not (fake_home / ".cache").exists()
    assert any(
        f"{expected_storage_dir}:/sandbox/.local/share/containers/storage" in v
        for v in run_call
    )


def _git_config_set_calls(calls: list[list[str]], key: str) -> list[list[str]]:
    return [
        c
        for c in calls
        if c[:5] == ["exec", c[1], "git", "config", "--global"]
        and len(c) >= 6
        and c[5] == key
        and len(c) >= 7
    ]


@pytest.mark.unit
def test_devbox_configures_git_identity_from_host_config(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    host_gitconfig = tmp_path / "host_gitconfig"
    env["GIT_CONFIG_GLOBAL"] = str(host_gitconfig)
    env.pop("MOCK_CONTAINER_GIT_NAME", None)
    env.pop("MOCK_CONTAINER_GIT_EMAIL", None)
    subprocess.run(
        ["git", "config", "--global", "user.name", "Host User"],
        env=env,
        check=True,
    )
    subprocess.run(
        ["git", "config", "--global", "user.email", "host-user@example.com"],
        env=env,
        check=True,
    )

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0
    assert "not configured" not in res.stderr

    calls = parse_podman_calls(log_file)
    name_calls = _git_config_set_calls(calls, "user.name")
    email_calls = _git_config_set_calls(calls, "user.email")
    assert any(c[6] == "Host User" for c in name_calls)
    assert any(c[6] == "host-user@example.com" for c in email_calls)


@pytest.mark.unit
def test_devbox_warns_when_no_git_identity_available(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env["GIT_CONFIG_GLOBAL"] = str(tmp_path / "nonexistent_gitconfig")
    env.pop("MOCK_CONTAINER_GIT_NAME", None)
    env.pop("MOCK_CONTAINER_GIT_EMAIL", None)

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0
    assert "not configured" in res.stderr
    assert 'git config --global user.name "Your Name"' in res.stderr

    calls = parse_podman_calls(log_file)
    assert _git_config_set_calls(calls, "user.name") == []
    assert _git_config_set_calls(calls, "user.email") == []


@pytest.mark.unit
def test_devbox_preserves_existing_container_git_identity(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    host_gitconfig = tmp_path / "host_gitconfig"
    env["GIT_CONFIG_GLOBAL"] = str(host_gitconfig)
    subprocess.run(
        ["git", "config", "--global", "user.name", "Host User"],
        env=env,
        check=True,
    )
    subprocess.run(
        ["git", "config", "--global", "user.email", "host-user@example.com"],
        env=env,
        check=True,
    )
    env["MOCK_CONTAINER_GIT_NAME"] = "Container User"
    env["MOCK_CONTAINER_GIT_EMAIL"] = "container-user@example.com"

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0
    assert "not configured" not in res.stderr

    calls = parse_podman_calls(log_file)
    # The container's existing identity must never be overwritten with the
    # (different) host identity.
    assert _git_config_set_calls(calls, "user.name") == []
    assert _git_config_set_calls(calls, "user.email") == []


@pytest.mark.unit
def test_devbox_runs_gh_auth_setup_git_when_token_available(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env["GH_TOKEN"] = "mock-github-token"  # pragma: allowlist secret

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    assert any(c[:5] == ["exec", c[1], "gh", "auth", "setup-git"] for c in calls)


@pytest.mark.unit
def test_devbox_warns_when_no_github_token_for_setup_git(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0
    assert "gh auth login && gh auth setup-git" in res.stderr

    calls = parse_podman_calls(log_file)
    assert not any(c[:5] == ["exec", c[1], "gh", "auth", "setup-git"] for c in calls)


@pytest.mark.unit
def test_devbox_warns_when_gh_auth_setup_git_fails(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env["GH_TOKEN"] = "mock-github-token"  # pragma: allowlist secret
    env["MOCK_GH_AUTH_SETUP_GIT_FAILS"] = "1"

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0
    assert "'gh auth setup-git' failed" in res.stderr


@pytest.mark.unit
def test_devbox_survives_git_config_set_failure(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    # A transient failure setting the Git identity inside the container
    # (e.g. a flaky `podman exec`) must not abort the whole launch under
    # `set -euo pipefail`; it should be reported as a warning and the
    # launcher should still finish entering the container.
    env, _ = mock_podman_env
    host_gitconfig = tmp_path / "host_gitconfig"
    env["GIT_CONFIG_GLOBAL"] = str(host_gitconfig)
    subprocess.run(
        ["git", "config", "--global", "user.name", "Host User"],
        env=env,
        check=True,
    )
    subprocess.run(
        ["git", "config", "--global", "user.email", "host-user@example.com"],
        env=env,
        check=True,
    )
    env.pop("MOCK_CONTAINER_GIT_NAME", None)
    env.pop("MOCK_CONTAINER_GIT_EMAIL", None)
    env["MOCK_GIT_CONFIG_SET_FAILS"] = "1"

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0, res.stderr
    assert "Failed to set Git user.name" in res.stderr
    assert "Failed to set Git user.email" in res.stderr
    assert "Entering container" in res.stdout


@pytest.mark.unit
def test_devbox_requests_nested_bridge_sysctls(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_calls = [c for c in calls if c and c[0] == "run" and "-d" in c]
    assert len(run_calls) == 1
    run_call = run_calls[0]
    for sysctl in (
        "net.ipv4.conf.default.route_localnet=1",
        "net.ipv4.conf.default.arp_notify=1",
        "net.ipv4.conf.default.rp_filter=2",
        "net.ipv4.ip_forward=1",
        "net.ipv6.conf.default.accept_dad=0",
        "net.ipv6.conf.default.accept_ra=0",
        "net.ipv6.conf.all.forwarding=1",
    ):
        assert sysctl in run_call
    assert any(
        "containers.conf" in arg and 'netns = "bridge"' in " ".join(call)
        for call in calls
        if call and call[0] == "exec"
        for arg in call
    )
    assert any(
        "podman" in call and "--rootless-netns" in call for call in calls if call
    )
    assert any(
        "podman" in call and "network" in call and "create" in call
        for call in calls
        if call
    )
    assert any(
        "podman" in call
        and "create" in call
        and "--network" in call
        and "--publish" in call
        for call in calls
        if call
    )


@pytest.mark.unit
def test_devbox_retries_without_nested_bridge_sysctls_on_rejection(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env["MOCK_REJECT_NESTED_SYSCTLS"] = "1"
    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0, res.stderr
    assert "retrying without them" in res.stderr
    assert "user-defined bridge networks" in res.stderr

    run_calls = [
        c for c in parse_podman_calls(log_file) if c and c[0] == "run" and "-d" in c
    ]
    assert len(run_calls) == 2
    assert any("--sysctl" in c for c in run_calls[:1])
    assert not any("--sysctl" in c for c in run_calls[1:])
    assert not any(
        "containers.conf" in arg and 'netns = "bridge"' in " ".join(call)
        for call in parse_podman_calls(log_file)
        if call and call[0] == "exec"
        for arg in call
    )


@pytest.mark.unit
def test_devbox_does_not_mask_unrelated_container_create_failure(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env["MOCK_FAIL_CONTAINER_RUN"] = "1"
    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode != 0
    assert "image create failed" in res.stderr

    run_calls = [
        c for c in parse_podman_calls(log_file) if c and c[0] == "run" and "-d" in c
    ]
    assert len(run_calls) == 1


@pytest.mark.unit
def test_devbox_does_not_treat_unrelated_sysctl_error_as_fallback(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env["MOCK_FAIL_UNRELATED_SYSCTL"] = "1"
    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode != 0
    assert "storage setup failed" in res.stderr
    assert "retrying without them" not in res.stderr

    run_calls = [
        c for c in parse_podman_calls(log_file) if c and c[0] == "run" and "-d" in c
    ]
    assert len(run_calls) == 1


@pytest.mark.unit
def test_devbox_retries_without_sysctls_on_ipv6_sysctl_rejection(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env["MOCK_REJECT_IPV6_SYSCTL"] = "1"
    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0
    assert "retrying without them" in res.stderr

    run_calls = [
        c for c in parse_podman_calls(log_file) if c and c[0] == "run" and "-d" in c
    ]
    assert len(run_calls) == 2
    assert not any("--sysctl" in c for c in run_calls[1:])


@pytest.mark.unit
def test_devbox_retries_without_sysctls_on_ipv6_forwarding_rejection(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env["MOCK_REJECT_IPV6_FORWARDING"] = "1"
    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0
    assert "retrying without them" in res.stderr

    run_calls = [
        c for c in parse_podman_calls(log_file) if c and c[0] == "run" and "-d" in c
    ]
    assert len(run_calls) == 2
    assert not any("--sysctl" in c for c in run_calls[1:])


@pytest.mark.unit
def test_devbox_removes_container_when_subid_setup_fails(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env["MOCK_UID_MAP_FAIL"] = "1"
    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode != 0

    calls = parse_podman_calls(log_file)
    assert any(c[:2] == ["rm", "-f"] for c in calls)


@pytest.mark.unit
def test_devbox_reports_container_removal_failure(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, _ = mock_podman_env
    env["MOCK_CONTAINER_EXISTS"] = "1"
    env["MOCK_RM_FAIL"] = "1"
    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["--recreate", "true"], env=env, cwd=run_dir)
    assert res.returncode != 0
    assert "failed to remove container" in res.stderr


@pytest.mark.unit
def test_devbox_reports_container_exists_probe_failure(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, _ = mock_podman_env
    env["MOCK_CONTAINER_EXISTS_ERROR"] = "1"
    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["--remove"], env=env, cwd=run_dir)
    assert res.returncode != 0
    assert "could not determine whether container" in res.stderr


@pytest.mark.unit
def test_devbox_sets_docker_host_ready_marker_env(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    assert "DEVBOX_SUBID_READY_FILE=/sandbox/.devbox-subids-ready" in run_call

    exec_calls = [c for c in calls if c and c[0] == "exec"]
    assert any(
        "docker.sock" in arg and "_ping" in " ".join(c) for c in exec_calls for arg in c
    )


@pytest.mark.unit
def test_devbox_warns_when_docker_api_never_ready(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env["MOCK_DOCKER_API_NEVER_READY"] = "1"
    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir, timeout=60)
    assert res.returncode == 0, res.stderr
    assert "Docker API did not become ready" in res.stderr
