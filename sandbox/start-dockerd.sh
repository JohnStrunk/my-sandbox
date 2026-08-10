#! /bin/bash
# Starts the Docker daemon inside the devbox container to provide
# Docker-in-Docker support, then relaxes the socket permissions so the
# unprivileged sandbox user (which may have an arbitrary, host-mapped UID/GID)
# can use it.

set -eo pipefail

if [ -S /var/run/docker.sock ]; then
  echo "==> Docker daemon already running"
  exit 0
fi

echo "==> Starting Docker daemon"
dockerd >/var/log/dockerd.log 2>&1 &

echo "==> Waiting for Docker daemon to be ready"
for _ in $(seq 1 30); do
  if [ -S /var/run/docker.sock ]; then
    break
  fi
  sleep 1
done

if [ ! -S /var/run/docker.sock ]; then
  echo "==> Docker daemon failed to start; see /var/log/dockerd.log" >&2
  exit 1
fi

# The sandbox user's UID/GID is remapped to match the host user at container
# startup, and there's no guarantee the resulting user picks up the "docker"
# group membership, so make the socket world read/writable instead.
chmod 666 /var/run/docker.sock
