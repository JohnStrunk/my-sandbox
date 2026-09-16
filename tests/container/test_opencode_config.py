import json
import subprocess

import pytest

from tests.conftest import run_in_devbox


@pytest.mark.container
def test_ripwire_skills_are_staged_and_active(devbox_image: str):
    for skill_path in (
        "/usr/local/share/ripwire/skills/ripwire-orient/SKILL.md",
        "/sandbox/.agents/skills/ripwire-orient/SKILL.md",
    ):
        res = run_in_devbox(
            devbox_image,
            ["test", "-f", skill_path],
            user="sandbox",
        )
        assert res.returncode == 0, (
            f"Ripwire skill missing at {skill_path}.\n"
            f"Stdout: {res.stdout}\nStderr: {res.stderr}"
        )


@pytest.mark.container
def test_ast_grep_skills_are_staged_and_active(devbox_image: str):
    for skill_path in (
        "/usr/local/share/ast-grep/skills/ast-grep/SKILL.md",
        "/sandbox/.agents/skills/ast-grep/SKILL.md",
        "/sandbox/.agents/skills/ast-grep-outline/SKILL.md",
    ):
        res = run_in_devbox(
            devbox_image,
            ["test", "-f", skill_path],
            user="sandbox",
        )
        assert res.returncode == 0, (
            f"ast-grep skill missing at {skill_path}.\n"
            f"Stdout: {res.stdout}\nStderr: {res.stderr}"
        )

    res = run_in_devbox(
        devbox_image,
        ["grep", "-q", "^name: ast-grep$", "/sandbox/.agents/skills/ast-grep/SKILL.md"],
        user="sandbox",
    )
    assert res.returncode == 0, (
        "The active ast-grep skill does not have the expected OpenCode skill name.\n"
        f"Stdout: {res.stdout}\nStderr: {res.stderr}"
    )


@pytest.mark.container
def test_devbox_tools_skill_is_staged_and_active(devbox_image: str):
    for skill_path in (
        "/usr/local/share/devbox/skills/devbox-tools/SKILL.md",
        "/sandbox/.agents/skills/devbox-tools/SKILL.md",
    ):
        res = run_in_devbox(
            devbox_image,
            ["test", "-f", skill_path],
            user="sandbox",
        )
        assert res.returncode == 0, (
            f"devbox-tools skill missing at {skill_path}.\n"
            f"Stdout: {res.stdout}\nStderr: {res.stderr}"
        )

    res = run_in_devbox(
        devbox_image,
        [
            "grep",
            "-q",
            '^name: "devbox-tools"$',
            "/sandbox/.agents/skills/devbox-tools/SKILL.md",
        ],
        user="sandbox",
    )
    assert res.returncode == 0, (
        "The active devbox-tools skill does not have the expected OpenCode "
        f"skill name.\nStdout: {res.stdout}\nStderr: {res.stderr}"
    )

    res = run_in_devbox(
        devbox_image,
        [
            "grep",
            "-q",
            "devbox-go",
            "/sandbox/.agents/skills/devbox-tools/SKILL.md",
        ],
        user="sandbox",
    )
    assert res.returncode == 0, (
        "Project-aware Go guidance is not available to a fresh OpenCode session.\n"
        f"Stdout: {res.stdout}\nStderr: {res.stderr}"
    )

    res = run_in_devbox(
        devbox_image,
        [
            "grep",
            "-Eq",
            "^### GitHub search$|perPage|search_issues",
            "/sandbox/.agents/skills/devbox-tools/SKILL.md",
        ],
        user="sandbox",
    )
    assert res.returncode == 0, (
        "GitHub search field and pagination guidance is not available to a fresh "
        f"OpenCode session.\nStdout: {res.stdout}\nStderr: {res.stderr}"
    )


@pytest.mark.container
def test_repomix_guidance_is_staged_and_active(devbox_image: str):
    res = run_in_devbox(
        devbox_image,
        [
            "bash",
            "-ceu",
            r"""
set -eu
source=/usr/local/share/devbox/skills/devbox-tools/SKILL.md
active=/sandbox/.agents/skills/devbox-tools/SKILL.md
test "$(sha256sum "$source" | cut -d ' ' -f1)" = \
  "$(sha256sum "$active" | cut -d ' ' -f1)"
awk '
/^### Repomix$/ { repomix=1 }
/--token-budget/ { budget=1 }
/--no-security-check/ { security=1 }
END { exit !(repomix && budget && security) }
' "$source"
""",
        ],
        user="sandbox",
    )
    assert res.returncode == 0, (
        "Repomix guidance is not available to a fresh OpenCode session.\n"
        f"Stdout: {res.stdout}\nStderr: {res.stderr}"
    )


@pytest.mark.container
def test_fresh_opencode_session_discovers_semble(devbox_image: str):
    config = json.dumps(
        {
            "mcp": {
                "semble": {
                    "type": "local",
                    "command": ["semble"],
                    "enabled": True,
                }
            }
        }
    )
    res = subprocess.run(
        [
            "podman",
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            "sandbox",
            "--env",
            f"OPENCODE_CONFIG_CONTENT={config}",
            devbox_image,
            "opencode",
            "mcp",
            "list",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    output = f"{res.stdout}\n{res.stderr}"
    assert res.returncode == 0, (
        f"A fresh OpenCode session could not connect to Semble.\n{output}"
    )
    assert "semble" in output
    assert "connected" in output


@pytest.mark.container
def test_fresh_opencode_session_discovers_github_mcp(devbox_image: str):
    config = json.dumps(
        {
            "mcp": {
                "github": {
                    "type": "local",
                    "command": ["github-mcp-server-proxy"],
                    "enabled": True,
                    "environment": {
                        "GITHUB_PERSONAL_ACCESS_TOKEN": (
                            "{env:GITHUB_PERSONAL_ACCESS_TOKEN}"
                        ),
                        "GITHUB_TOOLSETS": "context,repos,issues,pull_requests,users",
                    },
                }
            }
        }
    )
    res = subprocess.run(
        [
            "podman",
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            "sandbox",
            "--env",
            "GITHUB_PERSONAL_ACCESS_TOKEN="
            "mock-github-token",  # pragma: allowlist secret
            "--env",
            f"OPENCODE_CONFIG_CONTENT={config}",
            devbox_image,
            "opencode",
            "mcp",
            "list",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    output = f"{res.stdout}\n{res.stderr}"
    assert res.returncode == 0, (
        f"A fresh OpenCode session could not connect to the local GitHub MCP proxy.\n"
        f"{output}"
    )
    assert "github" in output
    assert "connected" in output
