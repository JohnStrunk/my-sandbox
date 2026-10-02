## Linked issue

<!-- Link the issue this change implements using a GitHub closing keyword
     (for example, "Fixes") followed by the issue reference, so the issue
     closes automatically when this pull request merges. -->

## Why this issue

<!-- Selection rationale: why this was the highest-value unassigned,
     unblocked issue when work started. Reference the triage labels and
     the selection order in AGENTS.md. -->

## Summary

<!-- What changed and why. Keep it brief; the diff and the linked issue
     carry the detail. -->

## Tests run

<!-- Exact commands and their results, with pass/skip counts. From a local VM,
     use the sanitizer and shared-worktree lock, for example:
     ./scripts/sanitized-test.sh --guest-vm --vm-lock --resource-preflight -- \
       env UV_PROJECT_ENVIRONMENT="$UV_PROJECT_ENVIRONMENT" \
       PRE_COMMIT_HOME="$HOME/.cache/pre-commit" ./scripts/fast-check.sh
     # Unit tests only (serial signal tests, then parallel-safe tests):
     ./scripts/sanitized-test.sh --guest-vm --vm-lock -- \
       env UV_PROJECT_ENVIRONMENT="$UV_PROJECT_ENVIRONMENT" \
       ./scripts/run-unit-tests.sh
     Paste real output instead of asserting success. -->

## Known limitations

<!-- Caveats, follow-ups, and anything intentionally out of scope, or
     "None". -->

## CI and merge status

<!-- CI results and merge state. Mergify queues and merges this pull
     request automatically once "CI Workflow - Success" passes; do not
     merge manually. -->
