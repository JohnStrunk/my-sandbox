# AGENTS

- When working on this repo, you should use worktrees to isolate your work
- Before starting work on a new feature, fetch `origin/main` and create the
  worktree from `origin/main`.
- Before committing or opening a PR, fetch `origin/main` again and rebase the
  feature branch if it has advanced.
- Create worktrees in `.worktrees/`
- Immediately after creating a worktree, enter it and bootstrap the test
  environment before editing Python files:

  ```shell
  cd .worktrees/<worktree>
  uv sync --extra test
  ```

  This creates the worktree's `.venv` and installs `pytest` and the other test
  dependencies. The project Pyright config points the language server at this
  local environment so test imports resolve before the first test run.
- Start or reopen OpenCode with the active worktree as its project root (for
  example, run `opencode .worktrees/<worktree>` from the repository root). Do
  not edit files from multiple worktrees in one session rooted at the main
  checkout.
  OpenCode scopes LSP workspaces to the active project directory, which keeps
  diagnostics from sibling worktrees out of the current session.
- Shell commands and file-tool paths are independent: relative shell paths use
  the shell's current working directory. Every shell command that reads or
  writes worktree files must pass the intended worktree as `workdir` or use an
  absolute path under `.worktrees/`; never rely on the main checkout's default
  directory.
- Keep the main checkout clean and up to date when possible. Never revert
  unrelated existing changes; isolate work from `origin/main` in `.worktrees/`.
- In fresh worktrees, use `uv run --extra test pytest` or verify that test
  dependencies are installed before invoking test tools.
- Run tests with a sanitized environment. Never print the complete environment;
  report only allowlisted variable names and set/unset status.

## How PRs land

- `CI Workflow - Success` is the authoritative CI prerequisite for merging. It
  summarizes the `Automated Tests` and `Pre-commit checks` jobs.
- Mergify queues and merges eligible PRs automatically after that check passes.
  PRs authored by `JohnStrunk` or `renovate-bot` need no approval; other
  authors need at least one approval and no changes-requested review.
- A `do-not-merge` label prevents Mergify from queueing the PR.
- Do not merge manually. Mergify may take a few minutes after CI passes to
  queue and merge the PR, so continue polling rather than merging by hand.
- Poll the actual merge state with:

  ```shell
  gh pr view <number> --json state,mergedAt,mergeCommit \
    -q '[.state,.mergedAt,.mergeCommit.oid]|@tsv'
  ```

  `state=MERGED` or a non-null `mergedAt` confirms completion. `merged` is not
  a valid `gh pr view --json` field.

## Issue triage vocabulary

Open issues carry selection-relevant labels so an unblocked, unassigned,
high-value issue can be chosen from a single issue-list call, without
reading bodies:

- Status (exactly one): `ready` (unblocked and actionable) or `blocked`
  (a non-issue blocker, such as an upstream release or a host capability).
  Dependencies on other issues use GitHub's "Blocked by" relationship, not
  a label.
- Value (exactly one): `value:high`, `value:medium`, or `value:low` --
  impact on users, workflows, and risk.
- Confidence (exactly one): `confidence:high` (acceptance criteria are
  provable and no open questions; selectable from list output alone),
  `confidence:medium` (check the body before selecting), or
  `confidence:low` (needs a body read or a clarifying question before
  committing effort).
- `trial` marks a timeboxed experiment with an adopt/opt-in/drop decision
  bar; trial issues still carry the three labels above.

Apply the labels at issue-creation time; the `.github/ISSUE_TEMPLATE/`
forms preselect `ready` + medium defaults, so triage means adjusting the
value/confidence tiers. Selection order: open + unassigned + `ready` +
no open "Blocked by" links, ranked by value, ties broken by confidence.
Bot-maintained issues (for example the Renovate Dependency Dashboard) are
exempt; label them `dependencies` instead.
