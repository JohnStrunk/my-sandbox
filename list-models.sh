#!/usr/bin/env bash

set -euo pipefail

if [[ -z "${ENMAAS_URL:-}" || -z "${ENMAAS_API_KEY:-}" ]]; then
  printf 'Set ENMAAS_URL and ENMAAS_API_KEY before listing models\n' >&2
  exit 2
fi

if [[ "${ENMAAS_URL,,}" != https://* ]]; then
  printf 'ENMAAS_URL must use HTTPS; refusing to send credentials\n' >&2
  exit 2
fi

url_authority="${ENMAAS_URL#*://}"
url_authority="${url_authority%%[/?#]*}"
if [[ -z "$url_authority" ]]; then
  printf 'ENMAAS_URL must be a valid HTTPS URL\n' >&2
  exit 2
fi

base_url="$ENMAAS_URL"
while [[ "$base_url" == */ ]]; do
  base_url="${base_url%/}"
done
models_url="${base_url}/models"
curl_flags=(
  --proto '=https'
  --fail
  --silent
  --show-error
  --connect-timeout 5
  --max-time 15
)
listing_failed=0

# Do not follow redirects: credentials should go only to the configured HTTPS URL.
printf '%s\n' '===== Models via Anthropic-compatible endpoint ====='
if ! curl "${curl_flags[@]}" \
  -H "x-api-key: ${ENMAAS_API_KEY}" \
  -H 'anthropic-version: 2023-06-01' \
  "$models_url" \
  | jq .; then
  printf 'Anthropic-compatible model listing failed\n' >&2
  listing_failed=1
fi

printf '%s\n' '===== Models via OpenAI-compatible endpoint ====='
if ! curl "${curl_flags[@]}" \
  -H "Authorization: Bearer ${ENMAAS_API_KEY}" \
  "$models_url" \
  | jq .; then
  printf 'OpenAI-compatible model listing failed\n' >&2
  listing_failed=1
fi

exit "$listing_failed"
