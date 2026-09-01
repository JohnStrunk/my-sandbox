#! /bin/bash

# Fast validation for iterating on launcher scripts, configuration, or
# documentation.
#
# Runs only pre-commit lint checks and unit tests (tests/unit). It never
# builds the devbox container image, so it completes in seconds instead of
# the several minutes the full suite can take. The pre-commit and uv
# executables are preinstalled in the devbox image, so no manual tool
# installation is needed; pre-commit downloads and caches each hook's own
# environment on its first invocation in a new container, a one-time cost
# that later runs skip since devbox containers persist across sessions.
#
# For the full validation, including container and integration tests, run:
#   uv run --extra test pytest -m "not e2e_inference"

set -e -o pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
TOP_DIR=$(cd "$SCRIPT_DIR/.." && pwd)

echo "==> Running pre-commit checks"
"$TOP_DIR/.github/lint-all.sh"

echo "==> Running unit tests"
(cd "$TOP_DIR" && uv run --extra test pytest -m unit)
