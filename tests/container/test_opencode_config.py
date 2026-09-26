import json
import subprocess

import pytest

from tests.conftest import run_in_devbox


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
def test_file_guidance_is_staged_and_active(devbox_image: str):
    res = run_in_devbox(
        devbox_image,
        [
            "bash",
            "-ceu",
            r"""
source=/usr/local/share/devbox/skills/devbox-tools/SKILL.md
active=/sandbox/.agents/skills/devbox-tools/SKILL.md
test "$(sha256sum "$source" | cut -d ' ' -f1)" = \
  "$(sha256sum "$active" | cut -d ' ' -f1)"
awk '
/^### Release artifact inspection$/ { in_section=1; next }
/^### / { in_section=0 }
in_section && /file <artifact>/ { command=1 }
in_section && /readelf -h <artifact>/ { fallback=1 }
in_section && /devbox --recreate/ { recreate=1 }
END { exit !(command && fallback && recreate) }
' "$active"
""",
        ],
        user="sandbox",
    )
    assert res.returncode == 0, (
        "Release artifact guidance is not available to a fresh OpenCode session.\n"
        f"Stdout: {res.stdout}\nStderr: {res.stderr}"
    )


@pytest.mark.container
def test_diff_guidance_is_staged_and_active(devbox_image: str):
    res = run_in_devbox(
        devbox_image,
        [
            "bash",
            "-ceu",
            r"""
source=/usr/local/share/devbox/skills/devbox-tools/SKILL.md
active=/sandbox/.agents/skills/devbox-tools/SKILL.md
test "$(sha256sum "$source" | cut -d ' ' -f1)" = \
  "$(sha256sum "$active" | cut -d ' ' -f1)"
awk '
/^### Classic diff and patch$/ { in_section=1; next }
/^### / { in_section=0 }
in_section && /diff -u/ { compare=1 }
in_section && /exits/ { exit_status=1 }
in_section && /patch target\.txt/ { apply=1 }
in_section && /already applied/ { new_file=1 }
in_section && /difft/ { fallback=1 }
in_section && /devbox --recreate/ { recreate=1 }
END { exit !(compare && exit_status && apply && new_file && fallback && recreate) }
' "$active"
""",
        ],
        user="sandbox",
    )
    assert res.returncode == 0, (
        "Classic diff and patch guidance is not available to a fresh OpenCode "
        f"session.\nStdout: {res.stdout}\nStderr: {res.stderr}"
    )


