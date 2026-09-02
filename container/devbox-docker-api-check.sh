#!/usr/bin/env bash
# Diagnose the nested rootless Podman Docker-compatible API.
#
# Distinguishes the two independent ways the API can be unusable:
#   1. The API socket/service itself isn't up (DOCKER_HOST unset, no socket,
#      or the service isn't answering) -- the whole API is unavailable.
#   2. The API is up, but this specific host couldn't configure the default
#      network mode and sysctls nested netavark needs for
#      *user-defined bridge networks* (`podman network create`, or
#      Testcontainers' `Network` class) -- a narrower limitation, not an
#      outage.
#
# Exit codes: 0 = API ready (bridge-network prerequisites too, unless
# --require-user-networks was not given and only the pasta fallback is available);
# 1 = API itself unavailable; 2 = API ready but --require-user-networks was
# given and bridge networks aren't available.
set -euo pipefail

require_user_networks=false
case "${1:-}" in
  "")
    ;;
  --require-user-networks)
    require_user_networks=true
    ;;
  --help | -h)
    printf '%s\n' \
      'Usage: devbox-docker-api-check [--require-user-networks]' \
      '' \
      'Check the nested rootless Podman Docker-compatible API that devbox' \
      "exposes through \$DOCKER_HOST." \
      '' \
      'By default, only the API service itself needs to be ready. Pass' \
      '--require-user-networks to additionally require netavark' \
      'user-defined bridge networks (e.g. Testcontainers Network aliases),' \
      'which need outer-host sysctl support this environment may not have.'
    exit 0
    ;;
  *)
    printf 'Unknown option: %s\n' "$1" >&2
    exit 2
    ;;
esac

service_log="${PODMAN_API_LOG:-/tmp/podman-api.log}"

docker_host="${DOCKER_HOST:-}"
if [[ "$docker_host" != unix://* ]]; then
  printf 'Docker API unavailable: DOCKER_HOST (%s) is not a unix:// socket URL.\n' \
    "${docker_host:-<unset>}" >&2
  exit 1
fi
docker_socket="${docker_host#unix://}"

if [[ ! -S "$docker_socket" ]]; then
  printf 'Docker API unavailable: no socket at %s.\n' "$docker_socket" >&2
  printf 'The Podman API service is not running; see %s.\n' "$service_log" >&2
  exit 1
fi

if ! ping_response="$(
  curl --connect-timeout 1 --max-time 5 --fail --silent --show-error \
    --unix-socket "$docker_socket" http://localhost/_ping 2>&1
)"; then
  printf 'Docker API unavailable: %s exists, but the service did not respond:\n' \
    "$docker_socket" >&2
  printf '%s\n' "$ping_response" >&2
  printf 'See %s for the Podman service log.\n' "$service_log" >&2
  exit 1
fi
if [[ "$ping_response" != *OK* ]]; then
  printf 'Docker API unavailable: unexpected /_ping response: %s\n' "$ping_response" >&2
  exit 1
fi

printf 'Docker API ready at %s (rootless Podman).\n' "$docker_host"

# The bridge-network preflight below is a best-effort read of the sysctl
# values a fresh netavark bridge would need (see the Dockerfile for how
# `devbox` tries to preseed them on the *outer* container). It can't fully
# predict whether an actual `podman network create` will succeed -- the
# authoritative check is trying it -- but it gives a fast, non-mutating
# signal without creating any networks.
required_sysctls=(
  net/ipv4/conf/default/route_localnet=1
  net/ipv4/conf/default/arp_notify=1
  net/ipv4/conf/default/rp_filter=2
  net/ipv4/ip_forward=1
  net/ipv6/conf/default/accept_dad=0
  net/ipv6/conf/default/accept_ra=0
  net/ipv6/conf/all/forwarding=1
)
# Overridable only for the unit tests exercising this script directly; real
# usage always reads the container's actual /proc/sys.
sysctl_root="${DEVBOX_SYSCTL_ROOT:-/proc/sys}"
containers_conf="${DEVBOX_CONTAINERS_CONF:-/sandbox/.config/containers/containers.conf}"
missing=()
for requirement in "${required_sysctls[@]}"; do
  path="$sysctl_root/${requirement%%=*}"
  expected="${requirement#*=}"
  actual="$(tr -d '[:space:]' < "$path" 2>/dev/null || true)"
  if [[ "$actual" != "$expected" ]]; then
    missing+=("$path is '${actual:-unreadable}' (want '$expected')")
  fi
done

network_mode=""
if [[ -r "$containers_conf" ]]; then
  network_mode="$(sed -n \
    's/^[[:space:]]*netns[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' \
    "$containers_conf" | tail -n 1)"
fi
if [[ "$network_mode" != bridge ]]; then
  missing+=(
    "nested default network mode is '${network_mode:-unset}' (want 'bridge' for user-defined networks)"
  )
fi
if [[ "$network_mode" == bridge ]]; then
  nested_sysctl_error=""
  # The single-quoted script is intentionally expanded by the nested shell.
  # shellcheck disable=SC2016
  if ! nested_sysctl_error="$(timeout 5 podman unshare --rootless-netns sh -c '
    set -eu
    for requirement in \
      net/ipv4/conf/default/route_localnet=1 \
      net/ipv4/conf/default/arp_notify=1 \
      net/ipv4/conf/default/rp_filter=2 \
      net/ipv4/ip_forward=1 \
      net/ipv6/conf/default/accept_dad=0 \
      net/ipv6/conf/default/accept_ra=0 \
      net/ipv6/conf/all/forwarding=1; do
      path="/proc/sys/${requirement%%=*}"
      expected="${requirement#*=}"
      actual="$(tr -d "[:space:]" < "$path")"
      if [ "$actual" != "$expected" ]; then
        printf "%s is %s (want %s)\\n" "$path" "$actual" "$expected" >&2
        exit 1
      fi
    done
  ' 2>&1)"; then
    missing+=(
      "nested rootless-network sysctls could not be verified${nested_sysctl_error:+: $nested_sysctl_error}"
    )
  fi
fi

if [[ "$network_mode" == bridge ]]; then
  printf '%s\n' \
    'Docker API default network: netavark bridge (checking user-defined network prerequisites).'
elif [[ "$network_mode" == pasta ]]; then
  printf '%s\n' \
    'Docker API default network: pasta (published ports supported; user-defined bridge networks unavailable).'
else
  printf 'Docker API default network: %s (user-defined networks unavailable; published-port support not verified).\n' \
    "${network_mode:-unset}"
fi
if [[ "$network_mode" == pasta ]]; then
  port_availability='The default pasta network still supports published ports on a single container.'
else
  port_availability='Published ports are not guaranteed until the configured default network prerequisites pass.'
fi

if [[ "${#missing[@]}" -eq 0 ]]; then
  printf '%s\n' \
    'Docker API network preflight passed: user-defined bridge networks look supported.'
  exit 0
fi

if [[ "$require_user_networks" == true ]]; then
  printf '%s\n' \
    'Docker API network preflight failed: user-defined bridge networks (e.g.' \
    'Testcontainers Network aliases) are not available with the current' \
    'nested network configuration.' >&2
  printf '%s\n' "$port_availability" >&2
  for detail in "${missing[@]}"; do
    printf '  - %s\n' "$detail" >&2
  done
  exit 2
fi

printf '%s\n' \
  'Docker API network preflight limited: user-defined bridge networks may not' \
  'be available with the current nested network configuration.' >&2
printf '%s\n' "$port_availability" >&2
for detail in "${missing[@]}"; do
  printf '  - %s\n' "$detail" >&2
done
