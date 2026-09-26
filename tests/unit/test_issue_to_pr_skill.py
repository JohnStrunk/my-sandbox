"""Regression test for the issue-to-PR skill (issue #78).

Proves the agent-facing skill is discoverable and pins the key
highest-value-selection rubric and issue-to-PR workflow constraints so a
future edit cannot casually weaken them.
"""

import re
from pathlib import Path

import pytest

SKILL_RELPATH = Path(".agents") / "skills" / "issue-to-pr" / "SKILL.md"


def _skill_text(repo_root: Path) -> str:
    path = repo_root / SKILL_RELPATH
    assert path.is_file(), f"missing skill file: {path}"
    return path.read_text(encoding="utf-8")


@pytest.mark.unit
def test_issue_to_pr_skill_file_exists(repo_root: Path):
    assert (repo_root / SKILL_RELPATH).is_file()


@pytest.mark.unit
def test_issue_to_pr_skill_frontmatter(repo_root: Path):
    text = _skill_text(repo_root)
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "SKILL.md must begin with YAML frontmatter"
    frontmatter = match.group(1)
    assert re.search(r'^name:\s*"?issue-to-pr"?\s*$', frontmatter, re.MULTILINE)
    assert re.search(r"^description:\s*\S", frontmatter, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.parametrize(
    "needle",
    [
        # Highest-value selection rubric.
        "highest-value",
        "no:assignee",
        "unblocked",
        "impact leads",
        "value-to-effort",
        "document the tradeoff",
        "state the assumptions",
        "never guess silently",
        "ask the user only",
        "duplicate",
        # gh-only / no SSH authentication constraint.
        "gh auth setup-git",
        "never attempt ssh",
        "https",
        # Required worktree layout and preservation of unrelated changes.
        ".worktrees/",
        "never revert",
        # Smallest-change, review, PR, and status-reporting steps.
        "smallest complete change",
        "subagent",
        "pr template",
        "known limitations",
        "ci status",
        "assign it to yourself",
        # One-shot wait-for-CI/merge recipe instead of blind sleeps (#177).
        "gh pr checks",
        "--watch",
        "bounded",
        "state,mergedat,mergecommit",
        "blind",
    ],
)
def test_issue_to_pr_skill_encodes_required_constraints(repo_root: Path, needle: str):
    # Collapse whitespace so the check is robust to how the prose is re-wrapped.
    normalized = " ".join(_skill_text(repo_root).lower().split())
    assert needle.lower() in normalized, (
        f"issue-to-pr skill is missing a required constraint: {needle!r}"
    )
