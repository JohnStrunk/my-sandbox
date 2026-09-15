#!/usr/bin/env bash
# Run a command with host credentials and configuration discovery disabled.
# Podman calls use a separate, explicit runtime-only environment.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'EOF'
Usage: sanitized-test.sh [options] -- command [arg ...]

Run a command with a temporary HOME/XDG tree and a small non-secret
environment allowlist. When Podman is available, calls made by the command
use a shim that restores only the host settings required by rootless Podman.

Options:
  --require-podman  Run a rootless Podman preflight before the command. A
                     missing or unusable runtime is reported as infrastructure
                     failure and the command is not started.
  --podman-probe-image IMAGE
                      Image used by the real-container preflight. Pre-pull a
                      local image and pass its tag to avoid registry access.
  --resource-preflight
                      Report cgroup resource limits and block constrained
                      parallel test commands before they start.
  --resource-cgroup-root DIR
                      Cgroup hierarchy to inspect (defaults to
                      /sys/fs/cgroup; useful for diagnostics and tests).
  -h, --help        Show this help text.
EOF
}

require_podman=false
resource_preflight_enabled=false
resource_cgroup_root="/sys/fs/cgroup"
podman_probe_image="docker.io/library/alpine:3.22"
while (($# > 0)); do
  case "$1" in
    --require-podman)
      require_podman=true
      shift
      ;;
    --podman-probe-image)
      if (($# < 2)) || [[ -z "$2" || "$2" == -* ]]; then
        printf 'sanitized-test: --podman-probe-image requires an image name\n' >&2
        exit 2
      fi
      podman_probe_image="$2"
      shift 2
      ;;
    --resource-preflight)
      resource_preflight_enabled=true
      shift
      ;;
    --resource-cgroup-root)
      if (($# < 2)) || [[ -z "$2" || "$2" == -* ]]; then
        printf 'sanitized-test: --resource-cgroup-root requires a directory\n' >&2
        exit 2
      fi
      resource_cgroup_root="$2"
      shift 2
      ;;
    --)
      shift
      break
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    -* | "")
      printf 'sanitized-test: unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
    *)
      break
      ;;
  esac
done

