---
name: "issue-to-pr"
description: >
  Take a repository issue from triage through a tested, reviewed implementation
  pull request, and choose the highest-value unassigned issue when none is
  specified. Use when asked to "work on the next issue", "pick up a backlog
  issue", "take an issue to a PR", or otherwise run the issue-to-PR workflow.
---

# Issue to Pull Request

Turn a repository issue into a tested, reviewed pull request. Reach GitHub only
through the `gh` CLI (required for all authenticated actions) or the GitHub MCP
server for read-only lookups -- both over HTTPS, never SSH. When the user names
no specific issue, first select one; otherwise run the workflow directly on the
named issue.

## When no issue is named: choose the highest-value one

Do not equate value with issue number, recency, or how easy an issue looks.
Select the highest-value open, unassigned, unblocked issue. This repo labels
issues for exactly that decision (see the triage vocabulary in `AGENTS.md`);
use the labels first and reserve full-body reads for what they cannot answer.

1. Enumerate candidates with one list call that includes labels, assignees,
   and dependency summaries -- for example `gh api
   'repos/<owner>/<repo>/issues?state=open&per_page=100'` returns all three
   per item (the GitHub MCP `list_issues` filter works too). Note that
   `--assignee ""` is a no-op and still returns assigned issues; `gh
   issue list` needs `--search "no:assignee"` instead. Keep
   issues that are unassigned, labeled `ready`, not labeled `blocked`, and
   whose `issue_dependencies_summary.total_blocked_by` is 0. An open
   unassigned issue carrying no triage labels at all is not dropped here;
   keep it as a step-5 candidate so untriaged work stays selectable.
2. Rank the candidates by their labels: `value:high` before `value:medium`
   before `value:low`, ties broken by `confidence:high` before
   `confidence:medium` before `confidence:low`.
3. Spend the read budget the confidence tier allows: a
   `confidence:high` issue is selectable from list output alone;
   `confidence:medium` warrants a quick body check; select
   `confidence:low` only after a body read (or a clarifying question)
   confirms it is worth the effort.
4. Labels are a prior, not a verdict. Weigh severity, how many people or
   flows are affected, urgency, risk reduction, and strategic alignment;
   impact leads the ranking: do not let low effort override a materially
   higher-impact issue unless you document the tradeoff.
5. When triage labels are missing, contradictory, or absent from the repo,
   state the assumptions you made, fall back to reading descriptions,
   comments, dependencies, and linked issues, and choose the best
   value-to-effort candidate the available evidence supports; never guess
   silently.
6. Rank with a transparent, concise rationale (a short score or ordered
   list with issue references). Ask the user only when the ambiguity would
   materially change which issue is selected; otherwise proceed and record
   your reasoning.
7. Search for duplicates before creating any follow-up issue.
8. Record why the chosen issue is highest value in both the issue comment
   and the pull request body.
9. Claim the issue (assign it to yourself) before writing any code.

## Issue-to-PR workflow

1. Authenticate with GitHub over HTTPS only. Treat SSH access to GitHub as
   unavailable: run `gh auth setup-git` so Git uses `gh` for HTTPS, and never
   attempt SSH remotes or `git@github.com:` URLs.
2. Preserve the workspace. Never revert or discard unrelated or dirty changes,
   and never build in a dirty main checkout. Fetch `origin/main` and create an
   isolated worktree from it under `.worktrees/`, per the repository `AGENTS.md`.
3. Claim and read. Assign the issue to yourself, then read the issue plus the
   repository guidance (`AGENTS.md`, `README.md`) and any referenced docs.
4. Plan. Post the approach and any initial findings or questions as a comment on
   the issue before coding.
5. Implement the smallest complete change that satisfies the acceptance criteria,
   following existing conventions and reusing what already exists.
6. Test from the fastest tier upward -- unit, lint, and type checks first, then
   the relevant integration or container coverage. Use the repository's
   documented test command (for example `uv run --extra test pytest`) in a
   sanitized environment, and never print the full environment.
7. Review. Have an independent subagent review the final diff for quality and
   completeness, and resolve every actionable finding before opening the pull
   request.
8. Rebase, then open the pull request. Fetch `origin/main` again and rebase the
   branch onto it if it advanced (per `AGENTS.md`). Search for a PR template and
   follow it when present. Describe what changed, the test results, and any known
   limitations. Push the branch, create the pull request, and report CI status.
9. Land it. Wait for CI with a single blocking `gh pr checks <number> --watch`
   call, then wait for the merge with the bounded
   `gh pr view <number> --json state,mergedAt,mergeCommit` loop from the
   repository's wait recipe (see "How PRs land" in `AGENTS.md`) -- never with
   blind `sleep` round-trips. Once the pull request merges (or the repository
   auto-merges it), update the local `main`, then remove the worktree and its
   branch.

## Guardrails

- Route every GitHub action through `gh` or the GitHub MCP server over HTTPS; no
  SSH.
- Keep the user's dirty and untracked files untouched; isolate all edits in the
  `.worktrees/` worktree created from the latest `origin/main`.
- Prefer reuse and existing house patterns; keep the change minimal.
- Leave decisions and verification evidence (test output, selection rationale)
  for the reviewer rather than asserting success without proof.
