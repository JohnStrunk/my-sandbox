#!/bin/bash
# Shared Fedora package and Ruby-pin logic for the devbox VM and the
# container preflight. Sourced by provision-system.sh after copying this file
# to root-owned storage; the container copies it at image build time.
#
# Keep VM-only packages separate: the preflight is a package/tool check, not a
# container-based devbox runtime, and must not install or exercise KVM or
# libvirt behavior.

# Packages needed by both the VM and the Fedora container preflight.
DEVBOX_FEDORA_PACKAGES=(
  aardvark-dns
  acl
  alsa-lib
  at-spi2-atk
  atk
  ca-certificates
  container-selinux
  cups-libs
  curl
  diffutils
  difftastic
  # Fedora's stable archive repository keeps older Ruby builds resolvable
  # after updates repodata drops them; normal repos still take precedence.
  fedora-repos-archive
  ffmpeg-free
  fd-find
  file
  fontconfig
  fuse-overlayfs
  gcc
  gh
  git
  glab
  glibc-devel
  gtk3
  hyperfine
  jq
  just
  libffi-devel
  libselinux-utils
  libXcomposite
  libXdamage
  libXext
  libXfixes
  libXi
  libXrandr
  libdrm
  libxkbcommon
  libyaml-devel
  make
  mesa-libgbm
  netavark
  nss
  openssl
  patch
  passt
  perl-File-Compare
  perl-File-Copy
  perl-FindBin
  perl-IPC-Cmd
  policycoreutils
  podman
  procps-ng
  python3
  python3-pip
  ripgrep
  rubypick
  shadow-utils
  shadow-utils-subid
  ShellCheck
  slirp4netns
  tokei
  unzip
  zlib-ng-compat-devel # Provides zlib-devel; rpm -q requires this concrete RPM name.
)

# Nested-VM, KVM, and C/C++ development tooling exists only in the Lima guest.
DEVBOX_FEDORA_VM_PACKAGES=(
  cmake
  edk2-ovmf
  gcc-c++
  gdb
  libvirt-client
  libvirt-daemon-config-network
  libvirt-daemon-kvm
  ninja-build
  pkgconf-pkg-config
  qemu-img
  qemu-kvm
)

devbox_install_dnf_packages() {
  local package missing=()
  for package in "$@"; do
    rpm -q "$package" >/dev/null 2>&1 || missing+=("$package")
  done
  if ((${#missing[@]})); then
    dnf install -y --setopt=install_weak_deps=False "${missing[@]}"
    dnf clean all
    rm -rf /var/cache/dnf
  fi
}

devbox_install_fedora_packages() {
  local mode="${1:?usage: devbox_install_fedora_packages MODE}"
  local packages=("${DEVBOX_FEDORA_PACKAGES[@]}")
  case "$mode" in
    vm)
      packages+=("${DEVBOX_FEDORA_VM_PACKAGES[@]}")
      ;;
    preflight) ;;
    *)
      printf 'devbox: unknown Fedora package mode: %s\n' "$mode" >&2
      return 1
      ;;
  esac
  devbox_install_dnf_packages "${packages[@]}"
}

devbox_refresh_dnf_metadata() {
  dnf -q makecache --refresh
}

devbox_verify_dnf_package_resolution() {
  local package output status
  for package in "$@"; do
    status=0
    output="$(dnf -q repoquery --available --qf '%{name}\n' "$package" 2>&1)" \
      || status=$?
    if ((status != 0)); then
      printf 'devbox: cannot query Fedora repositories for %s (dnf exit %s)\n' \
        "$package" "$status" >&2
      if [[ -n "$output" ]]; then
        printf '%s\n' "$output" >&2
      fi
      printf 'devbox: check repository configuration/connectivity before preflight\n' >&2
      return 1
    fi
    if ! grep -qx -- "$package" <<<"$output"; then
      printf 'devbox: Fedora package is unavailable in the configured repositories: %s\n' \
        "$package" >&2
      return 1
    fi
  done
}

