#!/usr/bin/env bash
# Host setup script to register credential providers with the OpenShell Gateway.
# Run once on host (or whenever host tokens refresh).
set -euo pipefail

# Enable OpenShell Gateway v2 providers
openshell settings set --global --key providers_v2_enabled --value true --yes

#####  gcloud  #####
# gcloud provider is used for direct access to Vertex AI
if [ -n "${GOOGLE_CLOUD_PROJECT:-}" ] && [ -n "${VERTEX_LOCATION:-}" ]; then
  # If gcloud is installed, ensure ADC is available for the provider to use
  if command -v gcloud >/dev/null 2>&1; then
    gcloud auth application-default print-access-token >/dev/null 2>&1 || \
      gcloud auth application-default login --quiet || \
      true
  fi
  echo "==> Setting up Google Cloud provider (gcloud)"
  openshell provider delete gcloud 2>/dev/null || true
  openshell provider create --name gcloud \
    --type google-cloud \
    --from-gcloud-adc \
    --config project_id="$GOOGLE_CLOUD_PROJECT" \
    --config region="$VERTEX_LOCATION"
fi

#####  github  #####
# Enable access to GitHub via the gh CLI if available and authenticated
if command -v gh >/dev/null 2>&1 && gh auth token >/dev/null 2>&1; then
  echo "==> Setting up GitHub provider (github)"
  GITHUB_TOKEN="$(gh auth token)"
  export GITHUB_TOKEN
  openshell provider delete github 2>/dev/null || true
  openshell provider create \
    --name github \
    --type github \
    --from-existing
fi

#####  litemaas  #####
# Enable access to LiteMaaS if LITEMAAS_API_KEY is set. This also requires
# that opencode is configured to use LiteMaaS as a custom provider and that it
# tries to read the API key from the environment variable LITEMAAS_API_KEY.
if [ -n "${LITEMAAS_API_KEY:-}" ]; then
  echo "==> Setting up LiteMaaS provider (litemaas)"
  openshell provider delete litemaas 2>/dev/null || true
  openshell provider create --name litemaas \
    --type generic \
    --credential LITEMAAS_API_KEY="$LITEMAAS_API_KEY"
fi

# if command -v gws >/dev/null 2>&1; then
#   echo "==> Setting up Google Workspace provider (gws-creds)"
#   TMP_GWS_CREDS="$(mktemp)"
#   if gws auth export --unmasked > "$TMP_GWS_CREDS" 2>/dev/null; then
#     openshell provider delete gws-creds 2>/dev/null || true
#     openshell provider create --name gws-creds --type generic \
#       --credential GWS_CREDENTIALS_JSON="$(cat "$TMP_GWS_CREDS")"
#   fi
#   rm -f "$TMP_GWS_CREDS"
# fi

# if [ -n "${ATLASSIAN_API_TOKEN:-}" ]; then
#   echo "==> Setting up Atlassian provider (atlassian)"
#   openshell provider delete atlassian 2>/dev/null || true
#   openshell provider create --name atlassian --type generic \
#     --credential ATLASSIAN_API_TOKEN="$ATLASSIAN_API_TOKEN"
# fi

echo "==> Gateway providers configuration complete."
