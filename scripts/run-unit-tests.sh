#!/usr/bin/env bash
# Run timing-sensitive signal/process-group tests first and serially, then run
# the remaining isolated unit tests in parallel to avoid scheduling races.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$TOP_DIR"

echo "==> Running timing-sensitive unit tests serially"
# Keep this selection non-empty; pytest's exit code 5 should expose a missing
# marker rather than silently dropping the process/signal coverage.
uv run --extra test pytest -m unit_serial

echo "==> Running parallel-safe unit tests (2 workers)"
uv run --extra test pytest -m "unit and not unit_serial" -n 2
