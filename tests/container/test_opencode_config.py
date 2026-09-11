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
