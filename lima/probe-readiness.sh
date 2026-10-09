#!/bin/bash
# Readiness probe for the devbox Lima VM, run as the guest user.
set -euo pipefail

fail() {
  echo "devbox readiness: $*" >&2
  exit 1
}

TOOL_BUILDER_HOME=/var/lib/devbox-toolbuilder
PATH="$HOME/.local/bin:$HOME/.cargo/bin:$TOOL_BUILDER_HOME/.local/bin:$TOOL_BUILDER_HOME/.cargo/bin:/usr/local/node/bin:/usr/local/go/bin:/usr/local/bin:$PATH"
export PATH
export UV_CACHE_DIR="$HOME/.cache/uv"
export CARGO_HOME="$HOME/.cargo"
export RUSTUP_HOME="$TOOL_BUILDER_HOME/.rustup"
export HF_HOME="$TOOL_BUILDER_HOME/.cache/semble/huggingface"
export SEMBLE_CACHE_LOCATION="$HOME/.cache/semble/index"
export PLAYWRIGHT_BROWSERS_PATH="$TOOL_BUILDER_HOME/.cache/ms-playwright"
export PLAYWRIGHT_MCP_BROWSER=chromium
RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
DOCKER_SOCKET=/var/run/docker.sock
ROOTLESS_DOCKER_SOCKET="$RUNTIME_DIR/docker.sock"
PODMAN_SOCKET="$RUNTIME_DIR/podman/podman.sock"
[[ "$DOCKER_SOCKET" != "$PODMAN_SOCKET" \
  && "$ROOTLESS_DOCKER_SOCKET" != "$PODMAN_SOCKET" ]] \
  || fail "Docker and Podman must use different API sockets"

OPENCODE_TMP=/tmp/opencode
OPENCODE_TMP_PROBE_DIR=""
cleanup_opencode_tmp_probe() {
  if [[ -n "$OPENCODE_TMP_PROBE_DIR" ]]; then
    rm -f -- "$OPENCODE_TMP_PROBE_DIR/sentinel" 2>/dev/null || true
    rmdir -- "$OPENCODE_TMP_PROBE_DIR" 2>/dev/null || true
  fi
}
trap cleanup_opencode_tmp_probe EXIT

verify_opencode_tmp() {
  local current_user metadata task_dir sentinel expected actual
  current_user="$(id -un)"
  if [[ -L "$OPENCODE_TMP" || ! -d "$OPENCODE_TMP" ]]; then
    fail "$OPENCODE_TMP is not a directory for '$current_user'; "\
      "recreate the VM from the current template"
  fi
  local mount_check_status=0
  /usr/bin/python3 -I -S - "$OPENCODE_TMP" \
    >/dev/null 2>&1 <<'PY' || mount_check_status=$?
import sys

target = sys.argv[1]
try:
    with open("/proc/self/mountinfo", encoding="utf-8") as mountinfo:
        for line in mountinfo:
            fields = line.split()
            if len(fields) < 5:
                raise SystemExit(2)
            if fields[4] == target:
                raise SystemExit(0)
except UnicodeError:
    raise SystemExit(2)
except OSError:
    raise SystemExit(2)
raise SystemExit(1)
PY
  case "$mount_check_status" in
    0)
      fail "$OPENCODE_TMP is a mountpoint for '$current_user'; "\
        "recreate the VM from the current template"
      ;;
    1) ;;
    *)
      fail "cannot inspect mount status for $OPENCODE_TMP "\
        "(exit $mount_check_status)"
      ;;
  esac
  if ! metadata="$(
    stat -c '%u:%g:%a' -- "$OPENCODE_TMP" 2>/dev/null
  )"; then
    fail "cannot inspect $OPENCODE_TMP for '$current_user'; "\
      "recreate the VM from the current template"
  fi
  if [[ "$metadata" != "0:0:1777" ]]; then
    fail "$OPENCODE_TMP has $metadata; expected root:root mode 01777 "\
      "for '$current_user'; recreate the VM from the current template"
  fi

  if ! task_dir="$(
    mktemp -d "$OPENCODE_TMP/readiness.XXXXXXXX" 2>/dev/null
  )"; then
    fail "user '$current_user' cannot create a private task dir under "\
      "$OPENCODE_TMP; recreate the VM from the current template"
  fi
  OPENCODE_TMP_PROBE_DIR="$task_dir"
  if [[ "$(stat -c '%a' -- "$task_dir" 2>/dev/null)" != 700 ]]; then
    fail "user '$current_user' did not get a private mode-0700 task "\
      "directory under $OPENCODE_TMP"
  fi

  sentinel="$task_dir/sentinel"
  expected="devbox-readiness-$RANDOM-$$"
  if ! printf '%s\n' "$expected" >"$sentinel" 2>/dev/null; then
    fail "user '$current_user' cannot write to $OPENCODE_TMP"
  fi
  if ! read -r actual <"$sentinel" || [[ "$actual" != "$expected" ]]; then
    fail "user '$current_user' cannot read from $OPENCODE_TMP"
  fi
  if ! rm -f -- "$sentinel" 2>/dev/null \
    || ! rmdir -- "$task_dir" 2>/dev/null; then
    fail "user '$current_user' cannot clean up its task dir under "\
      "$OPENCODE_TMP"
  fi
  OPENCODE_TMP_PROBE_DIR=""
}
verify_opencode_tmp

