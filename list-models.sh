#! /bin/bash

set -e -o pipefail


curl --fail --silent --show-error --location --connect-timeout 5 --max-time 15 \
  -H "x-api-key: ${PRICETAG_API_KEY}" \
  "${PRICETAG_HOSTED_URL}/models" \
  | jq -r '
      (
        ["Name", "ModelID"],
        (.data[] | [
          (.display_name // error("model is missing a display name")),
          (.id // error("model is missing a model ID"))
        ])
      )
      | @tsv
    ' \
  | column -t -s $'\t'
