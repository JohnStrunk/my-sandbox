#!/bin/bash
# User provisioning for the devbox Lima VM. Runs as the devbox user on
# every VM start, after the system script and after the 9p mounts are up.
# Every step is idempotent, so re-running is cheap and safe.
#
# Lima renders this script as a Go template when the instance is created
# (it also exposes the template params as $PARAM_* environment variables,
# used below). Keep the file free of stray template syntax.
set -euo pipefail

# --- Link host-shared paths into the guest home -----------------------------
# Mounts are same-path: each guest mount point is the literal host path
# (e.g. /home/<user>/src), so the absolute .git pointers of linked worktrees
# resolve unchanged inside the VM. The guest home itself (/home/<user>.guest)
# is VM-local, so each shared path is linked into it. Targets are discovered
# from the live 9p mounts, which keeps this working on any host layout.
link_shared() {
  local rel="$1" target link
  target="$(findmnt -rn -t 9p -o TARGET | grep -x -m1 -- ".*/${rel}" || true)"
  if [ -z "$target" ]; then
    echo "devbox: no 9p mount for '~/${rel}'; skipping symlink" >&2
    return 0
  fi
  link="${HOME}/${rel}"
  mkdir -p "$(dirname "$link")"
  if [ -e "$link" ] && [ ! -L "$link" ]; then
    echo "devbox: '~/${rel}' exists and is not a symlink; leaving it" >&2
    return 0
  fi
  ln -sfn "$target" "$link"
}

for rel in \
  .agents \
  .config/acli \
  .config/gcloud \
  .config/gh \
  .config/gws \
  .config/opencode \
  .local/share/opencode \
  kb \
  src
do
  link_shared "$rel"
done

# --- Git identity (seeded once from --param at create time) ------------------
if [ -n "${PARAM_GitUserName:-}" ] \
  && [ -z "$(git config --global user.name 2>/dev/null)" ]; then
  git config --global user.name "$PARAM_GitUserName"
fi
if [ -n "${PARAM_GitUserEmail:-}" ] \
  && [ -z "$(git config --global user.email 2>/dev/null)" ]; then
  git config --global user.email "$PARAM_GitUserEmail"
fi
if [ -z "$(git config --global user.name 2>/dev/null)" ] \
  || [ -z "$(git config --global user.email 2>/dev/null)" ]; then
  echo "devbox: Git user.name and/or user.email are not configured." >&2
  echo "Commits inside the VM will fail until you run:" >&2
  echo '  git config --global user.name "Your Name"' >&2
  echo "  git config --global user.email you@example.com" >&2
fi

# --- GitHub over HTTPS ----------------------------------------------------------
# The VM has no SSH keys for GitHub. Rewrite GitHub SSH remote URLs (kept
# by checkouts cloned on the host) to HTTPS without changing the host's
# Git config or any mounted repository's .git/config. gh (below) supplies
# credentials for HTTPS operations.
for ssh_prefix in "git@github.com:" "ssh://git@github.com/"; do
  if ! git config --global --get-all url."https://github.com/".insteadOf \
    2>/dev/null | grep -qxF "$ssh_prefix"; then
    git config --global --add url."https://github.com/".insteadOf \
      "$ssh_prefix"
  fi
done

# --- gh as Git's credential helper -----------------------------------------------
# gh's auth state comes from the host-shared ~/.config/gh. Register the
# helper explicitly: environment-only authentication may not appear in
# gh's stored host list.
if gh auth status >/dev/null 2>&1; then
  gh auth setup-git --hostname github.com --force
else
  echo "devbox: gh is not authenticated; authenticated Git operations" >&2
  echo "against github.com may fail. Run 'gh auth login' inside the VM," >&2
  echo "or 'gh auth login' on the host, then restart the VM." >&2
fi

# --- uv (Python packaging) --------------------------------------------------------
# Pinned from container/tool-versions.json (.tools.uv.version);
# scripts/validate_tool_versions.py keeps this pin in sync with the
# manifest, and Renovate keeps both current.
# renovate: datasource=github-releases depName=astral-sh/uv
UV_VERSION="0.12.16"
installed_uv="$("$HOME/.local/bin/uv" --version 2>/dev/null | awk '{print $2}' || true)"
if [ "$installed_uv" != "$UV_VERSION" ]; then
  curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" | sh
fi

# --- Stage the devbox-tools agent skill ---------------------------------------------
# Refreshed from the my-sandbox checkout on every start, mirroring how the
# container devbox stages image-owned skills after the host .agents mount
# is applied. Writes through the shared ~/.agents mount.
skill_src="${HOME}/src/my-sandbox/container/agent-skills/devbox-tools"
skill_dst="${HOME}/.agents/skills/devbox-tools"
if [ -d "$skill_src" ]; then
  mkdir -p "${HOME}/.agents/skills"
  rm -rf "$skill_dst"
  cp -R "$skill_src" "$skill_dst"
else
  echo "devbox: ${skill_src} not found; skipping devbox-tools skill" >&2
fi

# --- Warm the OpenCode background service --------------------------------------------
# OpenCode's model catalog lives behind its background service. Load it
# here so the first interactive session starts immediately. The service's
# state is VM-local (~/.local/state/opencode); the host keeps its own.
# Absolute path, like probe-readiness.sh: this script also runs through a
# non-login shell.
if ! timeout 30 /usr/local/bin/opencode models >/dev/null 2>&1; then
  echo "devbox: OpenCode model catalog warm-up failed; continuing" >&2
fi
