# Custom OpenShell sandbox image mirroring the devcontainer environment.
# hadolint ignore=DL3007
FROM ghcr.io/nvidia/openshell-community/sandboxes/base:latest AS base

# hadolint ignore=DL3002
USER root
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Install system utilities matching devcontainer
# hadolint ignore=DL3008
RUN apt-get update -q && apt-get install -y --no-install-recommends \
    ffmpeg \
    gh \
    glab \
    jq \
    ripgrep \
    shellcheck \
    && rm -rf /var/lib/apt/lists/*

# Install Hadolint
ARG HADOLINT_VERSION=2.12.0
RUN curl -sSL "https://github.com/hadolint/hadolint/releases/download/v${HADOLINT_VERSION}/hadolint-Linux-x86_64" -o /usr/local/bin/hadolint \
    && chmod +x /usr/local/bin/hadolint

# Go toolchain
ARG GO_VERSION=1.23.4
ARG TARGETARCH=amd64
RUN curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-${TARGETARCH}.tar.gz" -o /tmp/go.tgz \
    && tar -C /usr/local -xzf /tmp/go.tgz \
    && rm /tmp/go.tgz
ENV PATH="/usr/local/go/bin:${PATH}"

# uv (Python packaging)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.local/bin/uv /root/.local/bin/uvx /usr/local/bin/
ENV UV_CACHE_DIR=/sandbox/.uv_cache

# markdownlint-cli2 version must match the rev in .pre-commit-config.yaml -
# its pre-commit hook uses `language: system` and runs this binary directly.
# hadolint ignore=DL3016
RUN npm install -g --no-fund \
    markdownlint-cli2@0.23.0 \
    @playwright/cli \
    && npx -y playwright install --with-deps

# Install Google Antigravity CLI
RUN curl -fsSL https://antigravity.google/cli/install.sh | bash \
    && mv /root/.local/bin/agy /usr/local/bin/

# Install OpenCode (overwrites the outdated version in the base image)
RUN curl -fsSL https://opencode.ai/install | bash && \
    mv /root/.opencode/bin/opencode /usr/bin/ && \
    chmod +x /usr/bin/opencode


##################################################
FROM base AS devbox
ARG TARGETARCH=amd64
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# hadolint ignore=DL3008
RUN apt-get update -q && apt-get install -y --no-install-recommends \
    adduser \
    ca-certificates \
    curl \
    gnupg \
    passwd \
    sudo \
    && rm -rf /var/lib/apt/lists/*

# Install Google Cloud CLI
# https://cloud.google.com/sdk/docs/install-sdk#deb
# hadolint ignore=DL3008
RUN curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg \
    && chmod a+r /usr/share/keyrings/cloud.google.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" > /etc/apt/sources.list.d/google-cloud-sdk.list \
    && apt-get update -q && apt-get install -y --no-install-recommends \
    google-cloud-cli \
    && rm -rf /var/lib/apt/lists/*

# Atlassian CLI (acli)
RUN curl -L "https://acli.atlassian.com/linux/latest/acli_linux_${TARGETARCH}.tar.gz" -o /tmp/acli.tar.gz \
    && tar xzf /tmp/acli.tar.gz -C /tmp/ \
    && mv /tmp/acli_*_linux_${TARGETARCH}/acli /usr/local/bin/acli \
    && rm -rf /tmp/acli.tar.gz /tmp/acli_*_linux_${TARGETARCH}

# hadolint ignore=DL3016
RUN npm install -g --no-fund \
    @googleworkspace/cli

# Podman (for Podman-in-Podman support, so the sandbox can build/run its own
# containers). This runs fully rootless, nested inside the also-rootless
# outer devbox container: no --privileged and no added Linux capabilities
# are needed on the outer container, just /dev/fuse, /dev/net/tun, and (on
# SELinux hosts) a disabled security label -- see create-devbox. This setup
# follows the pattern documented by Podman's own maintainers for rootless
# Podman-in-Podman:
# https://www.redhat.com/en/blog/podman-inside-container ("Rootless Podman
# running rootless Podman") and podman's own reference image at
# https://github.com/containers/podman/tree/main/contrib/podmanimage/stable
# hadolint ignore=DL3008
RUN apt-get update -q && apt-get install -y --no-install-recommends \
    fuse-overlayfs \
    podman \
    slirp4netns \
    uidmap \
    && rm -rf /var/lib/apt/lists/*

# Ubuntu's newuidmap/newgidmap ship setuid-root only, without the file
# capabilities (cap_setuid/cap_setgid) that make the traditional
# "transition to euid 0 grants full capabilities" fallback actually work
# for a non-root container user -- the same known packaging gap the above
# Red Hat blog post documents for Fedora ("there is a known issue... where
# the filecaps on newuidmap and newgidmap are not set"; Fedora's fix is
# reinstalling shadow-utils, which isn't available/applicable here).
# Without this, nested `podman run` as the non-root "sandbox" user fails
# with "newuidmap: write to uid_map failed: Operation not permitted", even
# though the setuid bit alone looks like it should work.
RUN /usr/sbin/setcap cap_setuid+eip /usr/bin/newuidmap \
    && /usr/sbin/setcap cap_setgid+eip /usr/bin/newgidmap \
    && chmod u-s,g-s /usr/bin/newuidmap /usr/bin/newgidmap

# sandbox's /etc/subuid and /etc/subgid entries, needed so its own nested
# Podman can build a further user namespace for the containers *it* runs,
# are NOT set here at build time: the only subordinate ID range actually
# usable is whatever range keep-id's remapping (see create-devbox) leaves
# available inside *this* container's own user namespace, which depends on
# the invoking host user's own /etc/subuid allocation and isn't known until
# the container is started. A build-time guess here (e.g. "100000:65536")
# would almost always point outside that available range and fail with
# EPERM from newuidmap ("the mapped user IDs must have a mapping in the
# parent user namespace" -- see user_namespaces(7)). create-devbox computes
# and writes the correct entries after starting the container.

# Rootless Podman-in-Podman needs fuse-overlayfs: nesting the kernel's own
# overlay filesystem inside another overlay mount is normally rejected, but
# fuse-overlayfs runs in userspace and isn't subject to that restriction.
#
# containers.conf settings, and why each is needed for nested `podman run`
# as the non-root "sandbox" user in this outer rootless container:
#   - cgroups = "disabled": the outer container has no delegated, writable
#     cgroup subtree to give nested containers, so cgroup-based resource
#     limits/stats for them are unavailable regardless; this makes that
#     explicit instead of silently no-op-ing.
#   - volumes = ["/proc:/proc"]: bind-mounts the outer container's own
#     (already-mounted) /proc into nested containers instead of having crun
#     mount a fresh procfs instance for each one, which fails ("mount proc
#     to proc: Operation not permitted") in this nested setup. Taken
#     directly from podman's own reference image's containers.conf:
#     https://github.com/containers/podman/blob/main/contrib/podmanimage/stable/podman-containers.conf
#   - default_sysctls = []: without this, podman tries to tune
#     network/ping sysctls for every container it starts, which fails here
#     because the outer container's /proc/sys is read-only. This is
#     podman's own documented fix for rootless use:
#     https://github.com/containers/podman/pull/16209
#   - utsns = "host": avoids crun's "sethostname: Operation not permitted"
#     when nested containers try to set their own hostname in a fresh UTS
#     namespace.
#   - default_rootless_network_cmd = "slirp4netns": nested containers
#     default to unprivileged, userspace NAT networking instead of the CNI
#     bridge plugin, which needs bridge-creation privileges the outer
#     rootless container doesn't have.
# This combination (together with the newuidmap/newgidmap fix above and
# create-devbox's dynamic subordinate ID setup) follows the pattern
# documented by Podman's own maintainers, cited above.
RUN mkdir -p /sandbox/.config/containers \
    && printf '[storage]\ndriver = "overlay"\n\n[storage.options]\nmount_program = "/usr/bin/fuse-overlayfs"\n' \
        > /sandbox/.config/containers/storage.conf \
    && printf '[containers]\ncgroups = "disabled"\nvolumes = ["/proc:/proc"]\ndefault_sysctls = []\nutsns = "host"\n\n[network]\ndefault_rootless_network_cmd = "slirp4netns"\n' \
        > /sandbox/.config/containers/containers.conf \
    && chown -R sandbox:sandbox /sandbox/.config

COPY devbox-entry.sh /usr/local/bin/
RUN echo "sandbox ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers

ENTRYPOINT []
CMD ["/usr/local/bin/devbox-entry.sh"]


##################################################
# Last stage is picked up by OpenShell to create the sandbox image.
FROM base AS final
USER sandbox
WORKDIR /sandbox
