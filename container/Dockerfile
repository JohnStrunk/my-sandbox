# Custom OpenShell sandbox image mirroring the devcontainer environment.
# hadolint ignore=DL3007
FROM ghcr.io/nvidia/openshell-community/sandboxes/base:latest@sha256:aeef1c63f00e2913ea002ccb3aaf925f338b5c5d70e63576f0d95c16a138044e

# hadolint ignore=DL3002,DL3066
USER root
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Install system utilities matching devcontainer
# hadolint ignore=DL3008
RUN apt-get update -q && apt-get install -y --no-install-recommends \
    ca-certificates \
    ffmpeg \
    gh \
    glab \
    jq \
    ripgrep \
    shellcheck \
    && rm -rf /var/lib/apt/lists/*

# Install Red Hat internal CA certificates
COPY certs/ /usr/local/share/ca-certificates/redhat/
RUN /usr/sbin/update-ca-certificates
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt

# Install Hadolint
# renovate: datasource=github-releases depName=hadolint/hadolint
ARG HADOLINT_VERSION=2.15.1
RUN curl -sSL "https://github.com/hadolint/hadolint/releases/download/v${HADOLINT_VERSION}/hadolint-Linux-x86_64" -o /usr/local/bin/hadolint \
    && chmod +x /usr/local/bin/hadolint

# Go toolchain
# renovate: datasource=golang-version depName=golang
ARG GO_VERSION=1.27.0
ARG TARGETARCH=amd64
RUN curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-${TARGETARCH}.tar.gz" -o /tmp/go.tgz \
    && tar -C /usr/local -xzf /tmp/go.tgz \
    && rm /tmp/go.tgz
ENV PATH="/usr/local/go/bin:${PATH}"

# Rust toolchain
ENV RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    PATH="/usr/local/cargo/bin:${PATH}"
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --no-modify-path \
    && chmod -R a+w "${RUSTUP_HOME}" "${CARGO_HOME}"

# uv (Python packaging)
# renovate: datasource=github-releases depName=astral-sh/uv
ARG UV_VERSION=0.12.5
RUN curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" | sh \
    && mv /root/.local/bin/uv /root/.local/bin/uvx /usr/local/bin/
ENV UV_CACHE_DIR=/sandbox/.uv_cache

# markdownlint-cli2 version must match the rev in .pre-commit-config.yaml -
# its pre-commit hook uses `language: system` and runs this binary directly.
RUN npm install -g --no-fund \
    markdownlint-cli2@0.23.2 \
    @playwright/cli@0.1.18 \
    && npx -y playwright install --with-deps

# Install Google Antigravity CLI
# renovate: datasource=github-releases depName=google-antigravity/antigravity-cli
ARG AGY_VERSION=1.1.20
RUN arch="${TARGETARCH}" && [ "$arch" = "amd64" ] && arch="x64" \
    && curl -fsSL "https://github.com/google-antigravity/antigravity-cli/releases/download/${AGY_VERSION}/agy_cli_linux_${arch}.tar.gz" -o /tmp/agy.tgz \
    && tar -C /tmp -xzf /tmp/agy.tgz antigravity \
    && mv /tmp/antigravity /usr/local/bin/agy \
    && chmod +x /usr/local/bin/agy \
    && rm /tmp/agy.tgz

# Install OpenCode (overwrites the outdated version in the base image)
# renovate: datasource=github-releases depName=anomalyco/opencode
ARG OPENCODE_VERSION=1.18.23
RUN curl -fsSL https://opencode.ai/install | bash -s -- --version "${OPENCODE_VERSION}" && \
    mv /root/.opencode/bin/opencode /usr/bin/ && \
    chmod +x /usr/bin/opencode

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
# renovate: datasource=custom.acli depName=acli
ARG ACLI_VERSION=1.3.29-stable
RUN curl -L "https://acli.atlassian.com/linux/${ACLI_VERSION}/acli_${ACLI_VERSION}_linux_${TARGETARCH}.tar.gz" -o /tmp/acli.tar.gz \
    && tar xzf /tmp/acli.tar.gz -C /tmp/ \
    && mv /tmp/acli_*_linux_${TARGETARCH}/acli /usr/local/bin/acli \
    && rm -rf /tmp/acli.tar.gz /tmp/acli_*_linux_${TARGETARCH}

RUN npm install -g --no-fund \
    @googleworkspace/cli@0.22.5

# Podman (for Podman-in-Podman support, so the sandbox can build/run its own
# containers). This runs fully rootless, nested inside the also-rootless
# outer devbox container: no --privileged and no added Linux capabilities
# are needed on the outer container, just /dev/fuse, /dev/net/tun, and (on
# SELinux hosts) a disabled security label -- see devbox. This setup
# follows the pattern documented by Podman's own maintainers for rootless
# Podman-in-Podman:
# https://www.redhat.com/en/blog/podman-inside-container ("Rootless Podman
# running rootless Podman") and podman's own reference image at
# https://github.com/containers/podman/tree/main/contrib/podmanimage/stable
#
# netavark/aardvark-dns aren't pulled in automatically by Ubuntu's podman
# package. Without them, nested Podman silently falls back to the older CNI
# network backend on its first run (this choice is then cached on-disk and
# "sticks" even if the packages are installed later), which lacks
# aardvark-dns's container-name DNS resolution: containers on a
# user-defined network can't reach each other by name, breaking anything
# that looks like a multi-container dev workflow (e.g. an app + DB pod).
# hadolint ignore=DL3008
RUN apt-get update -q && apt-get install -y --no-install-recommends \
    aardvark-dns \
    fuse-overlayfs \
    netavark \
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
# usable is whatever range keep-id's remapping (see devbox) leaves
# available inside *this* container's own user namespace, which depends on
# the invoking host user's own /etc/subuid allocation and isn't known until
# the container is started. A build-time guess here (e.g. "100000:65536")
# would almost always point outside that available range and fail with
# EPERM from newuidmap ("the mapped user IDs must have a mapping in the
# parent user namespace" -- see user_namespaces(7)). devbox computes
# and writes the correct entries after starting the container.

# Rootless Podman-in-Podman needs fuse-overlayfs: nesting the kernel's own
# overlay filesystem inside another overlay mount is normally rejected, but
# fuse-overlayfs runs in userspace and isn't subject to that restriction.
#
# storage.conf's mountopt, matching the "nodev,fsync=0" used by podman's own
# reference image
# (https://github.com/containers/image_build/blob/main/podman/Containerfile):
#   - nodev: hardening parity with that reference config; nested containers
#     don't need to create device nodes on the overlay layers.
#   - fsync=0: fuse-overlayfs round-trips every write through userspace, so
#     fsync-ing on every layer write (the default) makes image pulls/builds
#     inside the nested Podman noticeably slower. This is safe here because
#     the whole point of this storage is disposable, nested container
#     layers, not data that needs durability guarantees.
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
# devbox's dynamic subordinate ID setup) follows the pattern
# documented by Podman's own maintainers, cited above.
RUN mkdir -p /sandbox/.config/containers \
    && printf '[storage]\ndriver = "overlay"\n\n[storage.options]\nmount_program = "/usr/bin/fuse-overlayfs"\nmountopt = "nodev,fsync=0"\n' \
    > /sandbox/.config/containers/storage.conf \
    && printf '[containers]\ncgroups = "disabled"\nvolumes = ["/proc:/proc"]\ndefault_sysctls = []\nutsns = "host"\n\n[network]\ndefault_rootless_network_cmd = "slirp4netns"\n' \
    > /sandbox/.config/containers/containers.conf \
    && mkdir -p /sandbox/.config/acli /sandbox/.config/gcloud /sandbox/.config/gws /sandbox/.config/opencode /sandbox/.local/state/opencode /sandbox/.local/share/opencode \
    && touch /sandbox/.local/state/opencode/model.json /sandbox/.config/gcloud/application_default_credentials.json \
    && chown -R sandbox:sandbox /sandbox/.config /sandbox/.local

# Two more settings from podman's own reference Podman-in-Podman image
# (https://github.com/containers/image_build/blob/main/podman/Containerfile),
# defensive for nested `podman build`/`buildah` in this doubly-rootless
# setup even though they weren't observed to be strictly required here:
#   - BUILDAH_ISOLATION=chroot: avoids buildah's default namespace-based
#     RUN-step isolation, which needs more namespace nesting than may be
#     available depending on the host's own subuid/subgid allocation (see
#     devbox).
#   - _CONTAINERS_USERNS_CONFIGURED="": tells Podman/Buildah that the user
#     namespace/subordinate ID setup is already handled (by devbox),
#     so they shouldn't try to auto-configure or re-exec into one themselves.
ENV BUILDAH_ISOLATION=chroot \
    _CONTAINERS_USERNS_CONFIGURED=""

COPY devbox-entry.sh /usr/local/bin/
RUN chmod +rx /usr/local/bin/devbox-entry.sh \
    && echo "sandbox ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers

ENTRYPOINT []
CMD ["/usr/local/bin/devbox-entry.sh"]
