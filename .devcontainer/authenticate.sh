#! /bin/bash

# Script to authenticate the user for installed tools

set -e -o pipefail

# https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps
# Default scopes: 'gist', 'read:org', 'repo', 'workflow'
gh auth login -s project
