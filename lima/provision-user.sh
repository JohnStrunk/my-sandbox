#!/bin/bash
# User provisioning for the devbox Lima VM. Runs on every start after mounts.
set -euo pipefail

DEVBOX_REPO="${PARAM_RepoPath:-}"
DEVBOX_SRC_ROOT="${PARAM_SrcPath:-}"
if [[ "$DEVBOX_SRC_ROOT" != /* || ! -d "$DEVBOX_SRC_ROOT" ]]; then
  echo "devbox: SrcPath must be the absolute guest-visible ~/src mount" >&2
  exit 1
fi
if [[ "$DEVBOX_REPO" != /* || ! -r "$DEVBOX_REPO/lima/tool-versions.json" ]]; then
  echo "devbox: RepoPath must be an absolute path to the my-sandbox checkout" >&2
  exit 1
fi
repo_prefix="${DEVBOX_SRC_ROOT%/}/"
case "$DEVBOX_REPO/" in
  "$repo_prefix"*) ;;
  *) echo "devbox: RepoPath must be inside SrcPath" >&2; exit 1 ;;
esac
MANIFEST="$DEVBOX_REPO/lima/tool-versions.json"
if ! cmp -s "$MANIFEST" /var/lib/devbox-vm/tool-versions.json; then
  echo "devbox: tool manifest changed during provisioning; restart the VM" >&2
  exit 1
fi
MANIFEST=/var/lib/devbox-vm/tool-versions.json
TOOL_BUILDER_HOME=/var/lib/devbox-toolbuilder
export CARGO_HOME="$HOME/.cargo"
export RUSTUP_HOME="$TOOL_BUILDER_HOME/.rustup"
export PATH="$HOME/.local/bin:$CARGO_HOME/bin:$TOOL_BUILDER_HOME/.local/bin:$TOOL_BUILDER_HOME/.cargo/bin:/usr/local/node/bin:/usr/local/go/bin:$PATH"
export UV_CACHE_DIR="$HOME/.cache/uv"
export GOPATH="$HOME/.cache/go"
export GOCACHE="$GOPATH/build-cache"
export PATH="$GOPATH/bin:$PATH"
export HF_HOME="$TOOL_BUILDER_HOME/.cache/semble/huggingface"
export SEMBLE_CACHE_LOCATION="$HOME/.cache/semble/index"
export PLAYWRIGHT_BROWSERS_PATH="$TOOL_BUILDER_HOME/.cache/ms-playwright"
export PLAYWRIGHT_MCP_BROWSER=chromium

manifest_agent_skill() {
  local tool="$1" field="$2"
  jq -er --arg tool "$tool" --arg field "$field" \
    '.tools[$tool].agent_skill[$field]' "$MANIFEST"
}

copy_if_changed() {
  local source="$1" destination="$2" mode="$3"
  if ! cmp -s "$source" "$destination"; then
    install -D -m "$mode" "$source" "$destination"
  fi
}

tree_differs() {
  local source="$1" destination="$2"
  [[ ! -d "$destination" ]] || ! diff -qr "$source" "$destination" >/dev/null 2>&1
}

# Same-path mounts preserve the absolute paths stored in linked-worktree .git files.
link_shared() {
  local rel="$1" target link
  target=""
  case "$rel" in
    src) target="$DEVBOX_SRC_ROOT" ;;
    .config/*) target="$HOME/.host-config/config/${rel#.config/}" ;;
    .local/share/opencode) target="$HOME/.host-config/local/share/opencode" ;;
    kb) target="$HOME/.host-config/kb" ;;
    *) echo "devbox: unsupported shared path '~/${rel}'" >&2; return 1 ;;
  esac
  findmnt -rn -t 9p,virtiofs -o TARGET | grep -Fxq -- "$target" || target=""
  if [[ -z "$target" ]]; then
    echo "devbox: no mount for '~/${rel}'; skipping symlink" >&2
    return 0
  fi
  link="$HOME/$rel"
  if [[ "$target" == "$link" ]]; then
    return 0
  fi
  if [[ -e "$link" && ! -L "$link" ]]; then
    echo "devbox: '~/${rel}' exists and is not a symlink; leaving it" >&2
    return 0
  fi
  mkdir -p "$(dirname "$link")"
  if [[ -L "$link" && "$(readlink "$link")" == "$target" ]]; then
    return 0
  fi
  ln -sfn "$target" "$link"
}

for rel in \
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

# Keep ~/.agents guest-local. A symlink left by the minimal bootstrap points
# into the host mount; remove only that symlink, never its target contents.
if [[ -L "$HOME/.agents" ]]; then
  rm -- "$HOME/.agents"
fi
mkdir -p \
  "$HOME/.cache/uv" \
  "$HOME/.cache/go/build-cache" \
  "$HOME/.cache/semble/index" \
  "$HOME/.local/share/kubebuilder-envtest" \
  "$HOME/.local/share/devbox-toolchain" \
  "$HOME/.local/bin" \
  "$HOME/.cargo/bin" \
  "$HOME/.agents/skills"

# Fingerprint the exact manifest and provisioners used during this start.
system_script_sha256="$(cat /var/lib/devbox-vm/system-provision.sha256)"
user_script_sha256="$(sha256sum "$0" | awk '{print $1}')"
manifest_sha256="$(sha256sum "$MANIFEST" | awk '{print $1}')"
tool_script_sha256="$(cat /var/lib/devbox-vm/tool-provision.sha256)"
tool_assets_sha256="$(cat /var/lib/devbox-vm/tool-assets.sha256)"
fingerprint="$(printf '%s\n%s\n%s\n%s\n%s\n' \
  "$manifest_sha256" "$system_script_sha256" "$user_script_sha256" \
  "$tool_script_sha256" "$tool_assets_sha256" \
  | sha256sum | awk '{print $1}')"
fingerprint_file="$HOME/.local/share/devbox-toolchain/provisioning.fingerprint"
previous_fingerprint="$(cat "$fingerprint_file" 2>/dev/null || true)"
if [[ -n "$previous_fingerprint" && "$previous_fingerprint" != "$fingerprint" ]]; then
  echo "devbox: toolchain fingerprint changed; applying updated manifest pins" >&2
fi

# Seed Git identity only when unset; route GitHub remotes through HTTPS/gh.
if [[ -n "${PARAM_GitUserName:-}" ]] \
  && [[ -z "$(git config --global user.name 2>/dev/null || true)" ]]; then
  git config --global user.name "$PARAM_GitUserName"
fi
if [[ -n "${PARAM_GitUserEmail:-}" ]] \
  && [[ -z "$(git config --global user.email 2>/dev/null || true)" ]]; then
  git config --global user.email "$PARAM_GitUserEmail"
fi
if [[ -z "$(git config --global user.name 2>/dev/null || true)" ]] \
  || [[ -z "$(git config --global user.email 2>/dev/null || true)" ]]; then
  echo "devbox: set Git user.name and user.email before committing in the VM" >&2
fi
for ssh_prefix in "git@github.com:" "ssh://git@github.com/"; do
  if ! git config --global --get-all url."https://github.com/".insteadOf \
    2>/dev/null | grep -qxF "$ssh_prefix"; then
    git config --global --add url."https://github.com/".insteadOf "$ssh_prefix"
  fi
done
if gh auth status >/dev/null 2>&1; then
  gh auth setup-git --hostname github.com --force
else
  echo "devbox: gh is not authenticated; run 'gh auth login' on the host or in the VM" >&2
fi

# Third-party packages, Rust, browser/model prefetch, and automatic version
# checks ran as devbox-toolbuilder with an empty environment and no host mounts.
for executable in \
  opencode markdownlint-cli2 playwright-cli repomix gws pre-commit pipenv \
  semble-bin rustup rustc cargo
do
  [[ -x "$TOOL_BUILDER_HOME/.local/bin/$executable" \
    || -x "$TOOL_BUILDER_HOME/.cargo/bin/$executable" ]] \
    || { echo "devbox: isolated tool $executable is missing" >&2; exit 1; }
done
copy_if_changed "/var/lib/devbox-vm/tool-assets/semble" \
  "$HOME/.local/bin/semble" 0755
copy_if_changed "/var/lib/devbox-vm/tool-assets/devbox-go" \
  "$HOME/.local/bin/devbox-go" 0755
copy_if_changed "/var/lib/devbox-vm/tool-assets/check_toolchain.py" \
  "$HOME/.local/bin/devbox-toolchain-check" 0755
ln -sfn "$TOOL_BUILDER_HOME/.local/bin/semble-bin" \
  "$HOME/.local/bin/semble-bin"
ln -sfn "$TOOL_BUILDER_HOME/.local/bin/rustup" "$HOME/.cargo/bin/rustup"

# VM-owned skills are staged after the host mount and win at their own names.
skill_src="$DEVBOX_REPO/lima/agent-skills/devbox-tools"
skill_dst="$HOME/.agents/skills/devbox-tools"
if [[ -d "$skill_src" ]] && tree_differs "$skill_src" "$skill_dst"; then
  rm -rf -- "$skill_dst"
  cp -R "$skill_src" "$skill_dst"
fi

AST_GREP_COMMIT="$(manifest_agent_skill ast_grep commit)"
AST_GREP_SKILL_SHA256="$(manifest_agent_skill ast_grep sha256)"
if [[ ! "$AST_GREP_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
  echo "devbox: invalid ast-grep skill commit in tool manifest" >&2
  exit 1
fi
if [[ ! "$AST_GREP_SKILL_SHA256" =~ ^[0-9a-f]{64}$ ]]; then
  echo "devbox: invalid ast-grep skill checksum in tool manifest" >&2
  exit 1
fi
ast_grep_skill_state="$HOME/.local/share/devbox-toolchain/ast-grep-skill.commit"
if [[ ! -f "$ast_grep_skill_state" ]] \
  || [[ "$(cat "$ast_grep_skill_state")" != "$AST_GREP_COMMIT" ]] \
  || [[ ! -f "$HOME/.agents/skills/ast-grep/SKILL.md" ]] \
  || [[ ! -f "$HOME/.agents/skills/ast-grep-outline/SKILL.md" ]]; then
  tmp="$(mktemp -d "$HOME/.cache/ast-grep-skill.XXXXXX")"
  archive="$tmp/agent-skill-${AST_GREP_COMMIT}.tar.gz"
  curl --retry 3 --retry-connrefused -fsSL \
    "https://github.com/ast-grep/agent-skill/archive/${AST_GREP_COMMIT}.tar.gz" \
    -o "$archive"
  printf '%s  %s\n' "$AST_GREP_SKILL_SHA256" "$archive" | sha256sum -c -
  tar -C "$tmp" -xzf "$archive"
  skill_root="$tmp/agent-skill-${AST_GREP_COMMIT}/ast-grep/skills"
  rm -rf -- "$HOME/.agents/skills/ast-grep" \
    "$HOME/.agents/skills/ast-grep-outline"
  cp -R "$skill_root/ast-grep" "$HOME/.agents/skills/ast-grep"
  cp -R "$skill_root/outline" "$HOME/.agents/skills/ast-grep-outline"
  printf '%s\n' "$AST_GREP_COMMIT" >"$ast_grep_skill_state"
  rm -rf -- "$tmp"
fi

# Expose host agent configuration and non-conflicting skills through the
# guest-local overlay. The VM-owned skill names above take precedence.
host_agents="$HOME/.host-config/agents"
if [[ -n "$host_agents" && -d "$host_agents" ]]; then
  for source in "$host_agents"/*; do
    [[ -e "$source" || -L "$source" ]] || continue
    name="${source##*/}"
    [[ "$name" == skills ]] && continue
    destination="$HOME/.agents/$name"
    if [[ ! -e "$destination" && ! -L "$destination" ]]; then
      ln -s "$source" "$destination"
    fi
  done
  if [[ -d "$host_agents/skills" ]]; then
    for source in "$host_agents/skills"/*; do
      [[ -e "$source" || -L "$source" ]] || continue
      name="${source##*/}"
      destination="$HOME/.agents/skills/$name"
      if [[ ! -e "$destination" && ! -L "$destination" ]]; then
        ln -s "$source" "$destination"
      fi
    done
  fi
fi

# Rootless Podman provides the Docker-compatible socket used by kind.
systemctl --user enable --now podman.socket

# OpenCode is not started here; its first credential-aware shell owns the service.
if [[ "$previous_fingerprint" != "$fingerprint" ]]; then
  fingerprint_tmp="$(mktemp "$HOME/.local/share/devbox-toolchain/provisioning.fingerprint.XXXXXX")"
  printf '%s\n' "$fingerprint" >"$fingerprint_tmp"
  mv -f "$fingerprint_tmp" "$fingerprint_file"
fi
