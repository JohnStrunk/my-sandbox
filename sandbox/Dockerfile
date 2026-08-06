# Custom OpenShell sandbox image mirroring the devcontainer environment.
# hadolint ignore=DL3007
FROM ghcr.io/nvidia/openshell-community/sandboxes/base:latest

USER root
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Install system utilities matching devcontainer
# hadolint ignore=DL3008
RUN apt-get update -q && apt-get install -y --no-install-recommends \
    ffmpeg \
    gh \
    glab \
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

# Atlassian CLI (acli)
RUN curl -L "https://acli.atlassian.com/linux/latest/acli_linux_${TARGETARCH}.tar.gz" -o /tmp/acli.tar.gz \
    && tar xzf /tmp/acli.tar.gz -C /tmp/ \
    && mv /tmp/acli_*_linux_${TARGETARCH}/acli /usr/local/bin/acli \
    && rm -rf /tmp/acli.tar.gz /tmp/acli_*_linux_${TARGETARCH}

# Google Workspace CLI + Playwright + Markdownlint
# hadolint ignore=DL3016
RUN npm install -g --no-fund \
    @googleworkspace/cli \
    markdownlint-cli2 \
    @playwright/cli \
    && npx -y playwright install --with-deps

# Install Google Antigravity CLI
RUN curl -fsSL https://antigravity.google/cli/install.sh | bash \
    && mv /root/.local/bin/agy /usr/local/bin/

# Install OpenCode (overwrites the outdated version in the base image)
RUN curl -fsSL https://opencode.ai/install | bash && \
    mv /root/.opencode/bin/opencode /usr/bin/ && \
    chmod +x /usr/bin/opencode


USER sandbox
WORKDIR /sandbox
