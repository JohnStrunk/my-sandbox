#!/bin/bash
# System provisioning for the devbox Lima VM. Runs as root on every VM
# start, before the user script. Every step is idempotent, so a tool-version
# bump in the shared manifest is applied on restart without rebuilding the VM.
set -euo pipefail

DEVBOX_USER="{{.User}}"
DEVBOX_GUEST_HOME="$(getent passwd "$DEVBOX_USER" | cut -d: -f6)"
DEVBOX_REPO="${PARAM_RepoPath:-}"
DEVBOX_SRC_ROOT="${PARAM_SrcPath:-}"
DEVBOX_KB_PATH="${PARAM_KbPath:-}"
if [[ -z "$DEVBOX_GUEST_HOME" || ! -d "$DEVBOX_GUEST_HOME" ]]; then
  echo "devbox: could not locate the guest home for $DEVBOX_USER" >&2
  exit 1
fi
DEVBOX_GUEST_HOME_REAL="$(realpath -e -- "$DEVBOX_GUEST_HOME")"
if [[ "$DEVBOX_SRC_ROOT" != /* || ! -d "$DEVBOX_SRC_ROOT" ]]; then
  echo "devbox: SrcPath must be the absolute guest-visible ~/src mount" >&2
  exit 1
fi
if [[ "$DEVBOX_REPO" != /* || ! -d "$DEVBOX_REPO" ]]; then
  echo "devbox: RepoPath must be an absolute path to the my-sandbox checkout" >&2
  exit 1
fi
if [[ "$DEVBOX_KB_PATH" != /* || "$DEVBOX_KB_PATH" == "/" ]]; then
  echo "devbox: KbPath must be an absolute path to the host knowledge-base checkout" >&2
  exit 1
fi
DEVBOX_SRC_ROOT="$(realpath -e -- "$DEVBOX_SRC_ROOT")"
DEVBOX_REPO="$(realpath -e -- "$DEVBOX_REPO")"
repo_prefix="${DEVBOX_SRC_ROOT%/}/"
case "$DEVBOX_REPO/" in
  "$repo_prefix"*) ;;
  *) echo "devbox: RepoPath must be inside SrcPath" >&2; exit 1 ;;
esac
MANIFEST_SOURCE="$DEVBOX_REPO/container/tool-versions.json"
if [[ -L "$MANIFEST_SOURCE" || ! -f "$MANIFEST_SOURCE" ]]; then
  echo "devbox: tool manifest must be a regular file inside RepoPath" >&2
  exit 1
fi
if [[ "$(realpath -e -- "$MANIFEST_SOURCE")" != "$MANIFEST_SOURCE" ]]; then
  echo "devbox: tool manifest must not resolve through a symlink" >&2
  exit 1
fi

manifest_version() {
  local tool="$1" version
  version="$(jq -er --arg tool "$tool" '.tools[$tool].version' "$MANIFEST")"
  version="${version#v}"
  if [[ ! "$version" =~ ^[0-9]+(\.[0-9]+){1,3}([+-][A-Za-z0-9.]+)?$ ]]; then
    echo "devbox: invalid version for manifest tool '$tool'" >&2
    return 1
  fi
  printf '%s\n' "$version"
}

manifest_checksum() {
  local tool="$1" arch="$2" checksum
  checksum="$(jq -er --arg tool "$tool" --arg arch "$arch" \
    '.tools[$tool].checksums[$arch]' "$MANIFEST")"
  if [[ ! "$checksum" =~ ^[0-9a-f]{64}$ ]]; then
    echo "devbox: invalid checksum for manifest tool '$tool' ($arch)" >&2
    return 1
  fi
  printf '%s\n' "$checksum"
}

manifest_arch() {
  case "$(uname -m)" in
    x86_64) printf '%s\n' amd64 ;;
    aarch64) printf '%s\n' arm64 ;;
    *)
      echo "devbox: unsupported architecture: $(uname -m)" >&2
      return 1
      ;;
  esac
}

verify_download() {
  local tool="$1" arch="$2" path="$3" checksum
  checksum="$(manifest_checksum "$tool" "$arch")"
  printf '%s  %s\n' "$checksum" "$path" | sha256sum -c -
}

# Copy a repository input through no-follow directory/file descriptors. The
# shared source mount is writable by the host, so check-then-cp would race.
copy_repo_file() {
  /usr/bin/python3 -I -S - "$DEVBOX_REPO" "$1" "$2" <<'PY'
import os
import pathlib
import shutil
import stat
import sys
import tempfile


def open_directory(path: str) -> int:
    if not os.path.isabs(path):
        raise ValueError(f"expected absolute directory path: {path}")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in pathlib.PurePosixPath(path).parts[1:]:
            if part in {"", ".", ".."}:
                raise ValueError(f"non-canonical path component: {part!r}")
            next_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=fd,
            )
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


repo, relative, destination = sys.argv[1:]
parts = pathlib.PurePosixPath(relative).parts
if not parts or any(part in {"", ".", ".."} for part in parts):
    raise ValueError(f"unsafe repository-relative path: {relative}")

repo_fd = open_directory(repo)
source_parent_fd = repo_fd
temporary_path = None
try:
    for part in parts[:-1]:
        next_fd = os.open(
            part,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=source_parent_fd,
        )
        if source_parent_fd != repo_fd:
            os.close(source_parent_fd)
        source_parent_fd = next_fd

    source_fd = os.open(
        parts[-1],
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        dir_fd=source_parent_fd,
    )
    if not stat.S_ISREG(os.fstat(source_fd).st_mode):
        os.close(source_fd)
        raise ValueError(f"repository input is not a regular file: {relative}")

    output_fd, temporary_path = tempfile.mkstemp(
        prefix=".devbox-copy-", dir=os.path.dirname(destination)
    )
    with os.fdopen(source_fd, "rb") as source, os.fdopen(output_fd, "wb") as target:
        shutil.copyfileobj(source, target)
        target.flush()
        os.fchmod(target.fileno(), 0o644)
        os.fsync(target.fileno())
    os.replace(temporary_path, destination)
    temporary_path = None
finally:
    if source_parent_fd != repo_fd:
        os.close(source_parent_fd)
    os.close(repo_fd)
    if temporary_path is not None:
        os.unlink(temporary_path)
PY
}

PROVISION_TMP="$(mktemp -d /tmp/devbox-provision.XXXXXX)"
new_temp_dir() {
  mktemp -d "$PROVISION_TMP/item.XXXXXX"
}
cleanup_temp_dirs() {
  rm -rf -- "$PROVISION_TMP"
}
trap cleanup_temp_dirs EXIT

# Read a root-owned snapshot so a concurrent edit on the shared mount cannot
# change versions or digests halfway through this privileged install.
if [[ ! -x /usr/bin/python3 ]]; then
  dnf install -y --setopt=install_weak_deps=False python3
fi
MANIFEST="$PROVISION_TMP/tool-versions.json"
copy_repo_file container/tool-versions.json "$MANIFEST"
if [[ -L "$MANIFEST" || "$(stat -c %s "$MANIFEST")" -gt 1048576 ]]; then
  echo "devbox: copied tool manifest is symlinked or too large" >&2
  exit 1
fi
/usr/bin/python3 -I -S - "$MANIFEST" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as manifest_file:
        manifest = json.load(manifest_file)
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit(f"devbox: invalid tool manifest: {exc}")
if not isinstance(manifest, dict) or not isinstance(manifest.get("tools"), dict):
    raise SystemExit("devbox: tool manifest must contain an object-valued tools field")
PY

# Keep root-owned stamps outside the guest user's writable home and shared tree.
if [[ -L /var/lib/devbox-vm ]]; then
  echo "devbox: refusing symlinked /var/lib/devbox-vm state directory" >&2
  exit 1
fi
install -d -m 0755 -o root -g root /var/lib/devbox-vm
install -d -m 0755 /etc/devbox
if [[ ! -L /etc/devbox/tool-versions.json ]] \
  || [[ "$(readlink /etc/devbox/tool-versions.json)" != "$MANIFEST_SOURCE" ]]; then
  ln -sfn "$MANIFEST_SOURCE" /etc/devbox/tool-versions.json
fi
manifest_snapshot=/var/lib/devbox-vm/tool-versions.json
if ! cmp -s "$MANIFEST" "$manifest_snapshot"; then
  install -m 0644 "$MANIFEST" "$manifest_snapshot.new"
  mv -f "$manifest_snapshot.new" "$manifest_snapshot"
fi
tool_script_snapshot=/var/lib/devbox-vm/provision-tools.sh
tool_assets_dir=/var/lib/devbox-vm/tool-assets
profile_file=/etc/profile.d/devbox-toolchain.sh
profile_tmp="$(new_temp_dir)/devbox-toolchain.sh"
cat >"$profile_tmp" <<'EOF'
export GOPATH="${GOPATH:-$HOME/.cache/go}"
export GOCACHE="${GOCACHE:-$GOPATH/build-cache}"
export CARGO_HOME="${CARGO_HOME:-$HOME/.cargo}"
export RUSTUP_HOME="${RUSTUP_HOME:-/var/lib/devbox-toolbuilder/.rustup}"
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/var/lib/devbox-toolbuilder/.cache/ms-playwright}"
export PATH="$HOME/.local/bin:$CARGO_HOME/bin:/var/lib/devbox-toolbuilder/.local/bin:/var/lib/devbox-toolbuilder/.cargo/bin:/usr/local/node/bin:/usr/local/go/bin:$GOPATH/bin:$PATH"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$HOME/.cache/uv}"
export HF_HOME="${HF_HOME:-/var/lib/devbox-toolbuilder/.cache/semble/huggingface}"
export SEMBLE_CACHE_LOCATION="${SEMBLE_CACHE_LOCATION:-$HOME/.cache/semble/index}"
export PLAYWRIGHT_MCP_BROWSER="${PLAYWRIGHT_MCP_BROWSER:-chromium}"
export DOCKER_HOST="unix:///run/user/$(id -u)/podman/podman.sock"
export KIND_EXPERIMENTAL_PROVIDER="${KIND_EXPERIMENTAL_PROVIDER:-podman}"
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
EOF
if ! cmp -s "$profile_tmp" "$profile_file"; then
  install -m 0644 "$profile_tmp" "$profile_file"
fi

# --- Fedora packages -------------------------------------------------------
# Mirrors the container toolchain and includes Playwright's headless Chromium
# libraries plus the nested-VM/operator runtime. Lima itself provides the
# subuid/subgid ranges, cgroup-v2 delegation, and linger for rootless Podman.
packages=(
  aardvark-dns
  alsa-lib
  at-spi2-atk
  atk
  ca-certificates
  cups-libs
  curl
  diffutils
  difftastic
  edk2-ovmf
  acl
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
  libXcomposite
  libXdamage
  libXext
  libXfixes
  libXi
  libXrandr
  libdrm
  libxkbcommon
  make
  mesa-libgbm
  netavark
  nss
  openssl
  patch
  passt
  podman
  procps-ng
  python3
  python3-pip
  qemu-img
  qemu-kvm
  ripgrep
  shadow-utils-subid
  ShellCheck
  slirp4netns
  tokei
  unzip
)
missing=()
for package in "${packages[@]}"; do
  rpm -q "$package" >/dev/null 2>&1 || missing+=("$package")
done
if ((${#missing[@]})); then
  dnf install -y --setopt=install_weak_deps=False "${missing[@]}"
  dnf clean all
  rm -rf /var/cache/dnf
fi

# The guest user needs access to the passed-through KVM device for nested L2s.
usermod --append --groups kvm "$DEVBOX_USER"
modprobe kvm || true
case "$(uname -m)" in
  x86_64) modprobe kvm_intel || modprobe kvm_amd || true ;;
esac

# Google Cloud CLI is from Google's signed RPM repository, matching the image.
# Skip RPM scriptlets so third-party package code never executes as root.
if ! rpm -q google-cloud-cli >/dev/null 2>&1; then
  case "$(manifest_arch)" in
    amd64) gcloud_arch=x86_64 ;;
    arm64) gcloud_arch=aarch64 ;;
  esac
  repo_file=/etc/yum.repos.d/google-cloud-sdk.repo
  cat >"$repo_file" <<EOF
[google-cloud-cli]
name=Google Cloud CLI
baseurl=https://packages.cloud.google.com/yum/repos/cloud-sdk-el9-${gcloud_arch}
enabled=1
gpgcheck=1
repo_gpgcheck=0
gpgkey=https://packages.cloud.google.com/yum/doc/yum-key.gpg
       https://packages.cloud.google.com/yum/doc/rpm-package-key.gpg
EOF
  dnf install -y --setopt=install_weak_deps=False --setopt=tsflags=noscripts \
    google-cloud-cli
  rm -f "$repo_file"
  dnf clean all
  rm -rf /var/cache/dnf
fi

# Snapshot the live provisioner and wrapper code without following symlinks in
# the writable host checkout. Root executes only the copied script as builder.
copy_repo_file lima/provision-tools.sh "$tool_script_snapshot"
install -d -m 0755 -o root -g root "$tool_assets_dir"
copy_repo_file container/devbox-go "$tool_assets_dir/devbox-go"
copy_repo_file lima/check_toolchain.py "$tool_assets_dir/check_toolchain.py"
copy_repo_file container/semble "$tool_assets_dir/semble"
tool_script_sha256="$(sha256sum "$tool_script_snapshot" | awk '{print $1}')"
printf '%s\n' "$tool_script_sha256" \
  >/var/lib/devbox-vm/tool-provision.sha256.new
mv -f /var/lib/devbox-vm/tool-provision.sha256.new \
  /var/lib/devbox-vm/tool-provision.sha256
tool_assets_sha256="$(sha256sum "$tool_assets_dir/devbox-go" \
  "$tool_assets_dir/check_toolchain.py" "$tool_assets_dir/semble" \
  | sha256sum | awk '{print $1}')"
printf '%s\n' "$tool_assets_sha256" \
  >/var/lib/devbox-vm/tool-assets.sha256.new
mv -f /var/lib/devbox-vm/tool-assets.sha256.new \
  /var/lib/devbox-vm/tool-assets.sha256

# Give only the guest UID access through the parent of every host-config
# mount. The package builder is a distinct account and cannot traverse it.
TOOL_BUILDER_USER=devbox-toolbuilder
TOOL_BUILDER_HOME=/var/lib/devbox-toolbuilder
if [[ -L "$TOOL_BUILDER_HOME" ]]; then
  echo "devbox: refusing symlinked toolbuilder home" >&2
  exit 1
fi
if ! id "$TOOL_BUILDER_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "$TOOL_BUILDER_HOME" \
    --shell /sbin/nologin "$TOOL_BUILDER_USER"
fi
if [[ "$(getent passwd "$TOOL_BUILDER_USER" | cut -d: -f6)" != "$TOOL_BUILDER_HOME" ]]; then
  echo "devbox: unexpected home for $TOOL_BUILDER_USER" >&2
  exit 1
fi
if [[ "$(id -u "$TOOL_BUILDER_USER")" == "$(id -u "$DEVBOX_USER")" ]]; then
  echo "devbox: toolbuilder must have a distinct uid from $DEVBOX_USER" >&2
  exit 1
fi
chown "$TOOL_BUILDER_USER":"$(id -gn "$TOOL_BUILDER_USER")" "$TOOL_BUILDER_HOME"
chmod 0755 "$TOOL_BUILDER_HOME"

protect_guest_mount_parent() {
  local parent="$1" label="$2"
  case "$parent" in
    / | /home | /root | /var | /usr | /tmp | /mnt | /run | /opt | /etc | /srv)
      echo "devbox: refusing to protect broad $label parent '$parent'" >&2
      exit 1
      ;;
  esac
  if [[ "$(realpath -m -- "$parent")" != "$parent" || -L "$parent" ]]; then
    echo "devbox: $label parent must be canonical and not a symlink" >&2
    exit 1
  fi
  if [[ ! -d "$parent" ]]; then
    install -d -m 0700 -o root -g root "$parent"
  fi
  if findmnt -rn -M "$parent" >/dev/null 2>&1; then
    echo "devbox: refusing to change permissions on mounted $label parent '$parent'" >&2
    exit 1
  fi
  chown root:root "$parent"
  chmod 0700 "$parent"
  setfacl --remove-all "$parent"
  setfacl --modify "u:$(id -u "$DEVBOX_USER"):--x" "$parent"
  runuser -u "$DEVBOX_USER" -- /usr/bin/test -x "$parent" \
    || { echo "devbox: guest cannot traverse $label parent '$parent'" >&2; exit 1; }
  if (cd / && runuser -u "$TOOL_BUILDER_USER" -- /usr/bin/test -x "$parent"); then
    echo "devbox: toolbuilder can traverse protected $label parent '$parent'" >&2
    exit 1
  fi
}

host_config_parent="$DEVBOX_GUEST_HOME/.host-config"
protect_guest_mount_parent "$host_config_parent" "host-config"
src_alias_parent="$(dirname "$DEVBOX_SRC_ROOT")"
src_alias_parent_real="$(realpath -m -- "$src_alias_parent")"
case "$src_alias_parent_real/" in
  "$DEVBOX_GUEST_HOME_REAL/"*)
    echo "devbox: SrcPath parent must not be inside the guest home" >&2
    exit 1
    ;;
esac
protect_guest_mount_parent "$src_alias_parent" "SrcPath"

if ! findmnt -rn -t 9p,virtiofs -o TARGET | grep -Fxq -- "$DEVBOX_SRC_ROOT"; then
  echo "devbox: source mount is not live at '$DEVBOX_SRC_ROOT'" >&2
  exit 1
fi
if (cd / && runuser -u "$TOOL_BUILDER_USER" -- /usr/bin/test -x "$DEVBOX_SRC_ROOT"); then
  echo "devbox: toolbuilder can traverse the source mount" >&2
  exit 1
fi

# Linked worktrees in ~/kb store an absolute host-side gitdir in their .git
# files. Recreate that location as an alias to the protected mount in-guest.
kb_mount_target="$DEVBOX_GUEST_HOME/.host-config/kb"
if [[ -L "$DEVBOX_KB_PATH" ]]; then
  if [[ "$(readlink "$DEVBOX_KB_PATH")" != "$kb_mount_target" ]]; then
    echo "devbox: refusing unexpected KbPath alias symlink" >&2
    exit 1
  fi
elif [[ -e "$DEVBOX_KB_PATH" ]]; then
  echo "devbox: refusing to replace existing KbPath path" >&2
  exit 1
else
  ln -s "$kb_mount_target" "$DEVBOX_KB_PATH"
fi
if [[ ! -d "$kb_mount_target" ]]; then
  echo "devbox: protected knowledge-base mount is missing" >&2
  exit 1
fi
if ! findmnt -rn -t 9p,virtiofs -o TARGET | grep -Fxq -- "$kb_mount_target"; then
  echo "devbox: knowledge-base mount is not live at '$kb_mount_target'" >&2
  exit 1
fi

TOOL_BUILDER_HF_HOME="$TOOL_BUILDER_HOME/.cache/semble/huggingface"
TOOL_BUILDER_PLAYWRIGHT_HOME="$TOOL_BUILDER_HOME/.cache/ms-playwright"
as_toolbuilder() {
  (
    cd /
    runuser -u "$TOOL_BUILDER_USER" -- env -i \
      HOME="$TOOL_BUILDER_HOME" \
      USER="$TOOL_BUILDER_USER" \
      LOGNAME="$TOOL_BUILDER_USER" \
      PATH="$TOOL_BUILDER_HOME/.local/bin:$TOOL_BUILDER_HOME/.cargo/bin:/usr/local/node/bin:/usr/local/go/bin:/usr/local/bin:/usr/bin:/bin" \
      UV_CACHE_DIR="$TOOL_BUILDER_HOME/.cache/uv" \
      CARGO_HOME="$TOOL_BUILDER_HOME/.cargo" \
      RUSTUP_HOME="$TOOL_BUILDER_HOME/.rustup" \
      PLAYWRIGHT_BROWSERS_PATH="$TOOL_BUILDER_PLAYWRIGHT_HOME" \
      HF_HOME="$TOOL_BUILDER_HF_HOME" \
      "$@"
  )
}

# --- Rootless Podman and Kubernetes host settings --------------------------
# Lima's boot script configures the static subordinate IDs, user-manager
# delegation, and linger. The VM itself can safely use netavark bridges, so
# prepare the forwarding/sysctl values once here rather than using the
# container devbox's pasta/netavark capability preflight.
sysctl_file=/etc/sysctl.d/90-devbox-kubernetes.conf
sysctl_tmp="$(new_temp_dir)/sysctl.conf"
cat >"$sysctl_tmp" <<'EOF'
net.ipv4.conf.default.arp_notify = 1
net.ipv4.conf.default.rp_filter = 2
net.ipv4.ip_forward = 1
net.ipv6.conf.default.accept_dad = 0
net.ipv6.conf.default.accept_ra = 0
net.ipv6.conf.all.forwarding = 1
fs.inotify.max_user_instances = 8192
fs.inotify.max_user_watches = 524288
fs.inotify.max_queued_events = 65536
kernel.pid_max = 4194304
EOF
if ! cmp -s "$sysctl_tmp" "$sysctl_file"; then
  install -m 0644 "$sysctl_tmp" "$sysctl_file"
fi
sysctl --system >/dev/null

systemd_dropin_dir=/etc/systemd/system/user@.service.d
install -d -m 0755 "$systemd_dropin_dir"
systemd_tmp="$(new_temp_dir)/user.conf"
cat >"$systemd_tmp" <<'EOF'
[Service]
TasksMax=infinity
LimitNOFILE=1048576
EOF
if ! cmp -s "$systemd_tmp" "$systemd_dropin_dir/90-devbox-kubernetes.conf"; then
  install -m 0644 "$systemd_tmp" "$systemd_dropin_dir/90-devbox-kubernetes.conf"
  systemctl daemon-reload
fi

containers_dropin=/etc/containers/containers.conf.d/90-devbox-kubernetes.conf
install -d -m 0755 "$(dirname "$containers_dropin")"
containers_tmp="$(new_temp_dir)/containers.conf"
cat >"$containers_tmp" <<'EOF'
[containers]
netns = "bridge"

[network]
network_backend = "netavark"
EOF
if ! cmp -s "$containers_tmp" "$containers_dropin"; then
  install -m 0644 "$containers_tmp" "$containers_dropin"
fi

# --- Architecture and pinned release binaries ------------------------------
arch="$(manifest_arch)"
case "$arch" in
  amd64)
    go_arch=amd64
    lima_arch=x86_64
    hadolint_arch=x86_64
    agy_arch=x64
    ;;
  arm64)
    go_arch=arm64
    lima_arch=aarch64
    hadolint_arch=arm64
    agy_arch=arm64
    ;;
  esac

NODE_VERSION="$(manifest_version node)"
case "$arch" in
  amd64) node_arch=x64 ;;
  arm64) node_arch=arm64 ;;
esac
node_version_installed="$(as_toolbuilder /usr/local/node/bin/node --version \
  2>/dev/null || true)"
if [[ "$node_version_installed" != "v$NODE_VERSION" ]]; then
  node_archive="node-v${NODE_VERSION}-linux-${node_arch}.tar.xz"
  tmp="$(new_temp_dir)"
  curl --retry 3 --retry-connrefused -fsSL \
    "https://nodejs.org/dist/v${NODE_VERSION}/${node_archive}" \
    -o "$tmp/$node_archive"
  verify_download node "$arch" "$tmp/$node_archive"
  tar -C "$tmp" -xJf "$tmp/$node_archive"
  node_dir="/usr/local/lib/devbox-node-v${NODE_VERSION}-${node_arch}"
  if [[ ! -x "$node_dir/bin/node" ]]; then
    mv "$tmp/node-v${NODE_VERSION}-linux-${node_arch}" "$node_dir"
  fi
  ln -sfnT "$node_dir" /usr/local/node
fi

UV_VERSION="$(manifest_version uv)"
uv_installed="$(as_toolbuilder /usr/local/bin/uv --version 2>/dev/null \
  | awk '{print $2}' || true)"
if [[ "$uv_installed" != "$UV_VERSION" ]]; then
  case "$arch" in
    amd64) uv_arch=x86_64 ;;
    arm64) uv_arch=aarch64 ;;
  esac
  uv_dir="uv-${uv_arch}-unknown-linux-gnu"
  tmp="$(new_temp_dir)"
  curl --retry 3 --retry-connrefused -fsSL \
    "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${uv_dir}.tar.gz" \
    -o "$tmp/uv.tar.gz"
  verify_download uv "$arch" "$tmp/uv.tar.gz"
  tar -C "$tmp" -xzf "$tmp/uv.tar.gz"
  install -m 0755 "$tmp/$uv_dir/uv" /usr/local/bin/uv
  install -m 0755 "$tmp/$uv_dir/uvx" /usr/local/bin/uvx
fi

HADOLINT_VERSION="$(manifest_version hadolint)"
hadolint_installed="$(as_toolbuilder /usr/local/bin/hadolint \
  --version 2>/dev/null | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n1 || true)"
if [[ "$hadolint_installed" != "$HADOLINT_VERSION" ]]; then
  tmp="$(new_temp_dir)"
  curl --retry 3 --retry-connrefused -fsSL \
    "https://github.com/hadolint/hadolint/releases/download/v${HADOLINT_VERSION}/hadolint-linux-${hadolint_arch}" \
    -o "$tmp/hadolint"
  verify_download hadolint "$arch" "$tmp/hadolint"
  install -m 0755 "$tmp/hadolint" /usr/local/bin/hadolint
fi

GO_VERSION="$(manifest_version go)"
go_installed="$(as_toolbuilder /usr/local/go/bin/go version \
  2>/dev/null | sed -n 's/.*go\([0-9.]*\).*/\1/p' || true)"
if [[ "$go_installed" != "$GO_VERSION" ]]; then
  tmp="$(new_temp_dir)"
  curl --retry 3 --retry-connrefused -fsSL \
    "https://go.dev/dl/go${GO_VERSION}.linux-${go_arch}.tar.gz" \
    -o "$tmp/go.tgz"
  verify_download go "$arch" "$tmp/go.tgz"
  tar -C "$tmp" -xzf "$tmp/go.tgz"
  rm -rf /usr/local/go
  mv "$tmp/go" /usr/local/go
fi
if [[ ! -L /usr/local/bin/go ]] \
  || [[ "$(readlink /usr/local/bin/go)" != /usr/local/go/bin/go ]]; then
  ln -sfn /usr/local/go/bin/go /usr/local/bin/go
fi

LIMACTL_VERSION="$(manifest_version limactl)"
limactl_stamp=/usr/local/share/devbox-vm/limactl.version
limactl_installed="$(cat "$limactl_stamp" 2>/dev/null || true)"
if [[ ! -x /usr/local/bin/limactl || "$limactl_installed" != "$LIMACTL_VERSION" ]]; then
  tmp="$(new_temp_dir)"
  curl --retry 3 --retry-connrefused -fsSL \
    "https://github.com/lima-vm/lima/releases/download/v${LIMACTL_VERSION}/lima-${LIMACTL_VERSION}-Linux-${lima_arch}.tar.gz" \
    -o "$tmp/limactl.tgz"
  verify_download limactl "$arch" "$tmp/limactl.tgz"
  tar -C /usr/local -xzf "$tmp/limactl.tgz"
  install -d -m 0755 "$(dirname "$limactl_stamp")"
  printf '%s\n' "$LIMACTL_VERSION" >"$limactl_stamp.new"
  mv "$limactl_stamp.new" "$limactl_stamp"
fi

ANTIGRAVITY_VERSION="$(manifest_version antigravity_cli)"
agy_installed="$(as_toolbuilder /usr/local/bin/agy --version \
  2>/dev/null | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n1 || true)"
if [[ "$agy_installed" != "$ANTIGRAVITY_VERSION" ]]; then
  tmp="$(new_temp_dir)"
  curl --retry 3 --retry-connrefused -fsSL \
    "https://github.com/google-antigravity/antigravity-cli/releases/download/${ANTIGRAVITY_VERSION}/agy_cli_linux_${agy_arch}.tar.gz" \
    -o "$tmp/agy.tgz"
  verify_download antigravity_cli "$arch" "$tmp/agy.tgz"
  tar -C "$tmp" -xzf "$tmp/agy.tgz" antigravity
  install -m 0755 "$tmp/antigravity" /usr/local/bin/agy
fi

ACLI_VERSION="$(manifest_version acli)"
acli_installed="$(as_toolbuilder /usr/local/bin/acli --version \
  2>/dev/null || true)"
if [[ "$acli_installed" != *"$ACLI_VERSION"* ]]; then
  tmp="$(new_temp_dir)"
  curl --retry 3 --retry-connrefused -fLsS \
    "https://acli.atlassian.com/linux/${ACLI_VERSION}/acli_${ACLI_VERSION}_linux_${arch}.tar.gz" \
    -o "$tmp/acli.tgz"
  verify_download acli "$arch" "$tmp/acli.tgz"
  tar -C "$tmp" -xzf "$tmp/acli.tgz"
  install -m 0755 "$tmp/acli_${ACLI_VERSION}_linux_${arch}/acli" \
    /usr/local/bin/acli
fi

KIND_VERSION="$(manifest_version kind)"
kind_installed="$(as_toolbuilder /usr/local/bin/kind version \
  2>/dev/null | grep -Eo 'v[0-9]+\.[0-9]+\.[0-9]+' | head -n1 \
  | sed 's/^v//' || true)"
if [[ "$kind_installed" != "$KIND_VERSION" ]]; then
  tmp="$(new_temp_dir)"
  curl --retry 3 --retry-connrefused -fsSL \
    "https://github.com/kubernetes-sigs/kind/releases/download/v${KIND_VERSION}/kind-linux-${arch}" \
    -o "$tmp/kind"
  verify_download kind "$arch" "$tmp/kind"
  install -m 0755 "$tmp/kind" /usr/local/bin/kind
fi

KUBECTL_VERSION="$(manifest_version kubectl)"
kubectl_installed="$(as_toolbuilder /usr/local/bin/kubectl \
  version --client -o json 2>/dev/null \
  | jq -r '.clientVersion.gitVersion // ""' | sed 's/^v//' || true)"
if [[ "$kubectl_installed" != "$KUBECTL_VERSION" ]]; then
  tmp="$(new_temp_dir)"
  curl --retry 3 --retry-connrefused -fsSL \
    "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/${arch}/kubectl" \
    -o "$tmp/kubectl"
  verify_download kubectl "$arch" "$tmp/kubectl"
  install -m 0755 "$tmp/kubectl" /usr/local/bin/kubectl
fi

HELM_VERSION="$(manifest_version helm)"
helm_installed="$(as_toolbuilder /usr/local/bin/helm version --short \
  2>/dev/null | grep -Eo 'v[0-9]+\.[0-9]+\.[0-9]+' | head -n1 \
  | sed 's/^v//' || true)"
if [[ "$helm_installed" != "$HELM_VERSION" ]]; then
  tmp="$(new_temp_dir)"
  curl --retry 3 --retry-connrefused -fsSL \
    "https://get.helm.sh/helm-v${HELM_VERSION}-linux-${arch}.tar.gz" \
    -o "$tmp/helm.tgz"
  verify_download helm "$arch" "$tmp/helm.tgz"
  tar -C "$tmp" -xzf "$tmp/helm.tgz"
  install -m 0755 "$tmp/linux-${arch}/helm" /usr/local/bin/helm
fi

# ast-grep release assets are checksum-verified against the live manifest.
AST_GREP_VERSION="$(manifest_version ast_grep)"
ast_grep_installed="$(as_toolbuilder /usr/local/bin/ast-grep \
  --version 2>/dev/null | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n1 || true)"
if [[ "$ast_grep_installed" != "$AST_GREP_VERSION" ]]; then
  tmp="$(new_temp_dir)"
  case "$arch" in
    amd64) ast_grep_arch=x86_64 ;;
    arm64) ast_grep_arch=aarch64 ;;
  esac
  curl --retry 3 --retry-connrefused -fsSL \
    "https://github.com/ast-grep/ast-grep/releases/download/${AST_GREP_VERSION}/app-${ast_grep_arch}-unknown-linux-gnu.zip" \
    -o "$tmp/ast-grep.zip"
  verify_download ast_grep "$arch" "$tmp/ast-grep.zip"
  unzip -q "$tmp/ast-grep.zip" -d "$tmp/bin"
  install -m 0755 "$tmp/bin/ast-grep" /usr/local/bin/ast-grep
  install -m 0755 "$tmp/bin/sg" /usr/local/bin/sg
