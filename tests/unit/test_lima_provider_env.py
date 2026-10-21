"""Tests for provider environment passthrough into the Lima devbox."""

import json
import re
import stat
from pathlib import Path

import pytest

from tests.conftest import run_bash_script

_EXPECTED_GUEST_ENV_NAMES = {
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "GEMINI_API_KEY",
    "GOOGLE_GENERATIVE_AI_API_KEY",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "CONTEXT7_API_KEY",
    "TAVILY_API_KEY",
    "IGLOO_MCP_COMMUNITY",
    "IGLOO_MCP_COMMUNITY_KEY",
    "IGLOO_MCP_APP_PASS",
    "IGLOO_MCP_APP_ID",
    "IGLOO_MCP_USERNAME",
    "IGLOO_MCP_PASSWORD",
    "GITLAB_HOST",
    "GITLAB_TOKEN",
    "LITEMAAS_API_KEY",
    "ENMAAS_URL",
    "ENMAAS_API_KEY",
    "MODELSCORP_URL",
    "MODELSCORP_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "GOOGLE_CLOUD_PROJECT",
    "VERTEX_LOCATION",
}

_PROVIDER_ENV_PAIRS = (
    ("ENMAAS_URL", "ENMAAS_API_KEY"),
    ("MODELSCORP_URL", "MODELSCORP_API_KEY"),
)


def _lima_provider_env_names(repo_root: Path) -> set[str]:
    shell = (repo_root / "lima" / "devbox-shell").read_text()
    match = re.search(r"^DEVBOX_ENV_VARS=\(\n(.*?)^\)", shell, re.DOTALL | re.MULTILINE)
    assert match, "Lima provider environment allowlist not found"
    names = re.findall(r"^\s+([A-Z][A-Z0-9_]*)\s*$", match.group(1), re.MULTILINE)
    assert len(names) == len(set(names)), "duplicate names in Lima provider allowlist"
    return set(names)


def _install_lima_mocks(tmp_path: Path) -> tuple[Path, Path, Path]:
    bin_dir = tmp_path / "mock-bin"
    bin_dir.mkdir()
    capture_file = tmp_path / "limactl-call.json"
    calls_file = tmp_path / "limactl-calls.log"

    limactl = bin_dir / "limactl"
    limactl.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == list ]]; then
  printf '%s\\n' "${MOCK_LIMA_STATUS:-Running}"
  exit 0
fi
if [[ "${1:-}" == start ]]; then
  printf '%s\\n' "$*" >>"$MOCK_LIMACTL_CALLS"
  exit 0
fi
python3 - "$MOCK_LIMACTL_CAPTURE" "$@" <<'PY'
import json
import os
import sys

names = os.environ["LIMA_SHELLENV_ALLOW"].split(",")
payload = {
    "args": sys.argv[2:],
    "block": os.environ.get("LIMA_SHELLENV_BLOCK"),
    "allow": names,
    "provider_env": {
        name: os.environ[name]
        for name in names
        if name in os.environ
        and name not in {"ENMAAS_API_KEY", "MODELSCORP_API_KEY"}
    },
    "api_key_status": {
        name: {
            "present": name in os.environ,
            "value_matches": (
                f"MOCK_EXPECTED_{name}" in os.environ
                and os.environ.get(name) == os.environ[f"MOCK_EXPECTED_{name}"]
            ),
        }
        for name in ("ENMAAS_API_KEY", "MODELSCORP_API_KEY")
    },
}
with open(sys.argv[1], "w") as output:
    json.dump(payload, output)
PY
"""
    )
    limactl.chmod(limactl.stat().st_mode | stat.S_IEXEC)

    gh = bin_dir / "gh"
    gh.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == auth && "${2:-}" == token ]]; then
  printf '%s\\n' "${MOCK_GH_AUTH_TOKEN:-}"
fi
"""
    )
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)

    return bin_dir, capture_file, calls_file


