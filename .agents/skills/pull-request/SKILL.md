---
name: "pull-request"
description: >
  Provides a structured process for creating pull requests use when asked to
  "make a PR".
---

# Creating a Pull Request

When asked to "make a PR" or "create a pull request", follow this structured
process:

- Ensure all tests pass and the code is ready for review
- Rebase the branch on the latest main to ensure it is up to date
- Create the Pull Request on GitHub
- Wait for CI in a single blocking call instead of polling or sleeping:

  `gh pr checks <number> --watch`

  It returns when every check has a conclusion, prints the final results, and
  exits zero only when all checks passed. Address any failures it reports.
- Wait for the merge in one bounded call instead of blind `sleep` polling:
  poll `gh pr view <number> --json state,mergedAt,mergeCommit` in a short
  loop until `state=MERGED` (or `CLOSED`), and on timeout inspect the check
  table to tell "in merge queue" from "blocked". Follow the canonical wait
  recipe and merge policy in this repository's `AGENTS.md` ("How PRs land") --
  it has the exact commands and the queued-versus-blocked signals.
- Once the PR merges, clean up any local branches and worktrees. Then update
  the main branch