@pytest.mark.container
def test_fresh_opencode_session_loads_semble_config(devbox_image: str):
    config = json.dumps(
        {
            "$schema": "https://opencode.ai/config.json",
            "mcp": {
                "servers": {
                    "semble": {
                        "type": "local",
                        "command": ["semble"],
                        "disabled": False,
                    },
                }
            },
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
            "debug",
            "config",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    output = f"{res.stdout}\n{res.stderr}"
    assert res.returncode == 0, f"OpenCode could not load the Semble config.\n{output}"
    sources = json.loads(res.stdout)
    assert any(
        source.get("info", {}).get("mcp", {}).get("servers", {}).get("semble")
        for source in sources
    )


@pytest.mark.container
def test_fresh_opencode_session_loads_provider_policies(devbox_image: str):
    """The launcher's baseline config must load as native OpenCode v2 config.

    The devbox baseline restricts providers with `experimental.policies`
    provider-use deny statements (the v2 successor of the v1
    `disabled_providers` list), sets `external_directory` permissions, and
    allows `websearch` so the built-in web search tool never falls back to
    an approval prompt. OpenCode drops policy statements that fail
    validation, so verify the generated shape is accepted and preserved by
    a fresh session.
    """
    config = json.dumps(
        {
            "$schema": "https://opencode.ai/config.json",
            "experimental": {
                "policies": [
                    {
                        "action": "provider.use",
                        "resource": "github-copilot",
                        "effect": "deny",
                    },
                    {
                        "action": "provider.use",
                        "resource": "gitlab",
                        "effect": "deny",
                    },
                ]
            },
            "permissions": [
                {
                    "action": "external_directory",
                    "resource": "/home/*",
                    "effect": "allow",
                },
                {
                    "action": "external_directory",
                    "resource": "/root/*",
                    "effect": "deny",
                },
                {
                    "action": "external_directory",
                    "resource": "/sandbox/*",
                    "effect": "allow",
                },
                {
                    "action": "external_directory",
                    "resource": "/tmp/*",
                    "effect": "allow",
                },
                {
                    "action": "websearch",
                    "resource": "*",
                    "effect": "allow",
                },
            ],
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
            "debug",
            "config",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    output = f"{res.stdout}\n{res.stderr}"
    assert res.returncode == 0, (
        f"OpenCode could not load the provider policy config.\n{output}"
    )
    sources = json.loads(res.stdout)
    inline = next(
        source
        for source in sources
        if source.get("info", {}).get("experimental", {}).get("policies")
    )
    assert inline["info"]["experimental"]["policies"] == [
        {"action": "provider.use", "resource": "github-copilot", "effect": "deny"},
        {"action": "provider.use", "resource": "gitlab", "effect": "deny"},
    ]
    permissions = inline["info"]["permissions"]
    assert {
        "action": "external_directory",
        "resource": "/root/*",
        "effect": "deny",
    } in permissions
    assert {
        "action": "external_directory",
        "resource": "/sandbox/*",
        "effect": "allow",
    } in permissions
    assert {
        "action": "websearch",
        "resource": "*",
        "effect": "allow",
    } in permissions


@pytest.mark.container
def test_fresh_opencode_session_loads_github_mcp_config(devbox_image: str):
    config = json.dumps(
        {
            "$schema": "https://opencode.ai/config.json",
            "mcp": {
                "servers": {
                    "github": {
                        "type": "local",
                        "command": ["github-mcp-server-proxy"],
                        "disabled": False,
                        "environment": {
                            "GITHUB_PERSONAL_ACCESS_TOKEN": (
                                "{env:GITHUB_PERSONAL_ACCESS_TOKEN}"
                            ),
                            "GITHUB_TOOLSETS": (
                                "context,repos,issues,pull_requests,users"
                            ),
                        },
                    },
                }
            },
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
            "debug",
            "config",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    output = f"{res.stdout}\n{res.stderr}"
    assert res.returncode == 0, (
        f"OpenCode could not load the local GitHub MCP config.\n{output}"
    )
    sources = json.loads(res.stdout)
    github = next(
        source["info"]["mcp"]["servers"]["github"]
        for source in sources
        if source.get("info", {}).get("mcp", {}).get("servers", {}).get("github")
    )
    assert github["type"] == "local"
    assert github["command"] == ["github-mcp-server-proxy"]
    assert github["disabled"] is False
    assert github["environment"]["GITHUB_TOOLSETS"] == (
        "context,repos,issues,pull_requests,users"
    )


@pytest.mark.container
def test_fresh_opencode_session_loads_websearch_provider_config(devbox_image: str):
    """The Tavily websearch fragment must load as native OpenCode v2 config.

    The launcher selects OpenCode's built-in websearch Tavily provider via a
    top-level `websearch.provider` key instead of registering Tavily's
    remote MCP server. OpenCode drops config keys that fail validation, so
    verify the shape is accepted and preserved by a fresh session: an
    OpenCode pin bump that renames the key or the provider ID must fail
    here rather than silently regressing to the provider selection prompt.
    """
    config = json.dumps(
        {
            "$schema": "https://opencode.ai/config.json",
            "websearch": {
                "provider": "tavily",
            },
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
            "debug",
            "config",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    output = f"{res.stdout}\n{res.stderr}"
    assert res.returncode == 0, (
        f"OpenCode could not load the websearch provider config.\n{output}"
    )
    sources = json.loads(res.stdout)
    websearch = next(
        source["info"]["websearch"]
        for source in sources
        if source.get("info", {}).get("websearch")
    )
    assert websearch == {"provider": "tavily"}
