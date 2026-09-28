#!/bin/bash
# System provisioning for the devbox Lima VM. Runs as root on every VM
# start, before the user script. Every step is idempotent, so re-running
# is cheap and safe.
#
# Lima renders this script as a Go template when the instance is created,
# substituting the guest username below. Keep the file free of stray
# template syntax so that substitution keeps working.
set -euo pipefail

DEVBOX_USER="{{.User}}"

# --- Fedora packages -------------------------------------------------------
# Mirrors the container devbox (container/Dockerfile) plus the nested-
# virtualization pieces (qemu-kvm, edk2-ovmf for L2 UEFI firmware).
# The rpm -q guards make re-runs skip dnf entirely when nothing changed.
packages=(
  aardvark-dns        # nested Podman container-name DNS
  curl
  edk2-ovmf           # UEFI firmware for nested (L2) Lima VMs
  fuse-overlayfs      # rootless Podman storage driver
  gh
  git
  jq
  netavark            # nested Podman network backend
  nodejs
  nodejs22-full-i18n  # full ICU data; avoids Node 22 Intl.Segmenter crash
  npm
  passt               # nested Podman default rootless network mode (pasta)
  podman
  qemu-kvm            # nested (L2) VMs
  shadow-utils-subid  # newuidmap/newgidmap for rootless Podman
  slirp4netns
)
missing=()
for package in "${packages[@]}"; do
  rpm -q "$package" >/dev/null 2>&1 || missing+=("$package")
done
if [ "${#missing[@]}" -gt 0 ]; then
  dnf install -y --setopt=install_weak_deps=False "${missing[@]}"
  dnf clean all
  rm -rf /var/cache/dnf
fi

# --- Nested virtualization access -------------------------------------------
# /dev/kvm is root:kvm 0660 on Fedora; give the devbox user access. The
# host-side requirement (nested=1 in kvm_intel/kvm_amd) is enforced by
# Lima itself: nestedVirtualization: true fails fast when it is off.
usermod --append --groups kvm "$DEVBOX_USER"
# Load the KVM module now so /dev/kvm exists before anything needs it.
modprobe kvm || true
case "$(uname -m)" in
  x86_64) modprobe kvm_intel || modprobe kvm_amd || true ;;
esac

# --- Rootless Podman base -----------------------------------------------------
# Lima's own boot script (20-rootless-base.sh in the Lima sources) already
# configures, on every boot and before provisioning runs:
#   - /etc/subuid and /etc/subgid entries for the devbox user
#   - cgroup delegation for the user's systemd instance (Delegate=yes)
#   - loginctl enable-linger
# Nothing is duplicated here; probe-readiness.sh verifies all three.

# --- OpenCode CLI -------------------------------------------------------------
# Pinned from container/tool-versions.json (.tools.opencode.version);
# scripts/validate_tool_versions.py keeps this pin in sync with the
# manifest, and Renovate keeps both current.
# renovate: datasource=npm depName=@opencode/cli
OPENCODE_VERSION="2.0.16"
installed_opencode="$(npm ls -g --depth=0 --json 2>/dev/null \
  | jq -r '.dependencies["@opencode/cli"].version // ""' || true)"
if [ "$installed_opencode" != "$OPENCODE_VERSION" ]; then
  npm install -g --no-fund "@opencode/cli@${OPENCODE_VERSION}"
fi

# --- limactl (for nested L2 VMs) ------------------------------------------------
# Pinned from container/tool-versions.json (.tools.limactl.version).
# The stamp file makes re-runs a no-op until the pin changes.
# renovate: datasource=github-releases depName=lima-vm/lima
LIMACTL_VERSION="2.2.0"
stamp="/usr/local/share/devbox-vm/limactl.version"
installed_limactl=""
if [ -f "$stamp" ]; then
  installed_limactl="$(cat "$stamp" 2>/dev/null || true)"
fi
if [ "$installed_limactl" != "$LIMACTL_VERSION" ]; then
  arch="$(uname -m)"
  curl -fsSL \
    "https://github.com/lima-vm/lima/releases/download/v${LIMACTL_VERSION}/lima-${LIMACTL_VERSION}-Linux-${arch}.tar.gz" \
    -o /tmp/devbox-limactl.tgz
  tar -C /usr/local -xzf /tmp/devbox-limactl.tgz
  rm -f /tmp/devbox-limactl.tgz
  mkdir -p /usr/local/share/devbox-vm
  printf '%s\n' "$LIMACTL_VERSION" >"$stamp"
fi
