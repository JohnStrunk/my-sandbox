"""Regression test for the pull-request skill (issue #177).

Proves the skill teaches the one-shot wait-for-CI/merge recipe (a single
blocking ``gh pr checks --watch`` call plus a bounded ``gh pr view`` merge
wait) instead of blind sleep-based polling, so a future edit cannot casually
weaken it.
"""

import re
from pathlib import Path

import pytest

SKILL_RELPATH = Path(".agents") / "skills" / "pull-request" / "SKILL.md"


def _skill_text(repo_root: Path) -> str:
    path = repo_root / SKILL_RELPATH
    assert path.is_file(), f"missing skill file: {path}"
    return path.read_text(encoding="utf-8")


@pytest.mark.unit
def test_pull_request_skill_file_exists(repo_root: Path):
    assert (repo_root / SKILL_RELPATH).is_file()


@pytest.mark.unit
def test_pull_request_skill_frontmatter(repo_root: Path):
    text = _skill_text(repo_root)
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "SKILL.md must begin with YAML frontmatter"
    frontmatter = match.group(1)
    assert re.search(r'^name:\s*"?pull-request"?\s*$', frontmatter, re.MULTILINE)
    assert re.search(r"^description:\s*\S", frontmatter, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.parametrize(
    "needle",
    [
        # Structured PR process steps.
        "ensure all tests pass",
        "rebase",
        "create the pull request",
        "clean up any local branches and worktrees",
        # One-shot wait-for-CI recipe (#177).
        "gh pr checks",
        "--watch",
        "exits zero",
        # One-shot bounded merge wait recipe (#177).
        "bounded",
        "state,mergedat,mergecommit",
        "state=merged",
        "in merge queue",
        "blocked",
        "agents.md",
        "how prs land",
        # No blind polling.
        "instead of blind",
    ],
)
def test_pull_request_skill_encodes_wait_recipe(repo_root: Path, needle: str):
    # Collapse whitespace so the check is robust to how the prose is re-wrapped.
    normalized = " ".join(_skill_text(repo_root).lower().split())
    assert needle.lower() in normalized, (
        f"pull-request skill is missing a required constraint: {needle!r}"
    )