def _run_lima_shell(repo_root: Path, env: dict[str, str]) -> dict:
    result = run_bash_script(
        repo_root / "lima" / "devbox-shell",
        env=env,
        cwd=repo_root,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(Path(env["MOCK_LIMACTL_CAPTURE"]).read_text())


@pytest.mark.unit
def test_lima_shell_has_the_expected_guest_environment_allowlist(repo_root: Path):
    assert _lima_provider_env_names(repo_root) == _EXPECTED_GUEST_ENV_NAMES


@pytest.mark.unit
def test_lima_shell_forwards_provider_env_with_matching_aliases_and_gates(
    repo_root: Path,
    isolated_env: dict[str, str],
    tmp_path: Path,
):
    bin_dir, capture_file, calls_file = _install_lima_mocks(tmp_path)
    env = isolated_env.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["MOCK_LIMACTL_CAPTURE"] = str(capture_file)
    env["MOCK_LIMACTL_CALLS"] = str(calls_file)
    env.update(
        {
            "GEMINI_API_KEY": "mock-gemini-token",  # pragma: allowlist secret
            "GOOGLE_GENERATIVE_AI_API_KEY": "wrong-alias",  # pragma: allowlist secret
            "GITHUB_TOKEN": "mock-github-token",  # pragma: allowlist secret
            "GITLAB_TOKEN": "mock-gitlab-token",  # pragma: allowlist secret
            "ANTHROPIC_BASE_URL": "https://anthropic.example/v1",
            # Incomplete groups are not forwarded to the VM.
            "IGLOO_MCP_COMMUNITY": "partial-community",
            "GOOGLE_CLOUD_PROJECT": "project-without-location",
            # Listed by the test credential scrubber, but not passed by devbox.
            "OPENROUTER_API_KEY": "not-forwarded",  # pragma: allowlist secret
        }
    )

    payload = _run_lima_shell(repo_root, env)

    assert payload["args"] == ["shell", "--preserve-env", "devbox"]
    assert payload["block"] == "*"
    assert set(payload["allow"]) == _EXPECTED_GUEST_ENV_NAMES
    assert payload["provider_env"] == {
        "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt",
        "REQUESTS_CA_BUNDLE": "/etc/ssl/certs/ca-certificates.crt",
        "NODE_EXTRA_CA_CERTS": "/etc/ssl/certs/ca-certificates.crt",
        "GEMINI_API_KEY": "mock-gemini-token",  # pragma: allowlist secret
        "GOOGLE_GENERATIVE_AI_API_KEY": "mock-gemini-token",  # pragma: allowlist secret
        "GH_TOKEN": "mock-github-token",  # pragma: allowlist secret
        "GITHUB_TOKEN": "mock-github-token",  # pragma: allowlist secret
        "GITLAB_HOST": "gitlab.com",
        "GITLAB_TOKEN": "mock-gitlab-token",  # pragma: allowlist secret
        "ANTHROPIC_BASE_URL": "https://anthropic.example/v1",
    }


@pytest.mark.unit
def test_lima_shell_uses_gh_auth_token_fallback(
    repo_root: Path,
    isolated_env: dict[str, str],
    tmp_path: Path,
):
    bin_dir, capture_file, calls_file = _install_lima_mocks(tmp_path)
    env = isolated_env.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["MOCK_LIMACTL_CAPTURE"] = str(capture_file)
    env["MOCK_LIMACTL_CALLS"] = str(calls_file)
    env["MOCK_GH_AUTH_TOKEN"] = "mock-gh-auth-token"  # pragma: allowlist secret

    payload = _run_lima_shell(repo_root, env)

    assert payload["provider_env"] == {
        "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt",
        "REQUESTS_CA_BUNDLE": "/etc/ssl/certs/ca-certificates.crt",
        "NODE_EXTRA_CA_CERTS": "/etc/ssl/certs/ca-certificates.crt",
        "GH_TOKEN": "mock-gh-auth-token",
        "GITHUB_TOKEN": "mock-gh-auth-token",
    }


@pytest.mark.unit
def test_lima_shell_forwards_complete_provider_credential_groups(
    repo_root: Path,
    isolated_env: dict[str, str],
    tmp_path: Path,
):
    bin_dir, capture_file, calls_file = _install_lima_mocks(tmp_path)
    env = isolated_env.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["MOCK_LIMACTL_CAPTURE"] = str(capture_file)
    env["MOCK_LIMACTL_CALLS"] = str(calls_file)
    credentials = {
        "GEMINI_API_KEY": "mock-gemini-token",  # pragma: allowlist secret
        "GH_TOKEN": "mock-github-token",  # pragma: allowlist secret
        "CONTEXT7_API_KEY": "mock-context7-token",  # pragma: allowlist secret
        "TAVILY_API_KEY": "mock-tavily-token",  # pragma: allowlist secret
        "IGLOO_MCP_COMMUNITY": "mock-community",
        "IGLOO_MCP_COMMUNITY_KEY": "mock-community-key",  # pragma: allowlist secret
        "IGLOO_MCP_APP_PASS": "mock-app-pass",  # pragma: allowlist secret
        "IGLOO_MCP_APP_ID": "mock-app-id",
        "IGLOO_MCP_USERNAME": "mock-username",
        "IGLOO_MCP_PASSWORD": "mock-password",  # pragma: allowlist secret
        "ANTHROPIC_API_KEY": "mock-anthropic-key",  # pragma: allowlist secret
        "ANTHROPIC_BASE_URL": "https://anthropic.example/v1",
        "GOOGLE_CLOUD_PROJECT": "mock-project",
        "VERTEX_LOCATION": "us-central1",
        "AWS_SECRET_ACCESS_KEY": "must-not-forward",  # pragma: allowlist secret
    }
    env.update(credentials)

    payload = _run_lima_shell(repo_root, env)

    forwarded = payload["provider_env"]
    for name, value in credentials.items():
        if name == "AWS_SECRET_ACCESS_KEY":
            assert name not in forwarded
        else:
            assert forwarded[name] == value
    assert forwarded["GOOGLE_GENERATIVE_AI_API_KEY"] == credentials["GEMINI_API_KEY"]
    assert forwarded["GITHUB_TOKEN"] == credentials["GH_TOKEN"]
    assert forwarded["SSL_CERT_FILE"] == "/etc/ssl/certs/ca-certificates.crt"
    assert forwarded["REQUESTS_CA_BUNDLE"] == "/etc/ssl/certs/ca-certificates.crt"
    assert forwarded["NODE_EXTRA_CA_CERTS"] == "/etc/ssl/certs/ca-certificates.crt"


@pytest.mark.unit
@pytest.mark.parametrize(("url_name", "api_key_name"), _PROVIDER_ENV_PAIRS)
def test_lima_shell_forwards_complete_provider_pair_without_logging_api_key(
    url_name: str,
    api_key_name: str,
    repo_root: Path,
    isolated_env: dict[str, str],
    tmp_path: Path,
):
    bin_dir, capture_file, calls_file = _install_lima_mocks(tmp_path)
    env = isolated_env.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["MOCK_LIMACTL_CAPTURE"] = str(capture_file)
    env["MOCK_LIMACTL_CALLS"] = str(calls_file)
    provider = api_key_name.removesuffix("_API_KEY").lower()
    provider_url = f"https://{provider}.example/v1"
    api_key = f"{provider}-test-secret-sentinel"  # pragma: allowlist secret
    env[url_name] = provider_url
    env[api_key_name] = api_key
    env[f"MOCK_EXPECTED_{api_key_name}"] = api_key
    direct_provider_secrets = {
        "OPENAI_API_KEY": "direct-openai-secret",  # pragma: allowlist secret
        "ANTHROPIC_API_KEY": "direct-anthropic-secret",  # pragma: allowlist secret
        "ANTHROPIC_BASE_URL": "https://direct-anthropic.example/v1",
    }
    env.update(direct_provider_secrets)
    env["MOCK_LIMA_STATUS"] = "Stopped"

    result = run_bash_script(
        repo_root / "lima" / "devbox-shell",
        args=["opencode", "run", "test prompt"],
        env=env,
        cwd=repo_root,
    )

    if result.returncode != 0:
        pytest.fail("Lima shell failed with a complete provider environment pair")
    mock_calls = calls_file.read_text()
    captured_call = capture_file.read_text()
    if any(
        api_key in output
        for output in (result.stdout, result.stderr, mock_calls, captured_call)
    ):
        pytest.fail("Provider API key appeared in output or captured test data")
    payload = json.loads(captured_call)
    forwarded = payload["provider_env"]
    assert payload["args"][-3:] == ["opencode", "run", "test prompt"]
    if api_key in " ".join(payload["args"]):
        pytest.fail("Provider API key appeared in Lima command arguments")
    assert api_key_name not in forwarded
    if forwarded.get(url_name) != provider_url:
        pytest.fail("Provider endpoint was not forwarded")

    if url_name == "ENMAAS_URL":
        for name, value in direct_provider_secrets.items():
            assert name not in forwarded
            if any(
                value in output
                for output in (result.stdout, result.stderr, mock_calls, captured_call)
            ):
                pytest.fail("Direct provider credentials appeared in launcher output")
    else:
        for name, value in direct_provider_secrets.items():
            assert forwarded[name] == value
    key_status = payload["api_key_status"][api_key_name]
    if not key_status["present"] or not key_status["value_matches"]:
        pytest.fail("Provider API key was not forwarded")


@pytest.mark.unit
@pytest.mark.parametrize(("url_name", "api_key_name"), _PROVIDER_ENV_PAIRS)
@pytest.mark.parametrize(
    "url", ["http://provider.example/v1", "ftp://provider.example/v1"]
)
def test_lima_shell_refuses_non_https_provider_pair_without_logging_api_key(
    url: str,
    url_name: str,
    api_key_name: str,
    repo_root: Path,
    isolated_env: dict[str, str],
    tmp_path: Path,
):
    bin_dir, capture_file, calls_file = _install_lima_mocks(tmp_path)
    env = isolated_env.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["MOCK_LIMACTL_CAPTURE"] = str(capture_file)
    env["MOCK_LIMACTL_CALLS"] = str(calls_file)
    provider = api_key_name.removesuffix("_API_KEY").lower()
    api_key = f"{provider}-test-secret-sentinel"  # pragma: allowlist secret
    env[url_name] = url
    env[api_key_name] = api_key
    env[f"MOCK_EXPECTED_{api_key_name}"] = api_key
    env["MOCK_LIMA_STATUS"] = "Stopped"

    result = run_bash_script(
        repo_root / "lima" / "devbox-shell",
        env=env,
        cwd=repo_root,
    )

    assert result.returncode != 0
    assert not capture_file.exists()
    if api_key in result.stdout or api_key in result.stderr:
        pytest.fail("Provider API key appeared in launcher output")
    if calls_file.exists() and api_key in calls_file.read_text():
        pytest.fail("Provider API key appeared in Lima command logs")
    assert f"{url_name} must use HTTPS" in result.stderr


@pytest.mark.unit
@pytest.mark.parametrize(("url_name", "api_key_name"), _PROVIDER_ENV_PAIRS)
@pytest.mark.parametrize("present_part", ["url", "api_key"])
def test_lima_shell_omits_incomplete_provider_pair_without_logging_api_key(
    url_name: str,
    api_key_name: str,
    present_part: str,
    repo_root: Path,
    isolated_env: dict[str, str],
    tmp_path: Path,
):
    bin_dir, capture_file, calls_file = _install_lima_mocks(tmp_path)
    env = isolated_env.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["MOCK_LIMACTL_CAPTURE"] = str(capture_file)
    env["MOCK_LIMACTL_CALLS"] = str(calls_file)
    provider = api_key_name.removesuffix("_API_KEY").lower()
    api_key = f"{provider}-test-secret-sentinel"  # pragma: allowlist secret
    if present_part == "url":
        env[url_name] = f"https://{provider}.example/v1"
    else:
        env[api_key_name] = api_key
        env[f"MOCK_EXPECTED_{api_key_name}"] = api_key
    env["MOCK_LIMA_STATUS"] = "Stopped"

    result = run_bash_script(
        repo_root / "lima" / "devbox-shell",
        env=env,
        cwd=repo_root,
    )

    if result.returncode != 0:
        pytest.fail("Lima shell failed while testing an incomplete provider pair")
    mock_calls = calls_file.read_text()
    if any(api_key in output for output in (result.stdout, result.stderr, mock_calls)):
        pytest.fail("Provider API key appeared in launcher output or command logs")

    captured_call = capture_file.read_text()
    if api_key in captured_call:
        pytest.fail("Provider API key appeared in the mock capture")
    payload = json.loads(captured_call)
    forwarded = payload["provider_env"]
    if url_name in forwarded or api_key_name in forwarded:
        pytest.fail("Incomplete provider pair was forwarded")
    key_status = payload["api_key_status"][api_key_name]
    if key_status["present"] or key_status["value_matches"]:
        pytest.fail("Incomplete provider API key was forwarded")


@pytest.mark.unit
@pytest.mark.parametrize(("url_name", "api_key_name"), _PROVIDER_ENV_PAIRS)
@pytest.mark.parametrize("empty_part", ["url", "api_key"])
def test_lima_shell_omits_provider_pair_with_empty_value(
    url_name: str,
    api_key_name: str,
    empty_part: str,
    repo_root: Path,
    isolated_env: dict[str, str],
    tmp_path: Path,
):
    bin_dir, capture_file, calls_file = _install_lima_mocks(tmp_path)
    env = isolated_env.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["MOCK_LIMACTL_CAPTURE"] = str(capture_file)
    env["MOCK_LIMACTL_CALLS"] = str(calls_file)
    provider = api_key_name.removesuffix("_API_KEY").lower()
    api_key = f"{provider}-test-secret-sentinel"  # pragma: allowlist secret
    env[f"MOCK_EXPECTED_{api_key_name}"] = api_key
    env[url_name] = f"https://{provider}.example/v1"
    env[api_key_name] = api_key
    empty_name = url_name if empty_part == "url" else api_key_name
    env[empty_name] = ""
    env["MOCK_LIMA_STATUS"] = "Stopped"

    result = run_bash_script(
        repo_root / "lima" / "devbox-shell",
        env=env,
        cwd=repo_root,
    )

    if result.returncode != 0:
        pytest.fail("Lima shell failed while testing an empty provider pair value")
    mock_calls = calls_file.read_text()
    captured_call = capture_file.read_text()
    if any(
        api_key in output
        for output in (result.stdout, result.stderr, mock_calls, captured_call)
    ):
        pytest.fail("Provider API key appeared in output or captured test data")

    payload = json.loads(captured_call)
    forwarded = payload["provider_env"]
    if url_name in forwarded or api_key_name in forwarded:
        pytest.fail("Empty provider credential value was forwarded")
    key_status = payload["api_key_status"][api_key_name]
    if key_status["present"] or key_status["value_matches"]:
        pytest.fail("Empty provider API key was forwarded")


@pytest.mark.unit
def test_direct_lima_shell_starts_stopped_vm_under_lifecycle_lock(
    repo_root: Path,
    isolated_env: dict[str, str],
    tmp_path: Path,
):
    bin_dir, capture_file, calls_file = _install_lima_mocks(tmp_path)
    env = isolated_env.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["MOCK_LIMACTL_CAPTURE"] = str(capture_file)
    env["MOCK_LIMACTL_CALLS"] = str(calls_file)
    env["MOCK_LIMA_STATUS"] = "Stopped"

    result = run_bash_script(
        repo_root / "lima" / "devbox-shell",
        env=env,
        cwd=repo_root,
    )

    assert result.returncode == 0, result.stderr
    assert calls_file.read_text().splitlines() == ["start devbox"]
    payload = json.loads(capture_file.read_text())
    assert payload["args"] == ["shell", "--preserve-env", "devbox"]
    lock_file = Path(env["XDG_CACHE_HOME"]) / "devbox/locks/lima-runtime.lock"
    assert lock_file.exists()
    assert lock_file.stat().st_mode & 0o777 == 0o600


@pytest.mark.unit
def test_lima_provision_does_not_start_opencode_without_provider_env(
    repo_root: Path,
):
    user_script = (repo_root / "lima" / "provision-user.sh").read_text()

    assert not re.search(
        r"^\s*(?:timeout\s+\S+\s+)?/usr/local/bin/opencode\b",
        user_script,
        re.MULTILINE,
    )
