#! /bin/bash
# Start nested Podman's Docker-compatible API and keep the devbox alive.

set -euo pipefail

docker_host="${DOCKER_HOST:-}"
service_log="${PODMAN_API_LOG:-/tmp/podman-api.log}"
service_pid=""
docker_socket=""

stop_service() {
  if [[ -n "$service_pid" ]] && kill -0 "$service_pid" 2>/dev/null; then
    kill "$service_pid" 2>/dev/null || true
  fi
  if [[ -n "$service_pid" ]]; then
    for _ in {1..50}; do
      if ! kill -0 "$service_pid" 2>/dev/null; then
        break
      fi
      sleep 0.1
    done
    if kill -0 "$service_pid" 2>/dev/null; then
      kill -KILL "$service_pid" 2>/dev/null || true
    fi
  fi
  if [[ -n "$service_pid" ]]; then
    wait "$service_pid" 2>/dev/null || true
  fi
  service_pid=""
  if [[ -n "$docker_socket" ]]; then
    rm -f "$docker_socket"
  fi
}

cleanup() {
  stop_service
}

trap cleanup EXIT
trap 'exit 143' INT TERM

wait_for_socket() {
  for _ in {1..100}; do
    if [[ -S "$docker_socket" ]]; then
      return 0
    fi
    if [[ -n "$service_pid" ]] && ! kill -0 "$service_pid" 2>/dev/null; then
      return 1
    fi
    sleep 0.1
  done
  return 1
}

start_service() {
  rm -f "$docker_socket"
  podman system service --time=0 "$docker_host" >"$service_log" 2>&1 &
  service_pid=$!
}

service_is_healthy() {
  [[ -S "$docker_socket" ]] || return 1
  curl --connect-timeout 1 --max-time 2 --fail --silent \
    --unix-socket "$docker_socket" http://localhost/_ping >/dev/null 2>&1
}

case "$docker_host" in
  unix:///*)
    docker_socket="${docker_host#unix://}"
    mkdir -p "$(dirname "$docker_socket")"
    mkdir -p "$(dirname "$service_log")"
    rm -f "$docker_socket"

    # devbox writes the dynamic subordinate-ID ranges after starting this
    # container. Starting the service earlier can make Podman cache an
    # unusable namespace configuration, so wait for that handoff marker.
    subid_ready_file="${DEVBOX_SUBID_READY_FILE:-}"
    if [[ -z "$subid_ready_file" ]]; then
      printf 'devbox-entry: subordinate-ID handoff marker is not configured; Docker API disabled.\n' >&2
      sleep infinity
      exit 0
    fi
    for _ in {1..600}; do
      [[ -e "$subid_ready_file" ]] && break
      sleep 0.1
    done
    if [[ ! -e "$subid_ready_file" ]]; then
      printf 'devbox-entry: subordinate-ID handoff marker did not appear; Docker API disabled.\n' >&2
      sleep infinity
      exit 0
    fi

    start_service
    if ! wait_for_socket; then
      printf 'devbox-entry: Podman API service did not create %s; see %s:\n' \
        "$docker_socket" "$service_log" >&2
      cat "$service_log" >&2 || true
      printf '%s\n' \
        'devbox-entry: Docker API is unavailable; regular devbox commands remain available.' >&2
    fi

    # The service is normally long-lived. If it exits or becomes unhealthy,
    # retry so a persistent devbox can recover from a transient service
    # failure without needing to be recreated. A permanently failing service
    # is retried less often after three consecutive failures.
    service_restart_count=0
    while :; do
      sleep 5
      if [[ -z "$service_pid" ]]; then
        continue
      fi
      if service_is_healthy; then
        service_restart_count=0
        continue
      fi

      if [[ "$service_restart_count" -ge 3 ]]; then
        printf 'devbox-entry: Docker API restart backoff enabled; see %s.\n' \
          "$service_log" >&2
        stop_service
        sleep 25
        service_restart_count=0
        start_service
        if ! wait_for_socket; then
          printf 'devbox-entry: Docker API restart after backoff failed; see %s.\n' \
            "$service_log" >&2
        fi
        continue
      fi

      service_restart_count=$((service_restart_count + 1))
      printf 'devbox-entry: restarting unhealthy Docker API service (attempt %s).\n' \
        "$service_restart_count" >&2
      stop_service
      start_service
      if ! wait_for_socket; then
        printf 'devbox-entry: Docker API restart failed; see %s.\n' \
          "$service_log" >&2
      fi
    done
    ;;
  *)
    printf 'devbox-entry: Docker API disabled because DOCKER_HOST is not a unix:// socket (%s).\n' \
      "${docker_host:-<unset>}" >&2
    sleep infinity
    ;;
esac
