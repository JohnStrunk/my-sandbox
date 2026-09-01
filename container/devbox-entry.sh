#!/usr/bin/env bash

set -euo pipefail

docker_host="${DOCKER_HOST:-}"
if [[ -z "$docker_host" ]]; then
  printf '%s\n' "Docker API service cannot start: DOCKER_HOST is not set." >&2
  exit 1
fi

case "$docker_host" in
  unix:///*)
    docker_socket="${docker_host#unix://}"
    ;;
  *)
    printf 'Docker API service cannot start: unsupported DOCKER_HOST: %s\n' \
      "$docker_host" >&2
    exit 1
    ;;
esac

service_log="${PODMAN_API_LOG:-/tmp/podman-api.log}"
service_pid=""
sleep_pid=""
subid_ready_file="${DEVBOX_SUBID_READY_FILE:-}"

print_service_log() {
  if [[ -f "$service_log" ]]; then
    while IFS= read -r line; do
      printf '  %s\n' "$line" >&2
    done < "$service_log"
  fi
}

report_service_failure() {
  printf 'Docker API service failed; see %s for details:\n' "$service_log" >&2
  print_service_log
}

# Called indirectly by the EXIT trap.
# shellcheck disable=SC2317,SC2329
cleanup() {
  if [[ -n "$sleep_pid" ]] && kill -0 "$sleep_pid" 2>/dev/null; then
    kill "$sleep_pid" 2>/dev/null || true
  fi
  if [[ -n "$service_pid" ]] && kill -0 "$service_pid" 2>/dev/null; then
    kill "$service_pid" 2>/dev/null || true
  fi
  if [[ -n "$service_pid" ]]; then
    wait "$service_pid" 2>/dev/null || true
  fi
}

mkdir -p "$(dirname "$docker_socket")"
rm -f "$docker_socket"

if [[ -n "$subid_ready_file" ]]; then
  while [[ ! -e "$subid_ready_file" ]]; do
    sleep 0.1
  done
fi

podman system service --time=0 "$docker_host" >"$service_log" 2>&1 &
service_pid=$!
trap cleanup EXIT
trap 'exit 143' INT TERM

for _ in {1..100}; do
  if [[ -S "$docker_socket" ]]; then
    break
  fi
  if ! kill -0 "$service_pid" 2>/dev/null; then
    report_service_failure
    sleep infinity
    exit 1
  fi
  sleep 0.1
done

if [[ ! -S "$docker_socket" ]]; then
  report_service_failure
  sleep infinity
  exit 1
fi

# Keep the container alive after the service exits so a user can inspect the
# service log and receive the preflight diagnosis from inside the devbox.
sleep infinity &
sleep_pid=$!
set +e
wait "$service_pid"
status=$?
set -e

if ! kill -0 "$service_pid" 2>/dev/null; then
  report_service_failure
fi
wait "$sleep_pid"
exit "$status"
