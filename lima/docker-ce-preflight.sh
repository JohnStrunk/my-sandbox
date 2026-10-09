#!/usr/bin/env bash
# Shared strict identity check for the provisioned rootful Docker CE Engine.

docker_ce_preflight() {
  local manifest expected_version expected_containerd_version
  local owner package_version expected_package_version docker_path
  local daemon_pid daemon_uid client_server
  local tool item path expected_owner package

  if (($# != 1)); then
    printf 'Docker CE preflight: expected the pinned tool manifest path\n' >&2
    return 1
  fi

  for tool in docker jq rpm stat systemctl; do
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
  if ! expected_containerd_version="$(jq -er \
    '.tools.containerd_io.version | sub("^v"; "")' "$manifest")"; then
    printf 'Docker CE preflight: cannot read the containerd_io version from %s (see jq diagnostic above)\n' \
      "$manifest" >&2
    return 1
  fi

  docker_path="$(command -v docker)"
  for item in "${docker_path}:docker-ce-cli" \
    '/usr/bin/dockerd:docker-ce' '/usr/bin/containerd:containerd.io'; do
    path="${item%%:*}"
    expected_owner="${item#*:}"
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

  for package in docker-ce docker-ce-cli containerd.io; do
    if ! package_version="$(rpm -q --queryformat '%{VERSION}' "$package")"; then
      printf 'Docker CE preflight: required package %s is missing (see rpm diagnostic above)\n' \
        "$package" >&2
      return 1
    fi
    case "$package" in
      containerd.io) expected_package_version="$expected_containerd_version" ;;
      *) expected_package_version="$expected_version" ;;
    esac
    if [[ "$package_version" != "$expected_package_version" ]]; then
      printf 'Docker CE preflight: %s is %s, manifest pins %s\n' \
        "$package" "$package_version" "$expected_package_version" >&2
      return 1
    fi
  done
  if rpm -q podman-docker >/dev/null 2>&1; then
    printf 'Docker CE preflight: podman-docker is installed and can shadow Docker CE\n' >&2
    return 1
  fi

  for item in containerd.service docker.socket docker.service; do
    if ! systemctl is-enabled --quiet "$item"; then
      printf 'Docker CE preflight: system unit %s is not enabled\n' "$item" >&2
      return 1
    fi
    if ! systemctl is-active --quiet "$item"; then
      printf 'Docker CE preflight: system unit %s is not active\n' "$item" >&2
      return 1
    fi
  done

  if ! daemon_pid="$(systemctl show --property=MainPID --value docker.service)"; then
    printf 'Docker CE preflight: cannot inspect the system docker.service process (see systemctl diagnostic above)\n' >&2
    return 1
  fi
  if [[ ! "$daemon_pid" =~ ^[1-9][0-9]*$ ]]; then
    printf 'Docker CE preflight: system docker.service has no valid MainPID (%s)\n' \
      "$daemon_pid" >&2
    return 1
  fi
  if ! daemon_uid="$(stat -c '%u' "/proc/${daemon_pid}")"; then
    printf 'Docker CE preflight: cannot verify owner of Docker process %s (see stat diagnostic above)\n' \
      "$daemon_pid" >&2
    return 1
  fi
  if [[ "$daemon_uid" != 0 ]]; then
    printf 'Docker CE preflight: system docker.service MainPID %s is owned by uid %s, expected rootful uid 0\n' \
      "$daemon_pid" "$daemon_uid" >&2
    return 1
  fi

  # Do not let a caller-selected context or endpoint redirect the validator.
  unset DOCKER_HOST DOCKER_CONTEXT
  export DOCKER_HOST='unix:///var/run/docker.sock'
  if ! client_server="$(docker version --format '{{.Client.Version}}|{{.Server.Version}}')"; then
    if [[ ! -S /var/run/docker.sock ]]; then
      printf 'Docker CE preflight: rootful Docker socket is missing at %s; check docker.socket\n' \
        "$DOCKER_HOST" >&2
    else
      printf 'Docker CE preflight: request to %s failed (see Docker diagnostic above)\n' \
        "$DOCKER_HOST" >&2
    fi
    return 1
  fi
  if [[ "$client_server" != "${expected_version}|${expected_version}" ]]; then
    printf 'Docker CE preflight: endpoint %s reported client/server %s, expected pinned Docker CE %s|%s\n' \
      "$DOCKER_HOST" "$client_server" "$expected_version" "$expected_version" >&2
    return 1
  fi
  return 0
}
