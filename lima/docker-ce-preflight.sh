#!/usr/bin/env bash
# Shared strict identity check for the provisioned rootful Docker CE Engine.

# Long (16-hex) key ID of Docker's release key. The full fingerprint is
# 060A 61C5 1B55 8A7F 742B 77AA C52F EB6B 621E 9F35, and the key file is
# committed at lima/keys/docker-ce.asc.
DOCKER_CE_RPM_SIGNING_KEY_ID=c52feb6b621e9f35
DOCKER_CE_RPM_PACKAGES=(docker-ce docker-ce-cli containerd.io docker-buildx-plugin)
DOCKER_CE_REPO_FILE=/etc/yum.repos.d/docker-ce.repo
DOCKER_CE_KEY_FILE=/etc/pki/rpm-gpg/RPM-GPG-KEY-docker-ce

# Fedora RPMs carry the build signature in the RSA header, which reports the
# full 16-hex key ID. Fall back to the legacy signature tags for older packages.
docker_ce_rpm_signing_key_id() {
  local package="$1" signatures key_id
  if ! signatures="$(rpm -q --queryformat \
    '%{RSAHEADER:pgpsig}\n%{DSAHEADER:pgpsig}\n%{SIGPGP:pgpsig}\n%{SIGGPG:pgpsig}\n' \
    "$package" 2>/dev/null)"; then
    return 1
  fi
  if ! key_id="$(grep -m1 -oE 'Key ID [0-9a-fA-F]{16}' <<<"$signatures")"; then
    return 1
  fi
  printf '%s\n' "${key_id#Key ID }"
}

docker_ce_preflight() {
  local tool item path expected_owner owner package arch key_id docker_path
  local daemon_pid daemon_uid client_server client_version server_version
  local server_arch server_platform host_arch expected_arch setting
  local client_server_pattern='^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.]+)?$'

  for tool in docker rpm stat systemctl; do
    if ! command -v "$tool" >/dev/null 2>&1; then
      printf 'Docker CE preflight: %s is required but was not found on PATH\n' \
        "$tool" >&2
      return 1
    fi
  done

  host_arch="$(uname -m)"
  case "$host_arch" in
    x86_64) expected_arch=amd64 ;;
    aarch64) expected_arch=arm64 ;;
    *)
      printf 'Docker CE preflight: unsupported architecture %s\n' \
        "$host_arch" >&2
      return 1
      ;;
  esac

  docker_path="$(command -v docker)"
  for item in "${docker_path}:docker-ce-cli" \
    '/usr/bin/dockerd:docker-ce' '/usr/bin/containerd:containerd.io' \
    '/usr/libexec/docker/cli-plugins/docker-buildx:docker-buildx-plugin'; do
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

  # Docker RPM versions are not pinned, so accept whatever the signed Fedora
  # stable repository installed at VM creation. Reject a package that is
  # missing, built for another architecture, or not signed by Docker's key.
  for package in "${DOCKER_CE_RPM_PACKAGES[@]}"; do
    if ! arch="$(rpm -q --queryformat '%{ARCH}' "$package" 2>/dev/null)"; then
      printf 'Docker CE preflight: required package %s is missing (see rpm diagnostic above)\n' \
        "$package" >&2
      return 1
    fi
    if [[ "$arch" != "$host_arch" ]]; then
      printf 'Docker CE preflight: %s is built for %s, expected %s\n' \
        "$package" "$arch" "$host_arch" >&2
      return 1
    fi
    if ! key_id="$(docker_ce_rpm_signing_key_id "$package")"; then
      printf 'Docker CE preflight: cannot read the RPM signing key for %s\n' \
        "$package" >&2
      return 1
    fi
    if [[ "${key_id,,}" != "$DOCKER_CE_RPM_SIGNING_KEY_ID" ]]; then
      printf 'Docker CE preflight: %s is signed by key %s, not the Docker release key %s; recreate the VM from a trusted template\n' \
        "$package" "$key_id" "$DOCKER_CE_RPM_SIGNING_KEY_ID" >&2
      return 1
    fi
  done

  if [[ -L "$DOCKER_CE_REPO_FILE" || ! -f "$DOCKER_CE_REPO_FILE" ]]; then
    printf 'Docker CE preflight: repository definition %s is missing or symlinked\n' \
      "$DOCKER_CE_REPO_FILE" >&2
    return 1
  fi
  # shellcheck disable=SC2016 # Match the literal $releasever and $basearch.
  for setting in '[docker-ce-stable]' 'enabled=0' 'gpgcheck=1' \
    'repo_gpgcheck=1' "gpgkey=file://$DOCKER_CE_KEY_FILE" \
    'baseurl=https://download.docker.com/linux/fedora/$releasever/$basearch/stable'
  do
    if ! grep -Fqx -- "$setting" "$DOCKER_CE_REPO_FILE"; then
      printf 'Docker CE preflight: %s does not contain %s\n' \
        "$DOCKER_CE_REPO_FILE" "$setting" >&2
      return 1
    fi
  done
  if [[ -L "$DOCKER_CE_KEY_FILE" || ! -f "$DOCKER_CE_KEY_FILE" ]]; then
    printf 'Docker CE preflight: pinned Docker RPM key %s is missing or symlinked\n' \
      "$DOCKER_CE_KEY_FILE" >&2
    return 1
  fi
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

  # Do not let a caller-selected context or endpoint, including a user's
  # ~/.docker/config.json currentContext, redirect the validator.
  unset DOCKER_HOST DOCKER_CONTEXT
  export DOCKER_HOST='unix:///var/run/docker.sock'
  if ! client_server="$(docker --context default version \
    --format '{{.Client.Version}}|{{.Server.Version}}|{{.Server.Arch}}|{{.Server.Platform.Name}}')"; then
    if [[ ! -S /var/run/docker.sock ]]; then
      printf 'Docker CE preflight: rootful Docker socket is missing at %s; check docker.socket\n' \
        "$DOCKER_HOST" >&2
    else
      printf 'Docker CE preflight: request to %s failed (see Docker diagnostic above)\n' \
        "$DOCKER_HOST" >&2
    fi
    return 1
  fi
  IFS='|' read -r client_version server_version server_arch server_platform \
    <<<"$client_server"
  if [[ ! "$client_version" =~ $client_server_pattern ]]; then
    printf 'Docker CE preflight: endpoint %s reported an invalid client version (%s)\n' \
      "$DOCKER_HOST" "$client_version" >&2
    return 1
  fi
  if [[ ! "$server_version" =~ $client_server_pattern ]]; then
    printf 'Docker CE preflight: endpoint %s reported an invalid server version (%s)\n' \
      "$DOCKER_HOST" "$server_version" >&2
    return 1
  fi
  if [[ "$server_arch" != "$expected_arch" ]]; then
    printf 'Docker CE preflight: endpoint %s reported architecture %s, expected %s\n' \
      "$DOCKER_HOST" "$server_arch" "$expected_arch" >&2
    return 1
  fi
  if [[ "$server_platform" != "Docker Engine"* ]]; then
    printf 'Docker CE preflight: endpoint %s reported platform %s, expected Docker Engine\n' \
      "$DOCKER_HOST" "$server_platform" >&2
    return 1
  fi
  return 0
}