for cert in redhat-ipa-ca.crt redhat-rhcsv2-ca.crt redhat-root-ca.crt; do
  test -s "/etc/pki/ca-trust/source/anchors/$cert" \
    || fail "Red Hat CA trust anchor $cert is missing"
done
test -r /etc/ssl/certs/ca-certificates.crt \
  || fail "the system CA bundle is missing"

for tool in \
  acli agy cargo diff difft fd file ffmpeg docker gh glab gcloud gws \
  hadolint helm hyperfine jq just kind kubectl limactl make minikube \
  markdownlint-cli2 newgidmap newuidmap \
  node npm npx opencode patch pipenv pip3 \
  playwright-cli podman pre-commit python3 qemu-img repomix rg \
  rustc rustup semble shellcheck tokei uv uvx virsh virt-host-validate
do
  command -v "$tool" >/dev/null 2>&1 || fail "$tool is not installed"
done

# Version commands ran under the isolated package builder. Check its stamp
# instead of executing third-party tools in this probe.
manifest_sha256="$(
  sha256sum /etc/devbox/tool-versions.json | awk '{print $1}'
)"
builder_manifest_sha256="$(
  cat /var/lib/devbox-vm/toolchain-manifest.sha256 \
    2>/dev/null || true
)"
[[ "$builder_manifest_sha256" == "$manifest_sha256" ]] \
  || fail "isolated manifest-pinned toolchain check is missing or stale"

test -x "$HOME/.local/bin/devbox-go" \
  || fail "devbox-go wrapper is not installed"
test -x /usr/local/bin/sg \
  || fail "ast-grep compatibility command sg is not installed"
test -x "$HOME/.local/bin/semble" \
  || fail "Semble wrapper is not installed"
test -d "$PLAYWRIGHT_BROWSERS_PATH" \
  || fail "Playwright Chromium is not installed"
test -d "$HF_HOME" || fail "Semble HF_HOME is not VM-local"
test -d "$HOME/.local/share/kubebuilder-envtest" \
  || fail "the VM-local setup-envtest asset cache is missing"
test -r "$HOME/.agents/skills/devbox-tools/SKILL.md" \
  || fail "VM-owned devbox-tools skill is not staged"
test -r "$HOME/.agents/skills/ast-grep/SKILL.md" \
  || fail "ast-grep skill is not staged"
test -r "$HOME/.agents/skills/ast-grep-outline/SKILL.md" \
  || fail "ast-grep-outline skill is not staged"
grep -Eq '^[0-9a-f]{64}$' \
  "$HOME/.local/share/devbox-toolchain/provisioning.fingerprint" \
  || fail "the toolchain fingerprint is missing or invalid"

# The host must pass KVM through, and the guest user must be in kvm.
test -e /dev/kvm \
  || fail "/dev/kvm is missing (host nested virtualization?)"
case "$(uname -m)" in
  x86_64)
    grep -qm1 -w vmx /proc/cpuinfo \
      || grep -qm1 -w svm /proc/cpuinfo \
      || fail "no vmx/svm CPU flag: host nested virtualization is off"
    ;;
esac
getent group kvm | grep -qw "$(id -un)" \
  || fail "the devbox user is not in the kvm group"
getent group libvirt | grep -qw "$(id -un)" \
  || fail "the devbox user is not in the libvirt group "\
    "required for Minikube KVM2"
systemctl is-enabled --quiet virtqemud.socket \
  || fail "the libvirt QEMU socket is not enabled"
systemctl is-active --quiet virtqemud.socket \
  || fail "the libvirt QEMU socket is not active"
systemctl is-enabled --quiet virtnetworkd.socket \
  || fail "the libvirt network socket is not enabled"
systemctl is-active --quiet virtnetworkd.socket \
  || fail "the libvirt network socket is not active"
virsh -c qemu:///system list --all >/dev/null 2>&1 \
  || fail "the guest user cannot access the system libvirt QEMU API"
virsh -c qemu:///system net-list --all >/dev/null 2>&1 \
  || fail "the system libvirt network API is unavailable"
