#!/usr/bin/env bash
# Verify the pinned The Source MCP environment resolves to wheels for its
# pinned interpreter and installs, without reading any credentials.
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
source_project="$repo_root/lima/the-source"
scratch="$(mktemp -d)"

# Invoked indirectly by the EXIT trap.
# shellcheck disable=SC2329
cleanup() {
  rm -rf -- "$scratch"
}
trap cleanup EXIT

uv_bin="$(command -v uv)" || {
  printf 'verify-the-source-mcp-install: uv is required\n' >&2
  exit 127
}

pinned_python="$(
  python3 - "$source_project/pyproject.toml" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as handle:
    specifier = tomllib.load(handle)["project"]["requires-python"]
if not specifier.startswith("=="):
    sys.exit(f"expected an exact requires-python pin, found {specifier!r}")
print(specifier[2:])
PY
)"

"$uv_bin" venv --quiet --python "$pinned_python" "$scratch/interpreter"

"$uv_bin" export \
  --quiet \
  --project "$source_project" \
  --locked \
  --format requirements-txt \
  --no-hashes \
  --no-emit-project \
  --no-emit-package igloo-mcp \
  -o "$scratch/requirements.txt"

# Every registry dependency must have a wheel for the pinned interpreter.
# Only the git-sourced igloo-mcp legitimately builds from source, so it is
# excluded from the export above.
"$uv_bin" pip install \
  --quiet \
  --python "$scratch/interpreter/bin/python" \
  --dry-run \
  --only-binary :all: \
  -r "$scratch/requirements.txt"

printf 'The Source MCP lock requires no source builds\n'

UV_PROJECT_ENVIRONMENT="$scratch/the-source" \
  "$uv_bin" sync --quiet --project "$source_project" --locked

test -x "$scratch/the-source/bin/igloo-mcp"

printf 'The Source MCP environment installed\n'
