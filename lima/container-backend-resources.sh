#!/usr/bin/env bash
# Shared Docker/Podman resource listing and reserved-prefix checks.

container_backend_list() {
  local backend="$1" resource_kind="$2"
  case "$resource_kind" in
    containers)
      "$backend" ps --all --format '{{.Names}} {{.Labels}}'
      ;;
    volumes)
      "$backend" volume ls --format '{{.Name}}'
      ;;
    networks)
      "$backend" network ls --format '{{.Name}}'
      ;;
    *)
      printf 'unsupported container resource kind: %s\n' "$resource_kind" >&2
      return 2
      ;;
  esac
}

container_backend_check_no_stale_resources() {
  local backend="$1" resource_kind="$2" reserved_prefix="$3"
  local validator="$4" display_name="$5" output stale description
  if [[ -n "$display_name" ]]; then
    description="$display_name $resource_kind"
  else
    description="$resource_kind"
  fi
  if ! output="$(container_backend_list "$backend" "$resource_kind")"; then
    printf '%s: could not inspect existing %s (see runtime diagnostic above)\n' \
      "$validator" "$description" >&2
    return 1
  fi
  stale="$(grep -F "$reserved_prefix" <<<"$output" || true)"
  if [[ -n "$stale" ]]; then
    printf '%s: stale %s remain from an interrupted smoke run:\n%s\n' \
      "$validator" "$description" "$stale" >&2
    printf "%s: remove resources with reserved prefix '%s' before retrying\n" \
      "$validator" "$reserved_prefix" >&2
    return 1
  fi
  return 0
}
