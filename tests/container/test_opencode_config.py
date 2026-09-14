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
