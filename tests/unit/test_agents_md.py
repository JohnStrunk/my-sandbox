"""Regression tests for the AGENTS.md PR-landing guidance (issue #177).

Needle tests prove the repository guidance documents the canonical one-shot
wait-for-CI/merge recipe -- a single blocking ``gh pr checks --watch`` call
plus a bounded ``gh pr view`` merge wait that distinguishes "in merge queue"
from "blocked" -- so a future edit cannot casually weaken it or reintroduce
blind sleep-based polling.

The behavioral tests execute the documented merge-wait loop itself (extracted
from the markdown, never a copied duplicate) against a scripted fake ``gh``,
proving the recipe behaves as documented: it exits zero once merged, exits
non-zero when the PR closes without merging, fails fast when ``gh`` itself
errors, and on timeout prints the check table that distinguishes queued from
blocked.
"""

import os
import subprocess
from pathlib import Path

import pytest

AGENTS_MD_RELPATH = Path("AGENTS.md")
RECIPE_PR_NUMBER = "4242"


def _agents_md_text(repo_root: Path) -> str:
    path = repo_root / AGENTS_MD_RELPATH
    assert path.is_file(), f"missing guidance file: {path}"
    return path.read_text(encoding="utf-8")


@pytest.mark.unit
def test_agents_md_exists(repo_root: Path):
    assert (repo_root / AGENTS_MD_RELPATH).is_file()


@pytest.mark.unit
@pytest.mark.parametrize(
    "needle",
    [
        # PR-landing policy section.
        "how prs land",
        "ci workflow - success",
        "do-not-merge",
        "mergify",
        # One-shot wait-for-CI recipe (#177).
        "one-shot",
        "gh pr checks",
        "--watch",
        "single blocking call",
        # Bounded merge-wait recipe (#177).
        "bounded",
        "state,mergedat,mergecommit",
        "not a valid",
        "mergify merge queue",
        "mergify merge protections",
        "distinguishes queued from blocked",
        "@mergifyio queue",
        # Blind polling is explicitly rejected.
        "blind",
    ],
)
def test_agents_md_encodes_pr_wait_recipe(repo_root: Path, needle: str):
    # Collapse whitespace so the check is robust to how the prose is re-wrapped.
    normalized = " ".join(_agents_md_text(repo_root).lower().split())
    assert needle.lower() in normalized, (
        f"AGENTS.md is missing a required constraint: {needle!r}"
    )


def _shell_blocks(text: str) -> list[str]:
    """Return the raw (still indented) contents of ```shell fenced blocks."""
    blocks: list[str] = []
    current: list[str] = []
    in_block = False
    for line in text.splitlines():
        stripped = line.strip()
        if not in_block and stripped.startswith("```shell"):
            in_block = True
            current = []
            continue
        if in_block and stripped == "```":
            blocks.append("\n".join(current))
            in_block = False
            continue
        if in_block:
            current.append(line)
    return blocks


def _dedent_block(block: str) -> str:
    """Strip the common leading indentation (list-item content indent)."""
    lines = block.splitlines()
    indents = [len(line) - len(line.lstrip(" ")) for line in lines if line.strip()]
    assert indents, "merge-wait block must not be empty"
    common = min(indents)
    return "\n".join(line[common:] if line.strip() else "" for line in lines)


def _extract_merge_wait_loop(repo_root: Path) -> str:
    """Extract the bounded merge-wait loop from AGENTS.md, placeholders filled."""
    blocks = [
        _dedent_block(block) for block in _shell_blocks(_agents_md_text(repo_root))
    ]
    loops = [b for b in blocks if "mergeCommit" in b and "sleep" in b]
    assert len(loops) == 1, "expected exactly one bounded merge-wait loop"
    script = loops[0]
    assert "<number>" in script, "loop must reference the PR number placeholder"
    # Pin the documented timing and output shape the loop's `case` logic and
    # the prose ("every 30 seconds", "15 minutes") depend on.
    assert "sleep 30" in script, "loop must keep the documented poll interval"
    assert "[.state,.mergedAt,.mergeCommit.oid]|@tsv" in script, (
        "loop must keep the tsv merge-state output the case logic parses"
    )
    return script.replace("<number>", RECIPE_PR_NUMBER)


_FAKE_GH = """\
#!/usr/bin/env bash
# Scripted gh double used by the AGENTS.md recipe tests.
printf '%s\\n' "$*" >> "$FAKE_GH_STATE/calls.log"
case "$1 $2" in
  "pr view")
    if [ "${VIEW_FAIL:-0}" = "1" ]; then
      echo "gh: pr view failed" >&2
      exit 1
    fi
    count_file="$FAKE_GH_STATE/view-count"
    n=$(( $(cat "$count_file" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$count_file"
    if [ "$n" -ge "${MERGE_AT:-999999}" ]; then
      case "${FINAL_STATE:-OPEN}" in
        MERGED) printf 'MERGED\\t2026-09-26T00:00:00Z\\tc0ffee\\n' ;;
        CLOSED) printf 'CLOSED\\t\\t\\n' ;;
        *) printf 'OPEN\\t\\t\\n' ;;
      esac
    else
      printf 'OPEN\\t\\t\\n'
    fi
    ;;
  "pr checks")
    case "${CHECKS_TABLE:-queued}" in
      blocked)
        printf 'CI Workflow - Success\\tfail\\nMergify Merge Protections\\tfail\\n'
        ;;
      *)
        printf 'CI Workflow - Success\\tpass\\nMergify Merge Queue\\tpending\\n'
        ;;
    esac
    ;;
  *)
    echo "unexpected gh invocation: $*" >&2
    exit 64
    ;;
esac
"""