# Fail before a version-qualified DNF install when the manifest's Ruby pin is
# not published as a Fedora stable package. Fedora's archive repository keeps
# published builds resolvable, but a pin that no repository provides must stop
# provisioning with actionable guidance instead of a bare DNF "No match for
# argument" that leaves cloud-init and the user provisioner degraded.
require_ruby_package_available() {
  local version="$1" output status=0

  # The runtime check compares `ruby --version`, which reports a plain
  # major.minor.patch version. Reject RPM release suffixes and other
  # qualifiers here instead of reporting them as an unavailable Fedora package.
  if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "devbox: invalid Ruby pin '$version' in lima/tool-versions.json" >&2
    echo "devbox: expected a plain X.Y.Z (major.minor.patch) upstream version without" >&2
    echo "devbox: an RPM release suffix or pre-release qualifier" >&2
    return 1
  fi

  # DNF5 exits successfully with empty output when the spec matches nothing,
  # so a missing exact line, not the exit status alone, means unavailable.
  output="$(dnf repoquery --available \
    --queryformat='%{name}-%{version}' "ruby-${version}" 2>&1)" || status=$?
  if ((status != 0)); then
    echo "devbox: cannot query Fedora repositories for ruby-${version} (dnf exit ${status})" >&2
    if [[ -n "$output" ]]; then
      printf '%s\n' "$output" >&2
    fi
    echo "devbox: check repository configuration/connectivity before provisioning Ruby" >&2
    return 1
  fi
  if ! grep -Fxq -- "ruby-${version}" <<<"$output"; then
    echo "devbox: ruby-${version} is not available from the configured Fedora repositories" >&2
    echo "devbox: the manifest Ruby pin must be a version Fedora publishes as a stable package;" >&2
    echo "devbox: check Fedora repository metadata (including the fedora-repos-archive" >&2
    echo "devbox: archive repository) or update lima/tool-versions.json to a published version" >&2
    return 1
  fi
}

devbox_verify_manifest_pinned_ruby_available() {
  local manifest="${1:?usage: devbox_verify_manifest_pinned_ruby_available MANIFEST}"
  local version

  if ! version="$(manifest_version ruby)"; then
    printf 'devbox: missing Ruby version pin in %s\n' "$manifest" >&2
    return 1
  fi
  require_ruby_package_available "${version#v}"
}

devbox_install_manifest_pinned_ruby() {
  local manifest="${1:?usage: devbox_install_manifest_pinned_ruby MANIFEST}"
  local version ruby_output

  if ! version="$(manifest_version ruby)"; then
    printf 'devbox: missing Ruby version pin in %s\n' "$manifest" >&2
    return 1
  fi
  version="${version#v}"

  # Fedora's rubypick RPM provides the generic /usr/bin/ruby command and can
  # pull a newer Ruby as a dependency, so allow downgrading to the pin below.
  ruby_output="$(/usr/bin/ruby --version 2>/dev/null)" || ruby_output=""
  if [[ "$ruby_output" != "ruby ${version} "* ]]; then
    # A pin can disappear from current Fedora repodata; fedora-repos-archive is
    # installed with the shared package set for that case, but fail fast when
    # no repository has it. Enforce signature checking for the archive repo
    # explicitly so a pinned archived RPM is still verified if its shipped repo
    # config drifts.
    require_ruby_package_available "${version}"
    dnf install -y --allow-downgrade --setopt=install_weak_deps=False \
      --setopt=updates-archive.gpgcheck=1 \
      "ruby-${version}"
    dnf clean all
    rm -rf /var/cache/dnf
    ruby_output="$(/usr/bin/ruby --version 2>/dev/null)" || ruby_output=""
  fi
  if [[ "$ruby_output" != "ruby ${version} "* ]]; then
    printf 'devbox: Ruby runtime does not match manifest pin %s: %s\n' \
      "$version" "${ruby_output:-unavailable}" >&2
    return 1
  fi
  printf 'fedora-packages: Ruby %s matches the manifest pin\n' "$version"
}