if ! virt_host_validation="$(virt-host-validate qemu 2>&1)"; then
  printf '%s\n' "$virt_host_validation" >&2
  fail "libvirt cannot use nested KVM; check /dev/kvm "\
    "and host virtualization"
fi

# Lima sets subordinate IDs and cgroup delegation for rootless Podman.
check_subordinate_ids() {
  local file="$1"
  awk -F: -v user="$(id -un)" \
    '$1 == user { total += $3 } END { exit !(total >= 65536) }' "$file" \
    || fail "$file has fewer than 65,536 subordinate IDs for $(id -un)"
}
check_subordinate_ids /etc/subuid
check_subordinate_ids /etc/subgid
for unit in containerd.service docker.socket docker.service; do
  systemctl is-enabled --quiet "$unit" \
    || fail "the rootful Docker system unit $unit is not enabled"
  systemctl is-active --quiet "$unit" \
    || fail "the rootful Docker system unit $unit is not active"
done
if ! docker_daemon_pid="$(
  systemctl show --property=MainPID --value docker.service
)"; then
  fail "cannot inspect the rootful Docker system service process"
fi
[[ "$docker_daemon_pid" =~ ^[1-9][0-9]*$ ]] \
  || fail "the rootful Docker system service has no valid MainPID"
if ! docker_daemon_uid="$(stat -c '%u' "/proc/$docker_daemon_pid")"; then
  fail "cannot inspect the rootful Docker process owner"
fi
[[ "$docker_daemon_uid" == 0 ]] \
  || fail "the Docker system service process is not owned by root"
getent group docker >/dev/null \
  || fail "the rootful Docker socket group is missing"
id -nG | tr ' ' '\n' | grep -qx docker \
  || fail "guest session lacks rootful docker-group access"
if systemctl --user is-active --quiet docker.service \
  || systemctl --user is-enabled --quiet docker.service; then
  fail "obsolete rootless Docker user unit is enabled or active; "\
    "recreate the VM"
fi
[[ ! -S "$ROOTLESS_DOCKER_SOCKET" ]] \
  || fail "obsolete rootless Docker socket is present; recreate the VM"
test -S "$DOCKER_SOCKET" \
  || fail "the rootful Docker socket is not available"
docker_socket_metadata="$(stat -c '%U:%G:%a' "$DOCKER_SOCKET")"
[[ "$docker_socket_metadata" == root:docker:660 ]] \
  || fail "Docker socket has $docker_socket_metadata; expected "\
    "root:docker:660"
[[ -r "$DOCKER_SOCKET" && -w "$DOCKER_SOCKET" ]] \
  || fail "the guest user cannot access the rootful Docker socket"
test -S "$PODMAN_SOCKET" \
  || fail "the rootless Podman API socket is not available"
[[ "$(stat -fc %T /sys/fs/cgroup)" == cgroup2fs ]] \
  || fail "the VM is not using cgroup v2"
check_sysctl() {
  local name="$1" expected="$2" actual
  actual="$(sysctl -n "$name" 2>/dev/null)" \
    || fail "could not read sysctl $name"
  [[ "$actual" == "$expected" ]] \
    || fail "sysctl $name is $actual; expected $expected"
}
check_sysctl net.ipv4.conf.default.route_localnet 0
check_sysctl net.ipv4.conf.default.arp_notify 1
check_sysctl net.ipv4.conf.default.rp_filter 2
check_sysctl net.ipv4.ip_forward 1
check_sysctl net.ipv6.conf.default.accept_dad 0
check_sysctl net.ipv6.conf.default.accept_ra 0
check_sysctl net.ipv6.conf.all.forwarding 1
check_sysctl fs.inotify.max_user_instances 8192
check_sysctl fs.inotify.max_user_watches 524288
check_sysctl fs.inotify.max_queued_events 65536
check_sysctl kernel.pid_max 4194304
user_service="user@$(id -u).service"
task_limit="$(systemctl show -p TasksMax --value "$user_service")"
[[ "$task_limit" == infinity ]] \
  || fail "user service task limit is $task_limit"
fd_limit="$(systemctl show -p LimitNOFILE --value "$user_service")"
[[ "$fd_limit" == 1048576 ]] \
  || fail "user service NOFILE limit is $fd_limit"
if ! docker_version_json="$(
  curl --fail --silent --show-error --max-time 5 \
    --unix-socket "$DOCKER_SOCKET" http://d/version 2>/dev/null
)"; then
  fail "the Docker CE API at $DOCKER_SOCKET is not responding"
fi
if ! docker_server_version="$(
  jq -er '.Version' <<<"$docker_version_json"
)"; then
  fail "the Docker CE API did not report a server version"
fi
if ! expected_docker_version="$(
  jq -er '.tools.docker_ce.version | sub("^v"; "")' \
    /etc/devbox/tool-versions.json
)"; then
  fail "the Docker CE version pin is missing from the tool manifest"