if (($# == 0)); then
  printf 'sanitized-test: a command is required\n' >&2
  usage >&2
  exit 2
fi

host_path="${PATH:-/usr/local/bin:/usr/bin:/bin}"
host_home="${HOME-}"
host_xdg_config_home="${XDG_CONFIG_HOME-}"
host_xdg_data_home="${XDG_DATA_HOME-}"
host_xdg_runtime_dir="${XDG_RUNTIME_DIR-}"
if [[ -z "$host_xdg_config_home" && -n "$host_home" ]]; then
  host_xdg_config_home="$host_home/.config"
fi
if [[ -z "$host_xdg_data_home" && -n "$host_home" ]]; then
  host_xdg_data_home="$host_home/.local/share"
fi
host_graphroot=""
if [[ -n "$host_xdg_data_home" ]]; then
  host_graphroot="$host_xdg_data_home/containers/storage"
fi
host_runroot=""
if [[ -n "$host_xdg_runtime_dir" ]]; then
  host_runroot="$host_xdg_runtime_dir/containers"
fi
host_containers_config_dir=""
if [[ -n "$host_xdg_config_home" ]]; then
  host_containers_config_dir="$host_xdg_config_home/containers"
fi
podman_path="$(command -v podman || true)"

runtime_root="$(mktemp -d "${TMPDIR:-/tmp}/my-sandbox-sanitized.XXXXXX")" || {
  printf 'sanitized-test: could not create a temporary isolation directory\n' >&2
  exit 125
}
# The cleanup function is invoked by the EXIT trap rather than directly.
# shellcheck disable=SC2329
cleanup() {
  if rm -rf -- "$runtime_root" 2>/dev/null; then
    return 0
  fi
  if [[ -n "${podman_wrapper:-}" && -x "${podman_wrapper:-}" ]] \
    && declare -p safe_env &>/dev/null; then
    env -i -- "${safe_env[@]}" timeout 120 "$podman_wrapper" unshare \
      rm -rf -- "$runtime_root" >/dev/null 2>&1 || true
  fi
  rm -rf -- "$runtime_root" 2>/dev/null || true
}
trap cleanup EXIT

isolated_home="$runtime_root/home"
isolated_xdg_config_home="$runtime_root/xdg/config"
isolated_xdg_data_home="$runtime_root/xdg/share"
isolated_xdg_state_home="$runtime_root/xdg/state"
isolated_xdg_cache_home="$runtime_root/xdg/cache"
isolated_xdg_runtime_dir="$runtime_root/xdg/runtime"
isolated_tmp="$runtime_root/tmp"
isolated_bin="$runtime_root/bin"
podman_home="$runtime_root/podman-home"
podman_config_home="$runtime_root/podman-config"
podman_config_containers_dir="$podman_config_home/containers"
isolated_docker_config="$runtime_root/docker-config"
registry_auth_file="$runtime_root/registry-auth.json"
if ! mkdir -p \
  "$isolated_home" \
  "$isolated_xdg_config_home" \
  "$isolated_xdg_data_home" \
  "$isolated_xdg_state_home" \
  "$isolated_xdg_cache_home" \
  "$isolated_xdg_runtime_dir" \
  "$isolated_tmp" \
  "$isolated_bin" \
  "$podman_home" \
  "$podman_config_containers_dir" \
  "$isolated_docker_config"; then
  printf '%s\n' \
    'sanitized-test: could not create the temporary isolation directories.' \
    'sanitized-test: this is an infrastructure/runtime configuration failure, not a product test failure.' >&2
  exit 125
fi
if ! chmod 700 \
  "$isolated_home" \
  "$podman_home" \
  "$isolated_docker_config" \
  "$isolated_xdg_runtime_dir" \
  "$isolated_tmp"; then
  printf '%s\n' \
    'sanitized-test: could not secure the temporary isolation directories.' \
    'sanitized-test: this is an infrastructure/runtime configuration failure, not a product test failure.' >&2
  exit 125
fi
if ! printf '{}\n' > "$registry_auth_file" \
  || ! chmod 600 "$registry_auth_file"; then
  printf '%s\n' \
    'sanitized-test: could not create the isolated registry auth file.' \
    'sanitized-test: this is an infrastructure/runtime configuration failure, not a product test failure.' >&2
  exit 125
fi

safe_path="$host_path"
podman_wrapper="$isolated_bin/podman"
podman_info_stdout="$runtime_root/podman-info.stdout"
podman_info_stderr="$runtime_root/podman-info.stderr"
podman_probe_stdout="$runtime_root/podman-probe.stdout"
podman_probe_stderr="$runtime_root/podman-probe.stderr"
podman_global_args=()
if [[ -n "$podman_path" ]]; then
  if [[ -n "$host_containers_config_dir" ]]; then
    for config_name in containers.conf storage.conf registries.conf policy.json; do
      config_source="$host_containers_config_dir/$config_name"
      if [[ -f "$config_source" ]] \
        && ! cp -- "$config_source" "$podman_config_containers_dir/"; then
        printf 'sanitized-test: could not copy Podman runtime config %s.\n' \
          "$config_source" >&2
        printf '%s\n' \
          'sanitized-test: this is an infrastructure/runtime configuration failure, not a product test failure.' >&2
        exit 125
      fi
    done
    config_dropins="$host_containers_config_dir/containers.conf.d"
    if [[ -d "$config_dropins" ]] \
      && ! cp -R -- "$config_dropins" "$podman_config_containers_dir/"; then
      printf 'sanitized-test: could not copy Podman config drop-ins %s.\n' \
        "$config_dropins" >&2
      printf '%s\n' \
        'sanitized-test: this is an infrastructure/runtime configuration failure, not a product test failure.' >&2
      exit 125
    fi
  fi
  if [[ -n "$host_graphroot" ]]; then
    podman_global_args+=(--root "$host_graphroot")
  fi
  if [[ -n "$host_runroot" ]]; then
    podman_global_args+=(--runroot "$host_runroot")
  fi
  if ! {
    printf '%s\n' '#!/usr/bin/env bash' 'set -euo pipefail'
    printf 'export PATH=%q\n' "$host_path"
    printf 'export HOME=%q\n' "$podman_home"
    printf 'export XDG_CONFIG_HOME=%q\n' "$podman_config_home"
    printf 'export XDG_DATA_HOME=%q\n' "$isolated_xdg_data_home"
    printf 'export XDG_RUNTIME_DIR=%q\n' "$isolated_xdg_runtime_dir"
    printf 'export TMPDIR=%q\n' "$isolated_tmp"
    printf 'export REGISTRY_AUTH_FILE=%q\n' "$registry_auth_file"
    printf '%s\n' \
      'unset AWS_CONFIG_FILE AWS_SHARED_CREDENTIALS_FILE AZURE_CONFIG_DIR CLOUDSDK_CONFIG' \
      'unset CONTAINERS_CONF CONTAINERS_REGISTRIES_CONF CONTAINERS_STORAGE_CONF' \
      'unset DOCKER_CONFIG DOCKER_AUTH_CONFIG GH_CONFIG_DIR GIT_CONFIG_GLOBAL' \
      'unset GIT_CONFIG_SYSTEM GIT_SSH_COMMAND GOOGLE_APPLICATION_CREDENTIALS' \
      'unset GLAB_CONFIG_DIR KUBECONFIG NETRC NPM_CONFIG_USERCONFIG' \
      'unset OPENCODE_CONFIG OPENCODE_CONFIG_DIR PIP_CONFIG_FILE SSH_AUTH_SOCK' \
      'unset GH_TOKEN GITHUB_TOKEN GEMINI_API_KEY GOOGLE_GENERATIVE_AI_API_KEY' \
      'unset CONTEXT7_API_KEY IGLOO_MCP_COMMUNITY IGLOO_MCP_COMMUNITY_KEY' \
      'unset IGLOO_MCP_APP_PASS IGLOO_MCP_APP_ID IGLOO_MCP_USERNAME' \
      'unset IGLOO_MCP_PASSWORD GITLAB_HOST GITLAB_TOKEN LITEMAAS_API_KEY' \
      'unset OPENAI_API_KEY ANTHROPIC_API_KEY ANTHROPIC_BASE_URL OCTO_OPEN_URL' \
      'unset OCTO_OPEN_KEY PRICETAG_ANTHROPIC_URL PRICETAG_HOSTED_URL' \
      'unset PRICETAG_OPENAI_URL PRICETAG_API_KEY OPENROUTER_API_KEY' \
      'unset GOOGLE_CLOUD_PROJECT VERTEX_LOCATION'
    printf 'export DOCKER_CONFIG=%q\n' "$isolated_docker_config"
    printf 'exec %q' "$podman_path"
    for argument in "${podman_global_args[@]}"; do
      printf ' %q' "$argument"
    done
    printf ' "$@"\n'
  } > "$podman_wrapper"; then
    printf '%s\n' \
      'sanitized-test: could not write the Podman runtime shim.' \
      'sanitized-test: this is an infrastructure/runtime configuration failure, not a product test failure.' >&2
    exit 125
  fi
  if ! chmod 700 "$podman_wrapper"; then
    printf '%s\n' \
      'sanitized-test: could not secure the Podman runtime shim.' \
      'sanitized-test: this is an infrastructure/runtime configuration failure, not a product test failure.' >&2
    exit 125
  fi
  safe_path="$isolated_bin:$host_path"
fi

safe_env=(
  "HOME=$isolated_home"
  "PATH=$safe_path"
  "MY_SANDBOX_SANITIZED_TEST_WRAPPER_ACTIVE=1"
  "TMPDIR=$isolated_tmp"
  "XDG_CONFIG_HOME=$isolated_xdg_config_home"
  "XDG_DATA_HOME=$isolated_xdg_data_home"
  "XDG_STATE_HOME=$isolated_xdg_state_home"
  "XDG_CACHE_HOME=$isolated_xdg_cache_home"
  "XDG_RUNTIME_DIR=$isolated_xdg_runtime_dir"
)
for name in LANG LC_ALL LC_CTYPE TERM CI; do
  if [[ -n "${!name-}" ]]; then
    safe_env+=("$name=${!name}")
  fi
done

resource_preflight() {
  if [[ ! -r "$SCRIPT_DIR/resource_preflight.py" ]]; then
    printf '%s\n' \
      'sanitized-test: resource preflight script is unavailable.' \
      'sanitized-test: this is an infrastructure/runtime configuration failure, not a product test failure.' >&2
    return 125
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' \
      'sanitized-test: python3 is required for the resource preflight.' \
      'sanitized-test: this is an infrastructure/runtime configuration failure, not a product test failure.' >&2
    return 125
  fi
  env -i -- "${safe_env[@]}" python3 "$SCRIPT_DIR/resource_preflight.py" \
    --cgroup-root "$resource_cgroup_root" --fail-on-constrained
}

podman_preflight() {
  local info_error info_output probe_error probe_output rootless_status status
  if [[ -z "$podman_path" ]]; then
    printf '%s\n' \
      'sanitized-test: Podman preflight failed; test command was not run.' \
      'sanitized-test: this is an infrastructure/runtime configuration failure, not a product test failure.' \
      'sanitized-test: podman is not available on PATH.' >&2
    return 125
  fi

  if ! : > "$podman_info_stdout" \
    || ! : > "$podman_info_stderr" \
    || ! : > "$podman_probe_stdout" \
    || ! : > "$podman_probe_stderr"; then
    printf '%s\n' \
      'sanitized-test: could not create Podman preflight diagnostic files.' \
      'sanitized-test: this is an infrastructure/runtime configuration failure, not a product test failure.' >&2
    return 125
  fi

  if env -i -- "${safe_env[@]}" timeout 60 "$podman_wrapper" info \
    --format '{{.Host.Security.Rootless}}' >"$podman_info_stdout" \
    2>"$podman_info_stderr"; then
    status=0
  else
    status=$?
  fi
  info_output="$(<"$podman_info_stdout")"
  info_error="$(<"$podman_info_stderr")"
  rootless_status="${info_output##*$'\n'}"
  if [[ "$status" -ne 0 || "$rootless_status" != "true" ]]; then
    printf '%s\n' \
      'sanitized-test: Podman preflight failed; test command was not run.' \
      'sanitized-test: this is an infrastructure/runtime configuration failure, not a product test failure.' \
      'sanitized-test: only the documented Podman runtime allowlist was restored.' \
      'sanitized-test: expected rootless Podman info to report true.' >&2
    if [[ -n "$info_output" ]]; then
      printf 'sanitized-test: podman info stdout:\n%s\n' "$info_output" >&2
    fi
    if [[ -n "$info_error" ]]; then
      printf 'sanitized-test: podman info stderr:\n%s\n' "$info_error" >&2
    fi
    return 125
  fi

  if env -i -- "${safe_env[@]}" timeout 120 "$podman_wrapper" run \
    --rm --pull=missing "$podman_probe_image" true >"$podman_probe_stdout" \
    2>"$podman_probe_stderr"; then
    return 0
  fi
  probe_output="$(<"$podman_probe_stdout")"
  probe_error="$(<"$podman_probe_stderr")"
  printf '%s\n' \
    'sanitized-test: Podman container preflight failed; test command was not run.' \
    'sanitized-test: rootless Podman info passed, but the probe image could not run.' \
    'sanitized-test: this is an infrastructure or registry configuration failure, not a product test failure.' \
    "sanitized-test: probe image: $podman_probe_image" >&2
  if [[ -n "$probe_output" ]]; then
    printf 'sanitized-test: podman run stdout:\n%s\n' "$probe_output" >&2
  fi
  if [[ -n "$probe_error" ]]; then
    printf 'sanitized-test: podman run stderr:\n%s\n' "$probe_error" >&2
  fi
  return 125
}

if [[ "$resource_preflight_enabled" == true ]]; then
  if resource_preflight; then
    :
  else
    preflight_status=$?
    exit "$preflight_status"
  fi
fi

if [[ "$require_podman" == true ]]; then
  if podman_preflight; then
    :
  else
    preflight_status=$?
    exit "$preflight_status"
  fi
fi

set +e
env -i -- "${safe_env[@]}" "$@"
command_status=$?
set -e
exit "$command_status"
