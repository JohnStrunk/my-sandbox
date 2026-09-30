"""VM OpenCode runtime config generation and pinned-schema checks."""

import json
import os
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path

import pytest

from tests.conftest import run_in_process_group


def _generated_config(repo_root: Path, env: dict[str, str]) -> tuple[dict, str]:
    result = subprocess.run(
        ["python3", str(repo_root / "lima/opencode_config.py")],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return json.loads(result.stdout), result.stdout


@pytest.mark.unit
def test_vm_config_baseline_and_no_github_mcp(
    repo_root: Path, isolated_env: dict[str, str]
):
    config, _ = _generated_config(repo_root, isolated_env)

    assert config["$schema"] == "https://opencode.ai/config.json"
    assert config["experimental"]["policies"] == [
        {"action": "provider.use", "resource": "github-copilot", "effect": "deny"},
        {"action": "provider.use", "resource": "gitlab", "effect": "deny"},
    ]
    assert {
        "action": "external_directory",
        "resource": "/root/*",
        "effect": "deny",
    } in config["permissions"]
    assert {
        "action": "external_directory",
        "resource": "/sandbox/*",
        "effect": "allow",
    } not in config["permissions"]
    assert {"action": "websearch", "resource": "*", "effect": "allow"} in config[
        "permissions"
    ]
    assert config["mcp"]["servers"] == {
        "semble": {"type": "local", "command": ["semble"], "disabled": False}
    }


@pytest.mark.unit
def test_vm_config_gates_credentials_and_serializes_references_only(
    repo_root: Path, isolated_env: dict[str, str]
):
    secrets = {
        "CONTEXT7_API_KEY": "mock-context7-secret",  # pragma: allowlist secret
        "TAVILY_API_KEY": "mock-tavily-secret",  # pragma: allowlist secret
        "IGLOO_MCP_COMMUNITY": "mock-community",
        "IGLOO_MCP_COMMUNITY_KEY": "mock-community-key",  # pragma: allowlist secret
        "IGLOO_MCP_APP_PASS": "mock-app-pass",  # pragma: allowlist secret
        "IGLOO_MCP_APP_ID": "mock-app-id",
        "IGLOO_MCP_USERNAME": "mock-user",
        "IGLOO_MCP_PASSWORD": "mock-password",  # pragma: allowlist secret
        "OCTO_OPEN_URL": "https://octo.example/v1",
        "OCTO_OPEN_KEY": "mock-octo-secret",  # pragma: allowlist secret
        "PRICETAG_ANTHROPIC_URL": "https://price-anthropic.example/v1",
        "PRICETAG_HOSTED_URL": "https://price-hosted.example/v1",
        "PRICETAG_OPENAI_URL": "https://price-openai.example/v1",
        "PRICETAG_API_KEY": "mock-pricetag-secret",  # pragma: allowlist secret
    }
    env = isolated_env | secrets

    config, serialized = _generated_config(repo_root, env)

    assert set(config["mcp"]["servers"]) == {"semble", "context7", "the-source"}
    assert config["mcp"]["servers"]["context7"]["headers"] == {
        "Authorization": "Bearer {env:CONTEXT7_API_KEY}"
    }
    source_env = config["mcp"]["servers"]["the-source"]["environment"]
    assert config["mcp"]["servers"]["the-source"]["command"] == [
        "bash",
        str(repo_root / "lima/run-the-source-mcp.sh"),
    ]
    assert source_env["IGLOO_MCP_PASSWORD"] == "{env:IGLOO_MCP_PASSWORD}"
    assert config["websearch"] == {"provider": "tavily"}

    providers = config["providers"]
    assert providers["octo-open"]["settings"] == {
        "baseURL": "{env:OCTO_OPEN_URL}",
        "apiKey": "{env:OCTO_OPEN_KEY}",
    }
    assert providers["anthropic"]["env"] == []
    assert providers["anthropic"]["settings"]["apiKey"] == "{env:PRICETAG_API_KEY}"
    assert providers["openai"]["env"] == []
    assert providers["pricetag-hosted"]["settings"]["apiKey"] == (
        "{env:PRICETAG_API_KEY}"
    )
    assert all(value not in serialized for value in secrets.values())
    assert "github" not in config["mcp"]["servers"]


@pytest.mark.unit
def test_vm_config_requires_complete_credential_groups(
    repo_root: Path, isolated_env: dict[str, str]
):
    env = isolated_env | {
        "IGLOO_MCP_COMMUNITY": "community",
        "IGLOO_MCP_PASSWORD": "partial-password",  # pragma: allowlist secret
        "OCTO_OPEN_URL": "https://octo.example/v1",
        "PRICETAG_OPENAI_URL": "https://price.example/v1",
    }

    config, serialized = _generated_config(repo_root, env)

    assert "the-source" not in config["mcp"]["servers"]
    assert "octo-open" not in config.get("providers", {})
    assert "openai" not in config.get("providers", {})
    assert "PRICETAG_API_KEY" not in serialized


@pytest.mark.unit
def test_the_source_wrapper_restricts_child_environment(
    repo_root: Path, isolated_env: dict[str, str], tmp_path: Path
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv_capture = tmp_path / "uv-capture.txt"
    mcp_capture = tmp_path / "mcp-capture.txt"

    def write_capture_script(path: Path, capture: Path) -> None:
        path.write_text(
            "#!/usr/bin/env bash\n"
            "{ env; printf '%s\\n' '--ARGS--'; "
            "if [[ $# -gt 0 ]]; then printf '<%s>\\n' \"$@\"; fi; } "
            f">{str(capture)!r}\n"
        )
        path.chmod(path.stat().st_mode | 0o111)

    uv = bin_dir / "uv"
    write_capture_script(uv, uv_capture)
    mcp_venv = (
        Path(isolated_env["HOME"]) / ".local/state/devbox-toolchain/the-source-mcp"
    )
    mcp = mcp_venv / "bin/igloo-mcp"
    mcp.parent.mkdir(parents=True)
    write_capture_script(mcp, mcp_capture)

    env = isolated_env | {
        "PATH": f"{bin_dir}:{isolated_env['PATH']}",
        "IGLOO_MCP_COMMUNITY": "mock-community",
        "IGLOO_MCP_COMMUNITY_KEY": "mock-community-key",  # pragma: allowlist secret
        "IGLOO_MCP_APP_PASS": "mock-app-pass",  # pragma: allowlist secret
        "IGLOO_MCP_APP_ID": "mock-app-id",
        "IGLOO_MCP_USERNAME": "mock-username",
        "IGLOO_MCP_PASSWORD": "mock-password",  # pragma: allowlist secret
        "IGLOO_MCP_SERVER_NAME": "The Source",
        "IGLOO_MCP_SERVER_INSTRUCTIONS": "mock instructions",
        "GH_TOKEN": "must-not-leak",  # pragma: allowlist secret
        "ANTHROPIC_API_KEY": "must-not-leak",  # pragma: allowlist secret
        "TAVILY_API_KEY": "must-not-leak",  # pragma: allowlist secret
    }

    result = run_in_process_group(
        ["bash", str(repo_root / "lima/run-the-source-mcp.sh")],
        env=env,
        cwd=repo_root,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr

    def captured(path: Path) -> tuple[dict[str, str], list[str]]:
        env_text, args_text = path.read_text().split("--ARGS--\n", maxsplit=1)
        child_env = dict(line.split("=", maxsplit=1) for line in env_text.splitlines())
        child_args = [line[1:-1] for line in args_text.splitlines()]
        return child_env, child_args

    uv_env, uv_args = captured(uv_capture)
    assert set(uv_env) == {
        "HOME",
        "PATH",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "UV_CACHE_DIR",
        "UV_PROJECT_ENVIRONMENT",
        "PWD",
        "SHLVL",
        "_",
    }
    assert uv_args == [
        "sync",
        "--project",
        str(repo_root / "lima/the-source"),
        "--locked",
    ]

    mcp_env, mcp_args = captured(mcp_capture)
    assert set(mcp_env) == {
        "HOME",
        "PATH",
        "IGLOO_MCP_COMMUNITY",
        "IGLOO_MCP_COMMUNITY_KEY",
        "IGLOO_MCP_APP_PASS",
        "IGLOO_MCP_APP_ID",
        "IGLOO_MCP_USERNAME",
        "IGLOO_MCP_PASSWORD",
        "IGLOO_MCP_SERVER_NAME",
        "IGLOO_MCP_SERVER_INSTRUCTIONS",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "PWD",
        "SHLVL",
        "_",
    }
    assert mcp_env["IGLOO_MCP_PASSWORD"] == "mock-password"  # pragma: allowlist secret
    assert mcp_env["SSL_CERT_FILE"] == "/etc/ssl/certs/ca-certificates.crt"
    assert mcp_env["REQUESTS_CA_BUNDLE"] == "/etc/ssl/certs/ca-certificates.crt"
    assert mcp_args == []
    assert "ANTHROPIC_API_KEY" not in mcp_env
    assert "GH_TOKEN" not in mcp_env


@pytest.mark.unit
def test_the_source_lock_pins_the_full_dependency_graph(repo_root: Path):
    project = (repo_root / "lima/the-source/pyproject.toml").read_text()
    lock = (repo_root / "lima/the-source/uv.lock").read_text()
    commit = "69a24ab7c7a037e5faabda16f3a2da93c1fd4bcd"  # pragma: allowlist secret

    assert f'rev = "{commit}"' in project
    assert f"?rev={commit}#{commit}" in lock
    assert 'hash = "sha256:' in lock
    assert 'source = { registry = "https://pypi.org/simple" }' in lock


@pytest.mark.unit
def test_price_tag_gateway_key_overrides_direct_provider_key(
    repo_root: Path, isolated_env: dict[str, str]
):
    env = isolated_env | {
        "ANTHROPIC_API_KEY": "direct-anthropic-secret",  # pragma: allowlist secret
        "OPENAI_API_KEY": "direct-openai-secret",  # pragma: allowlist secret
        "PRICETAG_API_KEY": "gateway-secret",  # pragma: allowlist secret
        "PRICETAG_ANTHROPIC_URL": "https://price-anthropic.example/v1",
        "PRICETAG_OPENAI_URL": "https://price-openai.example/v1",
    }

    config, serialized = _generated_config(repo_root, env)

    assert config["providers"]["anthropic"]["env"] == []
    assert config["providers"]["openai"]["env"] == []
    assert all(
        value not in serialized
        for value in (
            "direct-anthropic-secret",
            "direct-openai-secret",
            "gateway-secret",
        )
    )


@pytest.mark.unit
def test_installed_vm_opencode_accepts_generated_config_schema(
    repo_root: Path, isolated_env: dict[str, str], tmp_path: Path
):
    """Run against the VM's pinned OpenCode, not the host-global config."""
    manifest = Path("/etc/devbox/tool-versions.json")
    opencode = shutil.which("opencode", path=isolated_env["PATH"])
    if not manifest.is_file() or opencode is None:
        pytest.skip("requires the provisioned Lima VM OpenCode installation")

    expected_version = json.loads(manifest.read_text())["tools"]["opencode"]["version"]
    version = subprocess.run(
        [opencode, "--version"], check=True, capture_output=True, text=True
    ).stdout.strip()
    assert expected_version in version

    schema_env = isolated_env | {
        "CONTEXT7_API_KEY": "schema-context7-key",  # pragma: allowlist secret
        "TAVILY_API_KEY": "schema-tavily-key",  # pragma: allowlist secret
        "IGLOO_MCP_COMMUNITY": "schema-community",
        "IGLOO_MCP_COMMUNITY_KEY": "schema-community-key",  # pragma: allowlist secret
        "IGLOO_MCP_APP_PASS": "schema-app-pass",  # pragma: allowlist secret
        "IGLOO_MCP_APP_ID": "schema-app-id",
        "IGLOO_MCP_USERNAME": "schema-user",
        "IGLOO_MCP_PASSWORD": "schema-password",  # pragma: allowlist secret
        "OCTO_OPEN_URL": "https://octo.example/v1",
        "OCTO_OPEN_KEY": "schema-octo-key",  # pragma: allowlist secret
        "PRICETAG_ANTHROPIC_URL": "https://price-anthropic.example/v1",
        "PRICETAG_HOSTED_URL": "https://price-hosted.example/v1",
        "PRICETAG_OPENAI_URL": "https://price-openai.example/v1",
        "PRICETAG_API_KEY": "schema-pricetag-key",  # pragma: allowlist secret
    }
    config, _ = _generated_config(repo_root, schema_env)
    # debug config is a schema/parser probe, not an MCP behavior test. Disable
    # configured servers so the CLI cannot start their external processes.
    for server_config in config["mcp"]["servers"].values():
        server_config["disabled"] = True
    home = tmp_path / "home"
    config_home = home / ".config"
    data_home = home / ".local/share"
    state_home = home / ".local/state"
    for path in (config_home, data_home, state_home):
        path.mkdir(parents=True)
    env = {
        name: value
        for name, value in isolated_env.items()
        if name in {"PATH", "LANG", "LC_ALL", "TERM"}
    }
    env.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(config_home),
            "XDG_DATA_HOME": str(data_home),
            "XDG_STATE_HOME": str(state_home),
            "OPENCODE_CONFIG_CONTENT": json.dumps(config),
            "OPENCODE_SERVER_PASSWORD": (
                "schema-test-password"  # pragma: allowlist secret
            ),
        }
    )

    state_dir = state_home / "opencode"
    state_dir.mkdir(parents=True)
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
    server = subprocess.Popen(
        [
            opencode,
            "serve",
            "--service",
            "--hostname",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if server.poll() is not None:
                stdout, stderr = server.communicate()
                pytest.fail(f"OpenCode schema server exited early:\n{stdout}\n{stderr}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("OpenCode schema server did not become ready")

        registration = state_dir / "service-local.json"
        registration.write_text(
            json.dumps(
                {
                    "version": expected_version,
                    "url": f"http://127.0.0.1:{port}",
                    "pid": server.pid,
                    "password": env["OPENCODE_SERVER_PASSWORD"],
                }
            )
        )
        result = run_in_process_group(
            [opencode, "debug", "config"], env=env, timeout=30
        )
    finally:
        if server.poll() is None:
            os.killpg(server.pid, signal.SIGTERM)
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(server.pid, signal.SIGKILL)
                server.wait(timeout=5)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    sources = json.loads(result.stdout)
    generated = next(
        source["info"]
        for source in sources
        if source.get("info", {}).get("experimental", {}).get("policies")
    )
    assert generated["experimental"]["policies"] == config["experimental"]["policies"]
    assert generated["mcp"]["servers"]["semble"]["command"] == ["semble"]
    assert set(generated["mcp"]["servers"]) == {
        "semble",
        "context7",
        "the-source",
    }
    assert set(generated["providers"]) == {
        "octo-open",
        "anthropic",
        "pricetag-hosted",
        "openai",
    }
    assert "github" not in generated["mcp"]["servers"]
