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
- Create the Pull Request on GitHub in a draft state
- Monitor CI checks and address any issues
- Once all checks are passed, rebase the branch on the latest main
- Ensure CI checks pass after rebasing
- Mark the PR as ready for review, and enable auto-merge if possible.
