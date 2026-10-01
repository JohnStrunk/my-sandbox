#! /bin/bash

# Fast validation for iterating on launcher scripts, configuration, or
# documentation.
#
# Runs pre-commit lint checks and unit tests (tests/unit) without starting a
# disposable Lima VM, so it is the fast iteration tier. The VM toolchain
# provides pre-commit and uv; pre-commit downloads hook environments on the
# first run and reuses its VM-local cache afterward.
#
# For the full VM validation, run the documented VM and recursive tiers from
# the guest (see README.md).

set -e -o pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
TOP_DIR=$(cd "$SCRIPT_DIR/.." && pwd)

echo "==> Running pre-commit checks"
"$TOP_DIR/.github/lint-all.sh"

echo "==> Running unit tests"
(cd "$TOP_DIR" && uv run --extra test pytest -m unit)
