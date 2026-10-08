#!/usr/bin/env bash
# Require local rootless Podman rather than a configured remote connection.

podman_local_preflight() {
  local default_connection rootless
  unset DOCKER_HOST CONTAINER_HOST CONTAINER_CONNECTION PODMAN_HOST

  if ! command -v podman >/dev/null 2>&1; then
    printf 'Podman mode requires the podman command, but it was not found on PATH\n' >&2
    return 1
  fi
  if ! default_connection="$(podman system connection list \
    --format '{{if .Default}}{{.Name}}{{end}}')"; then
    printf 'Podman local preflight: could not inspect configured system connections (see Podman diagnostic above)\n' >&2
    return 1
  fi
  if [[ -n "$default_connection" ]]; then
    printf 'Podman local preflight: a system connection is configured as the default; local smoke requires no default system connection. Review podman system connection list before retrying.\n' >&2
    return 1
  fi
  if ! rootless="$(podman info --format '{{.Host.Security.Rootless}}')"; then
    printf 'Podman local preflight: podman info failed (see the Podman diagnostic above)\n' >&2
    return 1
  fi
  if [[ "$rootless" != true ]]; then
    printf 'Podman mode requires rootless Podman, but it reports rootless=%s\n' \
      "$rootless" >&2
    return 1
  fi
  return 0
}
