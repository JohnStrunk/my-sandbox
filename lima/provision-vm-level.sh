#!/bin/bash
# Write the guest-visible devbox VM level marker as root.
#
# Every creation path passes its level explicitly; the marker is descriptive
# only and is not an authorization or isolation boundary. The optional
# DEVBOX_VM_LEVEL_DIR override exists so unit tests can write to a temporary
# directory; production provisioning always uses /etc/devbox.
#
# Keep this writer contract synchronized with probe-readiness.sh: exactly one
# L[1-9][0-9]* line, root:root mode 0644, inside a root:root mode-0755
# /etc/devbox.
set -euo pipefail

level="${1:-${PARAM_VmLevel:-}}"
marker_dir="${DEVBOX_VM_LEVEL_DIR:-/etc/devbox}"
while [[ "$marker_dir" == */ && "$marker_dir" != / ]]; do
  marker_dir="${marker_dir%/}"
done
marker="$marker_dir/vm-level"

fail() {
  echo "devbox vm-level: $*" >&2
  exit 1
}

if [[ ! "$level" =~ ^L[1-9][0-9]*$ ]]; then
  fail "level must match L[1-9][0-9]*; got '$level'"
fi
if [[ "$marker_dir" != /* || "$marker_dir" == / ]]; then
  fail "marker directory must be an absolute non-root path"
fi
if [[ -L "$marker_dir" ]]; then
  fail "refusing symlinked marker directory '$marker_dir'"
fi
marker_dir_real="$(realpath -m -- "$marker_dir")" \
  || fail "cannot resolve marker directory '$marker_dir'"
if [[ "$marker_dir" != "$marker_dir_real" ]]; then
  fail "marker directory must be canonical and not pass through a symlink"
fi
if [[ "$marker_dir_real" == /etc/devbox && "$(id -u)" != 0 ]]; then
  fail "writing $marker_dir requires root"
fi
if [[ -e "$marker_dir" && ! -d "$marker_dir" ]]; then
  fail "marker directory is not a directory: '$marker_dir'"
fi
if [[ ! -d "$marker_dir" ]]; then
  if [[ "$(id -u)" == 0 ]]; then
    install -d -m 0755 -o root -g root -- "$marker_dir"
  else
    install -d -m 0755 -- "$marker_dir"
  fi
fi
if [[ "$marker_dir_real" == /etc/devbox ]]; then
  etc_target="$(findmnt -rn -T /etc -o TARGET)" \
    || fail "cannot inspect the filesystem containing /etc"
  marker_dir_target="$(findmnt -rn -T "$marker_dir" -o TARGET)" \
    || fail "cannot inspect the filesystem containing $marker_dir"
  if [[ "$etc_target" != "$marker_dir_target" ]]; then
    fail "refusing marker directory on a different filesystem than /etc"
  fi
  marker_fs_type="$(findmnt -rn -T "$marker_dir" -o FSTYPE)" \
    || fail "cannot inspect the filesystem type for $marker_dir"
  case "$marker_fs_type" in
    9p | virtiofs)
      fail "refusing host-shared marker filesystem '$marker_fs_type'"
      ;;
  esac
  chown root:root "$marker_dir"
  chmod 0755 "$marker_dir"
  dir_metadata="$(stat -c '%u:%g:%a' -- "$marker_dir")"
  if [[ "$dir_metadata" != "0:0:755" ]]; then
    fail "$marker_dir has $dir_metadata; expected root:root mode 0755"
  fi
else
  chmod 0755 "$marker_dir"
fi
if [[ -L "$marker" ]]; then
  fail "refusing symlinked VM level marker '$marker'"
fi
if [[ -e "$marker" && ! -f "$marker" ]]; then
  fail "VM level marker is not a regular file: '$marker'"
fi

marker_tmp=""
stamp_tmp=""
cleanup_temp_files() {
  if [[ -n "$marker_tmp" ]]; then
    rm -f -- "$marker_tmp"
  fi
  if [[ -n "$stamp_tmp" ]]; then
    rm -f -- "$stamp_tmp"
  fi
}
trap cleanup_temp_files EXIT

marker_tmp="$(mktemp "$marker_dir/.vm-level.XXXXXX")"
printf '%s\n' "$level" >"$marker_tmp"
if [[ "$(id -u)" == 0 ]]; then
  chown root:root "$marker_tmp"
fi
chmod 0644 "$marker_tmp"
mv -f -- "$marker_tmp" "$marker"
marker_tmp=""

if [[ -L "$marker" || ! -f "$marker" ]]; then
  fail "VM level marker was not written as a regular file"
fi
if [[ "$(cat "$marker")" != "$level" ]]; then
  fail "VM level marker content did not match '$level'"
fi

# Record the embedded helper digest for the guest provisioning fingerprint.
if [[ "$marker_dir_real" == /etc/devbox ]]; then
  state_dir=/var/lib/devbox-vm
  if [[ -L "$state_dir" ]]; then
    fail "refusing symlinked state directory '$state_dir'"
  fi
  install -d -m 0755 -o root -g root "$state_dir"
  stamp="$state_dir/vm-level-provision.sha256"
  stamp_tmp="$(mktemp "$stamp.XXXXXX")"
  sha256sum "$0" | awk '{print $1}' >"$stamp_tmp"
  chmod 0644 "$stamp_tmp"
  mv -f -- "$stamp_tmp" "$stamp"
  stamp_tmp=""
fi
trap - EXIT
