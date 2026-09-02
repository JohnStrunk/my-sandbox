# Custom OpenShell sandbox image mirroring the devcontainer environment.
# hadolint ignore=DL3007
FROM docker.io/fedora:latest@sha256:43b29f65a41eb9c35e1cd5323e3bdf3b655c2357a9f4f1ff2f9c2798e5045d80

# hadolint ignore=DL3002,DL3066
USER root
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Install system utilities matching devcontainer
# Prevent Node.js 22 Intl.Segmenter crashes without full ICU data:
# https://bugzilla.redhat.com/show_bug.cgi?id=2418608
# hadolint ignore=DL3041
RUN dnf install -y --setopt=install_weak_deps=False \
    ca-certificates \
    curl \
    ffmpeg-free \
    gh \
    git \
    glab \
    gnupg2 \
    jq \
    libcap \
    nodejs22-full-i18n \
    nodejs \
    npm \
    nss \
    openssl \
    procps-ng \
    ripgrep \
    ShellCheck \
    shadow-utils \
    sudo \
    && dnf clean all \
    && rm -rf /var/cache/dnf

# Install Red Hat internal CA certificates
COPY certs/ /etc/pki/ca-trust/source/anchors/
RUN update-ca-trust \
    && mkdir -p /usr/local/share/ca-certificates/redhat /etc/ssl/certs \
    && cp /etc/pki/ca-trust/source/anchors/*.crt /usr/local/share/ca-certificates/redhat/ \
    && ln -sf /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem /etc/ssl/certs/ca-certificates.crt
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt

# Keep Playwright's browser files available to the non-root runtime user and
# use the bundled Chromium build instead of an absent system Chrome install.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PLAYWRIGHT_MCP_BROWSER=chromium

# Keep image, CI, and pre-commit tool pins in one machine-readable manifest.
ARG TARGETARCH=amd64
COPY tool-versions.json /tmp/devbox-tool-versions.json

# Install Hadolint
RUN hadolint_version="$(jq -er '.tools.hadolint.version' /tmp/devbox-tool-versions.json)" \
    && hadolint_version="${hadolint_version#v}" \
    && case "${TARGETARCH:-amd64}" in \
        amd64) hadolint_arch=x86_64 ;; \
        arm64) hadolint_arch=arm64 ;; \
        *) echo "Unsupported architecture for Hadolint: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -sSL "https://github.com/hadolint/hadolint/releases/download/v${hadolint_version}/hadolint-linux-${hadolint_arch}" -o /usr/local/bin/hadolint \
    && chmod +x /usr/local/bin/hadolint

# Go toolchain
RUN go_version="$(jq -er '.tools.go.version' /tmp/devbox-tool-versions.json)" \
    && curl -fsSL "https://go.dev/dl/go${go_version}.linux-${TARGETARCH}.tar.gz" -o /tmp/go.tgz \
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
RUN uv_version="$(jq -er '.tools.uv.version' /tmp/devbox-tool-versions.json)" \
    && uv_version="${uv_version#v}" \
    && curl -LsSf "https://astral.sh/uv/${uv_version}/install.sh" | sh \
    && mv /root/.local/bin/uv /root/.local/bin/uvx /usr/local/bin/
ENV UV_CACHE_DIR=/sandbox/.uv_cache

# markdownlint-cli2 version must match the rev in .pre-commit-config.yaml -
# its pre-commit hook uses `language: system` and runs this binary directly.
# @playwright/cli bundles the matching Playwright package. Use its installer
# rather than `npx playwright`, which can resolve an unrelated package when
# this image is not built from an npm project.
RUN markdownlint_version="$(jq -er '.tools.markdownlint_cli2.version' /tmp/devbox-tool-versions.json)" \
    && playwright_version="$(jq -er '.tools.playwright_cli.version' /tmp/devbox-tool-versions.json)" \
    && markdownlint_version="${markdownlint_version#v}" \
    && playwright_version="${playwright_version#v}" \
    && npm install -g --no-fund \
    "markdownlint-cli2@${markdownlint_version}" \
    "@playwright/cli@${playwright_version}" \
    && playwright-cli install-browser chrome-for-testing \
    && chmod -R a+rX "${PLAYWRIGHT_BROWSERS_PATH}"

# Install Google Antigravity CLI
RUN agy_version="$(jq -er '.tools.antigravity_cli.version' /tmp/devbox-tool-versions.json)" \
    && agy_version="${agy_version#v}" \
    && arch="${TARGETARCH}" \
    && if [ "$arch" = "amd64" ]; then arch="x64"; fi \
    && curl -fsSL "https://github.com/google-antigravity/antigravity-cli/releases/download/${agy_version}/agy_cli_linux_${arch}.tar.gz" -o /tmp/agy.tgz \
    && tar -C /tmp -xzf /tmp/agy.tgz antigravity \
    && mv /tmp/antigravity /usr/local/bin/agy \
    && chmod +x /usr/local/bin/agy \
    && rm /tmp/agy.tgz

# Install OpenCode
RUN opencode_version="$(jq -er '.tools.opencode.version' /tmp/devbox-tool-versions.json)" \
    && opencode_version="${opencode_version#v}" \
    && curl -fsSL https://opencode.ai/install | bash -s -- --version "${opencode_version}" && \
    mv /root/.opencode/bin/opencode /usr/bin/ && \
    chmod +x /usr/bin/opencode

# Install tokenjuice, a deterministic output compactor for terminal-heavy
# agent workflows: https://github.com/vincentkoc/tokenjuice
RUN tokenjuice_version="$(jq -er '.tools.tokenjuice.version' /tmp/devbox-tool-versions.json)" \
    && npm install -g --no-fund "tokenjuice@${tokenjuice_version}"

# Install Google Cloud CLI
# https://cloud.google.com/sdk/docs/install-sdk#linux
# hadolint ignore=DL3041
RUN case "${TARGETARCH}" in \
      amd64) gcloud_arch=x86_64 ;; \
      arm64) gcloud_arch=aarch64 ;; \
      *) echo "Unsupported architecture for Google Cloud CLI: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && printf '%s\n' \
      '[google-cloud-cli]' \
      'name=Google Cloud CLI' \
      "baseurl=https://packages.cloud.google.com/yum/repos/cloud-sdk-el9-${gcloud_arch}" \
      'enabled=1' \
      'gpgcheck=1' \
      'repo_gpgcheck=0' \
      'gpgkey=https://packages.cloud.google.com/yum/doc/yum-key.gpg' \
      '       https://packages.cloud.google.com/yum/doc/rpm-package-key.gpg' \
      > /etc/yum.repos.d/google-cloud-sdk.repo \
    && dnf install -y --setopt=install_weak_deps=False google-cloud-cli \
    && rm -f /etc/yum.repos.d/google-cloud-sdk.repo \
    && dnf clean all \
    && rm -rf /var/cache/dnf

# Atlassian CLI (acli)
RUN acli_version="$(jq -er '.tools.acli.version' /tmp/devbox-tool-versions.json)" \
    && acli_version="${acli_version#v}" \
    && curl -L "https://acli.atlassian.com/linux/${acli_version}/acli_${acli_version}_linux_${TARGETARCH}.tar.gz" -o /tmp/acli.tar.gz \
    && tar xzf /tmp/acli.tar.gz -C /tmp/ \
    && mv /tmp/acli_*_linux_${TARGETARCH}/acli /usr/local/bin/acli \
    && rm -rf /tmp/acli.tar.gz /tmp/acli_*_linux_${TARGETARCH}

RUN google_workspace_version="$(jq -er '.tools.google_workspace_cli.version' /tmp/devbox-tool-versions.json)" \
    && google_workspace_version="${google_workspace_version#v}" \
    && npm install -g --no-fund \
    "@googleworkspace/cli@${google_workspace_version}"

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
# netavark/aardvark-dns are installed explicitly so nested Podman always uses
# the current network backend with container-name DNS resolution.
#
# passt (providing the `pasta` command) is nested Podman's default rootless
# network mode (see containers.conf below): unlike the netavark bridge mode,
# pasta needs no sysctl writes through the outer container's read-only
# /proc/sys (see below) to support published ports, so it works in this
# doubly-rootless setup without any added capabilities.
# hadolint ignore=DL3041
RUN dnf install -y --setopt=install_weak_deps=False \
    aardvark-dns \
    fuse-overlayfs \
    netavark \
    passt \
    podman \
    slirp4netns \
    shadow-utils-subid \
    && dnf clean all \
    && rm -rf /var/cache/dnf

# Ensure nested Podman can create user namespaces as the non-root sandbox user.
RUN /usr/sbin/setcap cap_setuid+eip /usr/bin/newuidmap \
    && /usr/sbin/setcap cap_setgid+eip /usr/bin/newgidmap \
    && chmod u-s,g-s /usr/bin/newuidmap /usr/bin/newgidmap

# Fedora's container image protects /etc/shadow with mode 000. PAM's
# unix_chkpwd helper needs to read it during sudo account checks, but Ubuntu
# 24.04 AppArmor blocks the required DAC_OVERRIDE in rootless containers.
# Root-only read access avoids that capability while keeping the hash private.
RUN useradd --create-home --user-group --home-dir /sandbox --shell /bin/bash sandbox \
    && chmod 0400 /etc/shadow

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
#   - netns = "pasta": selects pasta as nested Podman's default
#     Docker-API-compatible network mode. Podman's Docker-compat layer ignores
#     [network] default_rootless_network_cmd and substitutes "bridge" for an
#     unspecified NetworkMode unless this [containers] netns option is itself
#     set to something other than its "" / "default" / "private" values (see
#     podman's pkg/api/handlers/compat/containers_create.go). Setting it here
#     is what makes clients such as Testcontainers/Dockerode, which normally
#     don't request a network, get pasta -- and therefore working published
#     ports -- by default instead of a netavark bridge that needs sysctl writes
#     the outer container's read-only /proc/sys blocks.
#   - default_rootless_network_cmd = "pasta": keeps plain `podman run`
#     (native CLI, not the Docker-compat API) on the same default.
# This combination (together with the newuidmap/newgidmap fix above and
# devbox's dynamic subordinate ID setup) follows the pattern
# documented by Podman's own maintainers, cited above.
#
# A netavark bridge network (an explicit `podman network create`/
# `--network bridge`, as opposed to the pasta default above) still needs
# netavark to write a handful of IPv4 sysctls for the bridge/interface it
# creates (route_localnet, arp_notify, rp_filter, and ip_forward), plus
# IPv6 forwarding/autoconfiguration values when IPv6 is enabled. Those writes
# still fail through the outer container's read-only /proc/sys. `devbox`
# preseeds the equivalent defaults
# values on the *outer* container at launch (see devbox), then verifies those
# values inside nested Podman's rootless network namespace, then exercises a
# temporary user-defined network with a published port, before changing this
# `netns` setting to `bridge`. Netavark's read-before-write (see
# containers/netavark src/network/sysctl.rs) can then use the inherited
# values instead of hitting EROFS. This only helps if the host that launches
# the outer container can itself apply those sysctls; `devbox` detects and
# reports when it can't (see devbox-docker-api-check.sh), and the pasta-based
# default network above remains available either way.
RUN mkdir -p /sandbox/.config/containers \
    && printf '[storage]\ndriver = "overlay"\n\n[storage.options]\nmount_program = "/usr/bin/fuse-overlayfs"\nmountopt = "nodev,fsync=0"\n' \
    > /sandbox/.config/containers/storage.conf \
    && printf '[containers]\ncgroups = "disabled"\nvolumes = ["/proc:/proc"]\ndefault_sysctls = []\nutsns = "host"\nnetns = "pasta"\n\n[network]\nnetwork_backend = "netavark"\ndefault_rootless_network_cmd = "pasta"\n' \
    > /sandbox/.config/containers/containers.conf \
    && mkdir -p /sandbox/.config/acli /sandbox/.config/gcloud /sandbox/.config/gws /sandbox/.config/opencode /sandbox/.local/state/opencode /sandbox/.local/share/opencode /sandbox/.docker/run \
    && touch /sandbox/.local/state/opencode/model.json /sandbox/.config/gcloud/application_default_credentials.json \
    && chown -R sandbox:sandbox /sandbox/.config /sandbox/.local /sandbox/.docker

ENV HOME=/sandbox \
    DOCKER_HOST=unix:///sandbox/.docker/run/docker.sock

# Install pre-commit for the sandbox user so its executable and tool
# environment live under /sandbox/.local.
ENV PATH="/sandbox/.local/bin:${PATH}"
RUN pre_commit_version="$(jq -er '.tools.pre_commit.version' /tmp/devbox-tool-versions.json)" \
    && pre_commit_version="${pre_commit_version#v}" \
    && /usr/sbin/runuser -u sandbox -- uv tool install "pre-commit==${pre_commit_version}"

# Register tokenjuice's OpenCode plugin for the sandbox user, so terminal-heavy
# tool output (bash, grep, etc.) is compacted by default. Runs as sandbox (via
# runuser, which resolves its HOME from /etc/passwd) so the generated
# ~/.config/opencode/plugins/tokenjuice.js is owned by that user, matching the
# other /sandbox/.config content above. A host-mounted ~/.config/opencode
# directory (see devbox) takes precedence over this baked-in plugin, the same
# as the other OpenCode configuration and state defaults set up above.
RUN /usr/sbin/runuser -u sandbox -- tokenjuice install opencode

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

COPY devbox-entry.sh devbox-docker-api-check.sh /usr/local/bin/
RUN chmod +rx /usr/local/bin/devbox-entry.sh /usr/local/bin/devbox-docker-api-check.sh \
    && ln -s devbox-docker-api-check.sh /usr/local/bin/devbox-docker-api-check \
    && printf '%s\n' 'sandbox ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/sandbox \
    && chmod 0440 /etc/sudoers.d/sandbox \
    && visudo -cf /etc/sudoers

RUN rm -f /tmp/devbox-tool-versions.json

ENTRYPOINT []
CMD ["/usr/local/bin/devbox-entry.sh"]
