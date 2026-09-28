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

# Nested virtualization. With -cpu host the guest only sees the vmx/svm
# CPU flag when the host KVM module exposes nesting, so this is the real
# guard for L2 VM support on x86_64. (Lima <= 2.2 does not itself fail
# when host nesting is disabled.) aarch64 has no equivalent flag; the L2
# boot test in lima/README.md's checklist covers it.
test -e /dev/kvm || fail "/dev/kvm is missing (host nested virtualization?)"
case "$(uname -m)" in
  x86_64)
    grep -qm1 -w vmx /proc/cpuinfo \
      || fail "no vmx CPU flag: host nested virtualization is off (kvm_intel nested=0?)"
    ;;
esac
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
  # provision-user.sh links each shared path into the guest home; a real
  # (non-symlink) path here means the sharing setup did not run through.
  test -L "${HOME}/${rel}" \
    || fail "'~/${rel}' is not a symlink to its 9p mount (provisioning?)"
done

# OpenCode's volatile state must stay VM-local, never host-shared.
if [ -L "${HOME}/.local/state/opencode" ]; then
  fail "the OpenCode state dir (.local/state/opencode) must not be a symlink; it must stay VM-local"
fi
