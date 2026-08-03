#! /bin/bash

# This command ensures pre-commit is run on all modified files, not just those
# currently tracked by git. That allows linting of new files in addition to
# modified ones.
#
# It properly obeys .gitignore and allows explicit ignores via IGNORE_DIRS
# since checking .gitignore is slow.

set -e -o pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
TOP_DIR=$(cd "$SCRIPT_DIR/.." && pwd)

# Directories to ignore (relative to TOP_DIR)
# Include any directories that are in .gitignore and contain a large number of
# files
IGNORE_DIRS=(
	".git"
	".next"
	".venv"
	"node_modules"
	"out"
)

# Build find arguments as an array to avoid quoting/word splitting issues
find_args=("$TOP_DIR" -type f)
for dir in "${IGNORE_DIRS[@]}"; do
	find_args+=( -not -path "$TOP_DIR/$dir/*" )
done

# Find all files, filter out ignored files by querying `git`, and run
# pre-commit on the rest
find "${find_args[@]}" | while IFS= read -r file; do
	if ! git check-ignore -q "$file"; then
		echo "$file"
	fi
done | xargs pre-commit run --files
