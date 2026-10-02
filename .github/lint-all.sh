#! /bin/bash

# Run pre-commit on tracked files that still exist and on non-ignored
# untracked files. Git's NUL-delimited enumeration avoids a per-path
# `git check-ignore` subprocess and remains safe for unusual filenames.

set -e -o pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
TOP_DIR=$(cd "$SCRIPT_DIR/.." && pwd)

# These high-volume directory prefixes stay excluded even if files are tracked.

cd "$TOP_DIR"

# Prevent ambient repository overrides from redirecting the file inventory.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR \
	GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES \
	GIT_CEILING_DIRECTORIES GIT_DISCOVERY_ACROSS_FILESYSTEM \
	GIT_CONFIG_GLOBAL GIT_CONFIG_SYSTEM GIT_CONFIG_PARAMETERS GIT_CONFIG_COUNT
for name in "${!GIT_CONFIG_KEY_@}" "${!GIT_CONFIG_VALUE_@}"; do
	[[ -n "$name" ]] && unset "$name"
done

has_symlink_parent() {
	local path="$1"
	while [[ "$path" == */* ]]; do
		path="${path%/*}"
		[[ -L "$path" ]] && return 0
	done
	return 1
}

# `--cached` retains tracked files even when their paths match .gitignore;
# `--others --exclude-standard` adds only non-ignored untracked paths. Filter
# deleted entries and non-regular files to preserve find's previous -type f
# behavior. Skip symlink ancestors so tracked paths cannot escape TOP_DIR.
git ls-files --cached --others --exclude-standard -z |
	while IFS= read -r -d '' file; do
		case "$file" in
			.git/* | .next/* | .venv/* | node_modules/* | out/*)
				continue
				;;
		esac

		if [[ -f "$file" && ! -L "$file" ]] \
			&& ! has_symlink_parent "$file"; then
			# The prefix keeps filenames beginning with '-' from looking like options.
			printf './%s\0' "$file"
		fi
	done |
	xargs -0 -r pre-commit run --files
