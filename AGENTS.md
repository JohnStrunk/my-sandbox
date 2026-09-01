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