fi
[[ "$docker_server_version" == "$expected_docker_version" ]] \
  || fail "Docker server is $docker_server_version; "\
    "expected Docker CE $expected_docker_version"
curl --fail --silent --show-error --max-time 5 \
  --unix-socket "$PODMAN_SOCKET" http://d/_ping 2>/dev/null \
  | grep -qx OK \
  || fail "the separate rootless Podman API is not responding"

# Mounts use virtiofs or the documented 9p fallback. Host-shared paths sit
# behind root-owned parents traversable only by the guest UID.
verify_guest_only_parent() {
  local parent="$1" label="$2" acl
  [[ "$(stat -c %U "$parent")" == root ]] \
    || fail "$label parent is not root-owned"
  acl="$(getfacl --omit-header --numeric "$parent")"
  grep -Fxq "user:$(id -u):--x" <<<"$acl" \
    || fail "$label parent does not grant guest traversal"
  grep -Fxq 'other::---' <<<"$acl" \
    || fail "$label parent is accessible to other users"
}
verify_guest_only_parent "$HOME/.host-config" "host-config"
src_parent="$(dirname "${PARAM_SrcPath:-/nonexistent}")"
verify_guest_only_parent "$src_parent" "SrcPath"
for rel in \
  .config/acli \
  .config/gcloud \
  .config/gh \
  .config/gws \
  .config/opencode \
  .local/share/opencode \
  .local/state/opencode \
  kb \
  src
do
  target=""
  case "$rel" in
    src) target="${PARAM_SrcPath:-}" ;;
    .config/*) target="$HOME/.host-config/config/${rel#.config/}" ;;
    .local/share/opencode)
      target="$HOME/.host-config/local/share/opencode"
      ;;
    .local/state/opencode)
      target="$HOME/.host-config/local/state/devbox-opencode"
      ;;
    kb) target="$HOME/.host-config/kb" ;;
  esac
  [[ -n "$target" ]] \
    || fail "the shared mount for '~/${rel}' is not live"
  findmnt -rn -t 9p,virtiofs -o TARGET | grep -Fxq -- "$target" \
    || fail "the shared mount for '~/${rel}' is not at '$target'"
  if [[ "$target" == "$HOME/$rel" ]]; then
    [[ -d "$HOME/$rel" && ! -L "$HOME/$rel" ]] \
      || fail "'~/${rel}' is not the live mount"
  else
    [[ -L "$HOME/$rel" && "$(readlink "$HOME/$rel")" == "$target" ]] \
      || fail "'~/${rel}' is not linked to its shared mount"
  fi
done

kb_alias="${PARAM_KbPath:-}"
kb_alias_target="$(readlink "$kb_alias" 2>/dev/null || true)"
[[ -L "$kb_alias" && "$kb_alias_target" == "$HOME/.host-config/kb" ]] \
  || fail "KB worktree path alias is incorrect"
[[ -r "$HOME/kb/kbase.py" ]] \
  || fail "knowledge base is not readable via canonical path '~/kb'; "\
    "verify the ~/kb host mount"

findmnt -rn -t 9p,virtiofs -o TARGET \
  | grep -Fxq -- "$HOME/.host-config/agents" \
  || fail "the read-only host skill mount is not live"
[[ -d "$HOME/.agents" && ! -L "$HOME/.agents" ]] \
  || fail "$HOME/.agents is not the guest-local skill overlay"

# The host seed is read-only; persistent state is unique to the L1 VM and
# protected from the toolbuilder by the root-owned .host-config parent.
state_seed_mount="$HOME/.host-config/local/state/opencode-seed"
findmnt -rn -M "$state_seed_mount" -t 9p,virtiofs >/dev/null \
  || fail "read-only OpenCode state seed mount is missing"
state_seed_options="$(findmnt -rn -M "$state_seed_mount" -o OPTIONS)" \
  || fail "could not inspect OpenCode state seed mount"
case ",$state_seed_options," in
  *,ro,*) ;;
  *) fail "OpenCode host-state seed mount is not read-only" ;;
esac
state_target="$HOME/.host-config/local/state/devbox-opencode"
findmnt -rn -M "$state_target" -t 9p,virtiofs >/dev/null \
  || fail "persistent L1 OpenCode state mount is missing"
[[ -L "$HOME/.local/state/opencode" \
  && "$(readlink "$HOME/.local/state/opencode")" == "$state_target" ]] \
  || fail "OpenCode state does not point to the persistent L1-only mount"
[[ "$(stat -c %a "$state_target")" == 700 ]] \
  || fail "persistent OpenCode state mount must be mode 0700"

echo "devbox readiness: full toolchain and operator profile are ready"
