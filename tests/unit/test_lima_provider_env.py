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
    "OPENAI_API_KEY",
    "OCTO_OPEN_URL",
    "OCTO_OPEN_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "PRICETAG_ANTHROPIC_URL",
    "PRICETAG_HOSTED_URL",
    "PRICETAG_OPENAI_URL",
    "PRICETAG_API_KEY",
    "GOOGLE_CLOUD_PROJECT",
    "VERTEX_LOCATION",
}


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
        name: os.environ[name] for name in names if name in os.environ
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
            "PRICETAG_API_KEY": "mock-pricetag-token",  # pragma: allowlist secret
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
        "OCTO_OPEN_URL": "https://octo.example/v1",
        "OCTO_OPEN_KEY": "mock-octo-key",  # pragma: allowlist secret
        "PRICETAG_API_KEY": "mock-pricetag-key",  # pragma: allowlist secret
        "PRICETAG_ANTHROPIC_URL": "https://pricetag.example/anthropic",
        "PRICETAG_HOSTED_URL": "https://pricetag.example/hosted",
        "PRICETAG_OPENAI_URL": "https://pricetag.example/openai",
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
