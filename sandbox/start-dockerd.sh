#! /bin/bash
# Starts the Docker daemon inside the devbox container to provide
# Docker-in-Docker support, then relaxes the socket permissions so the
# unprivileged sandbox user (which may have an arbitrary, host-mapped UID/GID)
# can use it.

set -eo pipefail

if docker version >/dev/null 2>&1; then
  echo "==> Docker daemon already running"
  exit 0
fi

# dockerd binds the socket file early in its startup sequence, before it's
# actually ready to serve requests, so a stale socket file left behind by a
# previous, failed dockerd process wouldn't otherwise be cleaned up. Remove
# it so the readiness check below can't be fooled by it.
rm -f /var/run/docker.sock

# The devbox container's root filesystem is mounted with "private"
# propagation by default. That's fine for plain container start/stop, but it
# breaks anything that relies on shared bind-mount propagation between this
# container and the containers dockerd spawns (e.g. `docker run --mount
# bind-propagation=shared`, Kubernetes-style hostPath mounts, or tools like
# kind/k3d), which fail with "path ... is mounted on / but it is not a
# shared mount". Marking / as recursively shared fixes this; it's a no-op if
# already shared.
echo "==> Making root filesystem mount propagation shared"
mount --make-rshared /

echo "==> Starting Docker daemon"
# The base image's default PATH excludes /usr/sbin and /sbin, where
# iptables/nft live. Without them, dockerd fails to initialize its bridge
# network's NAT chain (though it may still create the socket file before
# crashing, leaving a stale, non-functional socket behind).
PATH="/usr/sbin:/sbin:$PATH" dockerd >/var/log/dockerd.log 2>&1 &

echo "==> Waiting for Docker daemon to be ready"
for _ in $(seq 1 30); do
  if docker version >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! docker version >/dev/null 2>&1; then
  echo "==> Docker daemon failed to start; see /var/log/dockerd.log" >&2
  exit 1
fi

# The sandbox user's UID/GID is remapped to match the host user at container
# startup, and there's no guarantee the resulting user picks up the "docker"
# group membership, so make the socket world read/writable instead.
chmod 666 /var/run/docker.sock