_FAKE_SLEEP = """\
#!/usr/bin/env bash
# No-op sleep so the bounded loop runs instantly under test.
exit 0
"""


def _run_recipe(
    repo_root: Path,
    tmp_path: Path,
    merge_at: int,
    final_state: str,
    checks_table: str = "queued",
    view_fail: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run the documented merge-wait loop with the fake gh double on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(_FAKE_GH, encoding="utf-8")
    (bin_dir / "sleep").write_text(_FAKE_SLEEP, encoding="utf-8")
    for name in ("gh", "sleep"):
        (bin_dir / name).chmod(0o755)
    state_dir = tmp_path / "gh-state"
    state_dir.mkdir()
    env = {
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "FAKE_GH_STATE": str(state_dir),
        "MERGE_AT": str(merge_at),
        "FINAL_STATE": final_state,
        "CHECKS_TABLE": checks_table,
        "HOME": str(tmp_path),
    }
    if view_fail:
        env["VIEW_FAIL"] = "1"
    script = _extract_merge_wait_loop(repo_root)
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


def _view_calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "gh-state" / "calls.log"
    calls = [
        line
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.startswith(f"pr view {RECIPE_PR_NUMBER}")
    ]
    return calls


@pytest.mark.unit
def test_recipe_loop_exits_zero_when_merged(repo_root: Path, tmp_path: Path):
    result = _run_recipe(repo_root, tmp_path, merge_at=3, final_state="MERGED")
    assert result.returncode == 0, result.stderr
    assert "waiting for merge" in result.stdout
    assert "merged: MERGED" in result.stdout
    # Polls 1-2 report OPEN; poll 3 reports the merge.
    assert result.stdout.count("waiting for merge") == 2
    calls = _view_calls(tmp_path)
    assert len(calls) == 3
    assert all(RECIPE_PR_NUMBER in call for call in calls)
    assert "state,mergedAt,mergeCommit" in calls[0]
    # The tsv output shape the loop's case logic parses must be requested.
    assert "[.state,.mergedAt,.mergeCommit.oid]|@tsv" in calls[0]


@pytest.mark.unit
def test_recipe_loop_reports_closed_without_merging(repo_root: Path, tmp_path: Path):
    result = _run_recipe(repo_root, tmp_path, merge_at=1, final_state="CLOSED")
    assert result.returncode == 1, result.stderr
    assert "closed without merging" in result.stdout
    # The CLOSED state is reported on the first poll, without waiting.
    assert "waiting for merge" not in result.stdout
    assert len(_view_calls(tmp_path)) == 1


@pytest.mark.unit
def test_recipe_loop_timeout_prints_queued_signal(repo_root: Path, tmp_path: Path):
    # The PR never merges: the bounded wait must give up, print the final
    # merge state, and show the check table (a pending Mergify Merge Queue
    # check is the "in merge queue" signal; a failing check would be
    # "blocked").
    result = _run_recipe(repo_root, tmp_path, merge_at=999999, final_state="OPEN")
    assert result.returncode == 1, result.stderr
    assert "timed out" in result.stdout
    assert "waiting for merge" in result.stdout
    assert "Mergify Merge Queue" in result.stdout
    # The loop is bounded: exactly one poll per iteration, no runaway.
    assert len(_view_calls(tmp_path)) == 30


@pytest.mark.unit
def test_recipe_loop_timeout_prints_blocked_signal(repo_root: Path, tmp_path: Path):
    # A failing authoritative check in the timeout check table is the
    # "blocked" signal, as distinct from the queued signal above.
    result = _run_recipe(
        repo_root,
        tmp_path,
        merge_at=999999,
        final_state="OPEN",
        checks_table="blocked",
    )
    assert result.returncode == 1, result.stderr
    assert "timed out" in result.stdout
    assert "CI Workflow - Success" in result.stdout
    assert "fail" in result.stdout
    assert "Mergify Merge Queue" not in result.stdout


@pytest.mark.unit
def test_recipe_loop_fails_fast_when_gh_view_errors(repo_root: Path, tmp_path: Path):
    # A failing `gh pr view` (bad number, expired auth, network loss) must
    # abort the wait immediately instead of dead-waiting for 15 minutes;
    # gh's own stderr passes through alongside the loop's message.
    result = _run_recipe(
        repo_root, tmp_path, merge_at=1, final_state="MERGED", view_fail=True
    )
    assert result.returncode == 1
    assert "gh pr view failed" in result.stderr
    assert "gh: pr view failed" in result.stderr
    assert "waiting for merge" not in result.stdout
    assert len(_view_calls(tmp_path)) == 1
