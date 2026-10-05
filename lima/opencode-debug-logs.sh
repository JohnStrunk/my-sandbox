#!/usr/bin/env bash
# Persist the managed OpenCode debug preference in dedicated devbox L1 state.
set -euo pipefail
umask 077

cleanup_temporary() {
  if [[ -n "${temporary:-}" ]]; then
    rm -f -- "$temporary"
  fi
}

requested="${1:-}"
case "$requested" in
  unchanged | enabled | disabled) ;;
  *)
    echo "opencode-debug-logs: expected unchanged, enabled, or disabled" >&2
    exit 2
    ;;
esac

state_file="$HOME/.local/state/opencode/devbox-debug-logs"
current=disabled
if [[ -e "$state_file" || -L "$state_file" ]]; then
  if [[ ! -f "$state_file" || -L "$state_file" ]]; then
    echo "devbox: refusing to read an unsafe OpenCode debug preference file; remove $state_file to recover" >&2
    exit 1
  fi
  current="$(cat "$state_file")"
  case "$current" in
    enabled | disabled) ;;
    *)
      echo "devbox: invalid OpenCode debug preference; remove $state_file to recover" >&2
      exit 1
      ;;
  esac
fi

effective="$requested"
if [[ "$requested" == unchanged ]]; then
  effective="$current"
fi

log_file="${XDG_DATA_HOME:-$HOME/.local/share}/opencode/log/opencode.log"
if [[ -e "$log_file" || -L "$log_file" ]]; then
  if [[ -L "$log_file" || ! -f "$log_file" ]]; then
    echo "devbox: refusing to change permissions on an unsafe OpenCode log '$log_file'" >&2
    exit 1
  fi
  chmod 600 -- "$log_file"
fi

prepare_debug_log() {
  local log_dir="${log_file%/*}"
  if [[ -L "$log_dir" || ( -e "$log_dir" && ! -d "$log_dir" ) ]]; then
    echo "devbox: refusing to prepare an unsafe OpenCode log directory '$log_dir'" >&2
    return 1
  fi
  (umask 077; mkdir -p -- "$log_dir")
  chmod 700 -- "$log_dir"
  python3 - "$log_file" <<'PY'
import os
import stat
import sys

path = sys.argv[1]
flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK
flags |= getattr(os, "O_NOFOLLOW", 0)
try:
    descriptor = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("OpenCode log path is not a regular file")
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
except OSError as error:
    raise SystemExit(f"devbox: could not secure OpenCode log file: {error}")
PY
}
prepare_debug_log

service_config="${XDG_CONFIG_HOME:-$HOME/.config}/opencode/service.json"
if [[ -e "$service_config" || -L "$service_config" ]]; then
  if [[ -L "$service_config" || ! -f "$service_config" ]]; then
    echo "devbox: refusing to inspect an unsafe OpenCode service config '$service_config'" >&2
    exit 1
  fi
  if ! configured_matches="$(python3 - "$service_config" "$effective" <<'PY'
import json
import pathlib
import sys

try:
    config = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
    service_env = config.get("env", {})
    if not isinstance(service_env, dict):
        raise ValueError("invalid service env map")
    value = service_env.get("OPENCODE_LOG_LEVEL")
    if value is not None and not isinstance(value, str):
        raise ValueError("invalid OPENCODE_LOG_LEVEL value")
except (AttributeError, OSError, ValueError, json.JSONDecodeError):
    raise SystemExit(2)

if value is None:
    compatible = True
else:
    level = value.lower()
    if sys.argv[2] == "enabled":
        compatible = level == "debug"
    else:
        compatible = level not in {"all", "trace", "debug"}
print("compatible" if compatible else "conflict")
PY
  )"; then
    echo "devbox: could not safely inspect the OpenCode service log-level override" >&2
    exit 1
  fi
  if [[ "$configured_matches" == conflict ]]; then
    echo "devbox: the OpenCode service config sets a conflicting OPENCODE_LOG_LEVEL; remove or adjust it with 'opencode service unset env OPENCODE_LOG_LEVEL' before retrying" >&2
    exit 1
  fi
fi

service_matches_effective_level() {
  python3 - "$HOME/.local/state/opencode/service.json" "$effective" <<'PY'
import json
import pathlib
import sys

registration = pathlib.Path(sys.argv[1])
effective = sys.argv[2]
if registration.is_symlink() or not registration.is_file():
    raise SystemExit(1)
try:
    service = json.loads(registration.read_text(encoding="utf-8"))
    pid = service.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        raise ValueError("invalid service pid")
    proc = pathlib.Path("/proc") / str(pid)
    command = (proc / "cmdline").read_bytes().split(b"\0")
    environment = (proc / "environ").read_bytes().split(b"\0")
except (AttributeError, OSError, ValueError, json.JSONDecodeError):
    raise SystemExit(1)

if not all(
    any(part in argument for argument in command)
    for part in (b"opencode", b"serve", b"--service")
):
    raise SystemExit(1)

log_level = None
for entry in environment:
    if entry.startswith(b"OPENCODE_LOG_LEVEL="):
        log_level = entry.partition(b"=")[2].lower()
        break

if effective == "enabled":
    matches = log_level == b"debug"
else:
    matches = log_level not in {b"all", b"trace", b"debug"}
raise SystemExit(0 if matches else 1)
PY
}

if ! service_status="$(opencode service status)"; then
  echo "devbox: could not query managed OpenCode service status" >&2
  exit 1
fi
status_lower="${service_status,,}"
if [[ -z "$service_status" ]]; then
  echo "devbox: could not determine managed OpenCode service status" >&2
  exit 1
fi

service_running=false
if [[ "$status_lower" != stopped ]]; then
  service_running=true
fi
service_matches=false
if [[ "$service_running" == true ]] && service_matches_effective_level; then
  service_matches=true
fi

if [[ "$requested" == unchanged ]]; then
  if [[ "$service_running" == true && "$service_matches" != true ]]; then
    echo "devbox: the running OpenCode service does not match the stored debug preference; no flag leaves the service unchanged. Use --debug or --no-debug to apply the requested level." >&2
    exit 1
  fi
  printf "%s\n" "$effective"
  exit 0
fi

if [[ "$service_running" == true && "$service_matches" != true ]]; then
  if ! opencode service stop >&2; then
    echo "devbox: could not stop the running OpenCode service; the debug preference was not changed. Retry after checking the service." >&2
    exit 1
  fi
  echo "devbox: stopped the running OpenCode service because its log level did not match the requested setting; active sessions may have been interrupted. The next OpenCode command will start it with debug logging $requested." >&2
fi

if [[ "$requested" == enabled ]]; then
  if [[ "$current" != enabled ]]; then
    mkdir -p "$(dirname "$state_file")"
    temporary="$(mktemp "${state_file}.XXXXXX")"
    trap cleanup_temporary EXIT
    printf "%s\n" enabled >"$temporary"
    chmod 600 "$temporary"
    mv -f -- "$temporary" "$state_file"
    trap - EXIT
  fi
else
  rm -f -- "$state_file"
fi

if [[ "$service_matches" == true ]]; then
  echo "devbox: managed OpenCode service already has debug logging $requested." >&2
else
  echo "devbox: configured managed OpenCode debug logging as $requested; it takes effect when the service starts." >&2
fi
printf "%s\n" "$requested"
