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
  queue and merge the PR, so use the one-shot wait recipe below instead of
  blind `sleep` polling or merging by hand.

After opening a PR, wait for CI and the merge with two one-shot tool calls
(both can block for many minutes, so run them with a tool-call timeout of at
least 20 minutes or with timeouts disabled):

1. Watch CI to completion in a single blocking call. It returns when every
   check has a conclusion, prints the final results, and exits zero only when
   all checks passed (address any failures it reports before waiting further):

   ```shell
   gh pr checks <number> --watch
   ```

2. Wait for the Mergify queue to merge the PR with one bounded call. It polls
   the merge state every 30 seconds, gives up after 15 minutes, fails fast
   when `gh` itself errors, and prints the check table on timeout so the
   queued-versus-blocked reason is visible:

   ```shell
   pr=<number>
   for _ in $(seq 30); do
     row="$(gh pr view "$pr" --json state,mergedAt,mergeCommit \
       -q '[.state,.mergedAt,.mergeCommit.oid]|@tsv')"
     [ -n "$row" ] || { printf 'gh pr view failed\n' >&2; exit 1; }
     case "$row" in
       MERGED*) printf 'merged: %s\n' "$row"; exit 0 ;;
       CLOSED*) printf 'closed without merging: %s\n' "$row"; exit 1 ;;
       *) printf 'waiting for merge: %s\n' "$row" ;;
     esac
     sleep 30
   done
   printf 'merge wait timed out; last state: %s\n' "$row"
   gh pr checks "$pr"
   exit 1
   ```

   `state=MERGED` (with `mergedAt` and `mergeCommit` set) confirms completion.
   `merged` is not a valid `gh pr view --json` field. If the bounded wait
   times out, the printed check table distinguishes queued from blocked:

   - A pending `Mergify Merge Queue` check means the PR is in the merge
     queue; rerun the bounded wait.
   - A failing `CI Workflow - Success` or `Mergify Merge Protections` check
     means the PR is blocked; fix the failure or the reported protection
     (missing approval, `do-not-merge` label, changes-requested review).
   - All checks green but nothing queued means eligibility is in doubt;
     re-check it, give Mergify a few more minutes, and as a last resort
     comment `@Mergifyio queue` to ask Mergify to queue the PR explicitly.

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
