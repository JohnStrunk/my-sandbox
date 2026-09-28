#!/bin/bash
# Readiness probe for the devbox Lima VM. Lima runs probes over SSH as
# the devbox user (no login shell), so tool paths are absolute.
# A failing probe makes `limactl start` report the VM as not ready.
set -euo pipefail

fail() {
  echo "devbox readiness: $1" >&2
  exit 1
}

# Tools installed by provision-system.sh / provision-user.sh.
test -x /usr/bin/git || fail "git is not installed"
test -x /usr/bin/gh || fail "gh is not installed"
test -x /usr/bin/podman || fail "podman is not installed"
test -x /usr/local/bin/opencode || fail "opencode is not installed"
test -x /usr/local/bin/limactl || fail "limactl is not installed"
test -x "${HOME}/.local/bin/uv" || fail "uv is not installed"

# Nested virtualization.
test -e /dev/kvm || fail "/dev/kvm is missing (host nested virtualization?)"
getent group kvm | grep -qw "$(id -un)" \
  || fail "the devbox user is not in the kvm group"

# Rootless Podman base (configured by Lima's 20-rootless-base.sh).
grep -q "^$(id -un):" /etc/subuid \
  || fail "/etc/subuid has no entry for $(id -un)"
grep -q "^$(id -un):" /etc/subgid \
  || fail "/etc/subgid has no entry for $(id -un)"

# Same-path 9p mounts (keep in sync with the mounts in devbox.yaml).
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
  findmnt -rn -t 9p -o TARGET | grep -xq -- ".*/${rel}" \
    || fail "the 9p mount for '~/${rel}' is not live"
done

# OpenCode's volatile state must stay VM-local, never host-shared.
if [ -L "${HOME}/.local/state/opencode" ]; then
  fail "the OpenCode state dir (.local/state/opencode) must not be a symlink; it must stay VM-local"
fi
