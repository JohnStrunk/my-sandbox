#!/usr/bin/env bash

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
      'Check the nested rootless Podman Docker-compatible API.' \
      'Use --require-user-networks to fail when bridge network sysctls are unavailable.'
    exit 0
    ;;
  *)
    printf 'Unknown option: %s\n' "$1" >&2
    exit 2
    ;;
esac

docker_host="${DOCKER_HOST:-}"
if [[ "$docker_host" != unix:///* ]]; then
  printf '%s\n' \
    'Docker API unavailable: DOCKER_HOST is not configured as a Unix socket.' >&2
  exit 1
fi

docker_socket="${docker_host#unix://}"
service_log="${PODMAN_API_LOG:-/tmp/podman-api.log}"
if [[ ! -S "$docker_socket" ]]; then
  printf 'Docker API unavailable: no socket exists at %s.\n' "$docker_socket" >&2
  printf 'The Podman API service is not running; inspect %s.\n' "$service_log" >&2
  exit 1
fi

if ! ping_response="$(
  curl --connect-timeout 1 --max-time 5 --fail --silent --show-error \
    --unix-socket "$docker_socket" \
    http://localhost/_ping 2>&1
)"; then
  printf 'Docker API unavailable: socket %s exists, but the service did not respond.\n' \
    "$docker_socket" >&2
  printf '%s\n' "$ping_response" >&2
  printf 'Inspect %s for the Podman service error.\n' "$service_log" >&2
  exit 1
fi

if [[ "$ping_response" != *OK* ]]; then
  printf 'Docker API unavailable: unexpected /_ping response: %s\n' \
    "$ping_response" >&2
  exit 1
fi

if ! network_info="$(
  timeout 5 podman info --format '{{.Host.Security.Rootless}} {{.Host.NetworkBackend}} {{.Host.RootlessNetworkCmd}}' \
    2>&1
)"; then
  printf '%s\n' \
    'Docker API ready, but nested Podman network inspection failed:' >&2
  printf '%s\n' "$network_info" >&2
  exit 1
fi

read -r rootless network_backend rootless_network_cmd <<< "$network_info"
if [[ "$rootless" != true ]]; then
  printf 'Docker API preflight failed: nested Podman is not rootless (%s).\n' \
    "$rootless" >&2
  exit 1
fi

printf 'Docker API ready at %s (rootless Podman).\n' "$docker_host"

user_networks_available=true
network_failure_details=()
if [[ "$network_backend" != netavark ]]; then
  user_networks_available=false
  network_failure_details+=(
    "network backend is '$network_backend', not the supported netavark backend"
  )
else
  for requirement in \
    net/ipv4/conf/default/route_localnet=1 \
    net/ipv4/conf/default/arp_notify=1 \
    net/ipv4/conf/default/rp_filter=2 \
    net/ipv4/ip_forward=1; do
    sysctl_path="/proc/sys/${requirement%%=*}"
    expected="${requirement#*=}"
    if [[ ! -r "$sysctl_path" ]]; then
      user_networks_available=false
      network_failure_details+=("$sysctl_path is not readable")
      continue
    fi
    actual="$(<"$sysctl_path")"
    actual="${actual//[[:space:]]/}"
    if [[ "$actual" != "$expected" ]]; then
      user_networks_available=false
      network_failure_details+=(
        "$sysctl_path is '$actual' (expected '$expected')"
      )
    fi
  done
fi

if [[ "$rootless_network_cmd" != pasta ]]; then
  printf 'Docker API network warning: rootless network program is %s; pasta is recommended for published ports.\n' \
    "${rootless_network_cmd:-unset}" >&2
fi

if [[ "$user_networks_available" == true ]]; then
  printf '%s\n' \
    'Docker API network preflight passed: user-defined bridge networks are supported.'
elif [[ "$require_user_networks" == true ]]; then
  if [[ "$network_backend" == netavark ]]; then
    printf '%s\n' \
      'Docker API network preflight failed: netavark cannot create user-defined bridge networks with the current outer sysctl state.' \
      'The default pasta network remains available for published ports; recreate the devbox on a host that permits the required outer --sysctl values.' >&2
  else
    printf 'Docker API network preflight failed: user-defined bridge networks require netavark, but the active backend is %s.\n' \
      "$network_backend" >&2
  fi
  for detail in "${network_failure_details[@]}"; do
    printf '  - %s\n' "$detail" >&2
  done
  exit 2
else
  printf '%s\n' \
    'Docker API network preflight limited: user-defined bridge networks are unavailable with the current outer sysctl state.' \
    'The default pasta network remains available for published ports.' >&2
  for detail in "${network_failure_details[@]}"; do
    printf '  - %s\n' "$detail" >&2
  done
fi
