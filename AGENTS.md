# AGENTS

- When working on this repo, you should use worktrees to isolate your work
- Before starting work on a new feature, fetch `origin/main` and create the
  worktree from `origin/main`.
- Before committing or opening a PR, fetch `origin/main` again and rebase the
  feature branch if it has advanced.
- Create worktrees in `.worktrees/`
- Keep the main checkout clean and up to date when possible. Never revert
  unrelated existing changes; isolate work from `origin/main` in `.worktrees/`.
- In fresh worktrees, use `uv run --extra test pytest` or verify that test
  dependencies are installed before invoking test tools.
- Run tests with a sanitized environment. Never print the complete environment;
  report only allowlisted variable names and set/unset status.

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