fi

# Run all language-package installers and automatic tool version commands with
# a clean environment and no access to the host credential mounts.
as_toolbuilder /bin/bash "$tool_script_snapshot" \
  "$manifest_snapshot" "$DEVBOX_GUEST_HOME" \
  "$(id -u "$DEVBOX_USER")" "$tool_assets_dir" "$DEVBOX_SRC_ROOT"
toolchain_manifest_sha256="$(sha256sum "$manifest_snapshot" | awk '{print $1}')"
toolchain_stamp_tmp="$(mktemp /var/lib/devbox-vm/toolchain-manifest.sha256.XXXXXX)"
printf '%s\n' "$toolchain_manifest_sha256" >"$toolchain_stamp_tmp"
chmod 0644 "$toolchain_stamp_tmp"
mv -f "$toolchain_stamp_tmp" /var/lib/devbox-vm/toolchain-manifest.sha256

# Record the exact embedded system provisioner that just ran. The user script
# combines this with its own embedded-script digest and the shared manifest.
system_script_sha256="$(sha256sum "$0" | awk '{print $1}')"
if [[ "$(cat /var/lib/devbox-vm/system-provision.sha256 2>/dev/null || true)" \
  != "$system_script_sha256" ]]; then
  stamp_tmp="$(mktemp /var/lib/devbox-vm/system-provision.sha256.XXXXXX)"
  printf '%s\n' "$system_script_sha256" >"$stamp_tmp"
  chmod 0644 "$stamp_tmp"
  mv -f "$stamp_tmp" /var/lib/devbox-vm/system-provision.sha256
fi
