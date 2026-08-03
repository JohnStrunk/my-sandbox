#! /bin/bash

#! /bin/bash

set -e -o pipefail -x

LOG_FILE="/tmp/install-packages.log"
exec > >(tee -a "$LOG_FILE") 2>&1

cd "$(dirname "$0")"

# Switch to nftables to avoid iptables errors in Docker.
# This fixes "Table does not exist (do you need to insmod?)" errors when starting dockerd.
sudo update-alternatives --set iptables /usr/sbin/iptables-nft
sudo service docker stop || true
sudo pkill -x dockerd || true
sudo pkill -x containerd || true
sudo /usr/local/share/docker-init.sh

# Package versions to be managed by Renovate
# renovate: datasource=npm depName=@bitwarden/cli
BITWARDEN_CLI_VERSION="2026.6.0"
# renovate: datasource=npm depName=markdownlint-cli2
MARKDOWNLINT_CLI2_VERSION="0.23.1"
# renovate: datasource=npm depName=@playwright/cli
PLAYWRIGHT_CLI_VERSION="0.1.17"
# renovate: datasource=npm depName=playwright
PLAYWRIGHT_VERSION="1.61.1"
# renovate: datasource=pypi depName=ruff
RUFF_VERSION="0.16.0"

# Install Google Antigravity CLI
curl -fsSL https://antigravity.google/cli/install.sh | bash

# Install npm tools
npm install -g --no-fund \
    @bitwarden/cli@"$BITWARDEN_CLI_VERSION" \
    markdownlint-cli2@"$MARKDOWNLINT_CLI2_VERSION" \
    @playwright/cli@"$PLAYWRIGHT_CLI_VERSION"

npx -y playwright@"$PLAYWRIGHT_VERSION" install --with-deps


uv tool install ruff@"$RUFF_VERSION"



# # Install skills
# # https://skills.sh/
# # https://github.com/vercel-labs/skills
# # npx -y skills install upstash/context7 --project --agent antigravity-cli --agent opencode --skill find-docs
# for skill in \
#     "JohnStrunk/skills@ears-gherkin-dev" \
#     "upstash/context7@find-docs" \
# ; do
#     source="$(echo "$skill" | cut -d '@' -f 1)"
#     skillname="$(echo "$skill" | cut -d '@' -f 2)"
#     ( cd .. && \
#     npx -y skills install "$source" --project \
#         --agent antigravity-cli \
#         --agent opencode \
#         --skill "$skillname"
#     )
# done
