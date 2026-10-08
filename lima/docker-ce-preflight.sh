#!/usr/bin/env bash
# Shared strict identity check for the provisioned rootless Docker CE Engine.

docker_ce_preflight() {
  local runtime_dir socket manifest expected_version owner package_version docker_path
  local client_server daemon_pid daemon_uid current_uid tool item path expected_owner package

  if (($# != 1)); then
    printf 'Docker CE preflight: expected the pinned tool manifest path\n' >&2
    return 1
  fi

  for tool in docker jq rpm systemctl; do
    if ! command -v "$tool" >/dev/null 2>&1; then
      printf 'Docker CE preflight: %s is required but was not found on PATH\n' \
        "$tool" >&2
      return 1
    fi
  done

  manifest="$1"
  if [[ ! -r "$manifest" ]]; then
    printf 'Docker CE preflight: pinned tool manifest is not readable: %s\n' \
      "$manifest" >&2
    return 1
  fi
  if ! expected_version="$(jq -er '.tools.docker_ce.version | sub("^v"; "")' \
    "$manifest")"; then
    printf 'Docker CE preflight: cannot read the docker_ce version from %s (see jq diagnostic above)\n' \
      "$manifest" >&2
    return 1
  fi

  docker_path="$(command -v docker)"
  for item in "${docker_path}:docker-ce-cli" '/usr/bin/dockerd:docker-ce' \
    '/usr/bin/dockerd-rootless.sh:docker-ce-rootless-extras'; do
    local path="${item%%:*}"
    local expected_owner="${item#*:}"
    if ! owner="$(rpm -qf --queryformat '%{NAME}' "$path")"; then
      printf 'Docker CE preflight: cannot verify RPM ownership of %s (see rpm diagnostic above)\n' \
        "$path" >&2
      return 1
    fi
    if [[ "$owner" != "$expected_owner" ]]; then
      printf 'Docker CE preflight: %s is owned by %s, expected %s\n' \
        "$path" "$owner" "$expected_owner" >&2
      return 1
    fi
  done

  for package in docker-ce docker-ce-cli docker-ce-rootless-extras; do
    if ! package_version="$(rpm -q --queryformat '%{VERSION}' "$package")"; then
      printf 'Docker CE preflight: required package %s is missing (see rpm diagnostic above)\n' \
        "$package" >&2
      return 1
    fi
    if [[ "$package_version" != "$expected_version" ]]; then
      printf 'Docker CE preflight: %s is %s, manifest pins %s\n' \
        "$package" "$package_version" "$expected_version" >&2
      return 1
    fi
  done
  if rpm -q podman-docker >/dev/null 2>&1; then
    printf 'Docker CE preflight: podman-docker is installed and can shadow Docker CE\n' >&2
    return 1
  fi
  if systemctl is-active --quiet docker.service \
    || systemctl is-active --quiet docker.socket \
    || systemctl is-active --quiet containerd.service; then
    printf 'Docker CE preflight: rootful Docker/containerd services must remain inactive\n' >&2
    return 1
  fi
  if ! systemctl --user is-active --quiet docker.service; then
    printf 'Docker CE preflight: rootless user service docker.service is not active; start it with systemctl --user start docker.service\n' >&2
    return 1
  fi
  if ! daemon_pid="$(systemctl --user show --property=MainPID --value docker.service)"; then
    printf 'Docker CE preflight: cannot inspect the rootless docker.service process (see systemctl diagnostic above)\n' >&2
    return 1
  fi
  if [[ ! "$daemon_pid" =~ ^[1-9][0-9]*$ ]]; then
    printf 'Docker CE preflight: rootless docker.service has no valid MainPID (%s)\n' \
      "$daemon_pid" >&2
    return 1
  fi
  if ! daemon_uid="$(stat -c '%u' "/proc/${daemon_pid}")"; then
    printf 'Docker CE preflight: cannot verify owner of rootless Docker process %s (see stat diagnostic above)\n' \
      "$daemon_pid" >&2
    return 1
  fi
  current_uid="$(id -u)"
  if [[ "$daemon_uid" != "$current_uid" ]]; then
    printf 'Docker CE preflight: docker.service MainPID %s is owned by uid %s, expected rootless uid %s\n' \
      "$daemon_pid" "$daemon_uid" "$current_uid" >&2
    return 1
  fi

  runtime_dir="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
  socket="${runtime_dir}/docker.sock"
  if [[ ! -S "$socket" ]]; then
    printf 'Docker CE preflight: rootless Docker socket is missing at %s; check docker.service\n' \
      "$socket" >&2
    return 1
  fi
  unset DOCKER_CONTEXT
  export DOCKER_HOST="unix://${socket}"
  if ! client_server="$(docker version --format '{{.Client.Version}}|{{.Server.Version}}')"; then
    printf 'Docker CE preflight: request to %s failed (see Docker diagnostic above)\n' \
      "$DOCKER_HOST" >&2
    return 1
  fi
  if [[ "$client_server" != "${expected_version}|${expected_version}" ]]; then
    printf 'Docker CE preflight: endpoint %s reported client/server %s, expected pinned Docker CE %s|%s\n' \
      "$DOCKER_HOST" "$client_server" "$expected_version" "$expected_version" >&2
    return 1
  fi
  return 0
}
