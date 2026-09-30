#!/usr/bin/env python3
"""Classify KVM and nested-virtualization prerequisites for VM test tiers."""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

INFRASTRUCTURE_LIMIT = 125
NESTED_PARAMETERS = (
    Path("/sys/module/kvm_intel/parameters/nested"),
    Path("/sys/module/kvm_amd/parameters/nested"),
)


@dataclass(frozen=True)
class CapabilityResult:
    available: bool
    reasons: tuple[str, ...]


def check_vm_capabilities(
    *,
    require_nested: bool = False,
    kvm_device: Path = Path("/dev/kvm"),
    nested_parameters: tuple[Path, ...] = NESTED_PARAMETERS,
) -> CapabilityResult:
    """Check observable host capabilities without starting a VM."""
    reasons: list[str] = []
    try:
        is_character_device = kvm_device.is_char_device()
        accessible = os.access(kvm_device, os.R_OK | os.W_OK)
    except OSError:
        is_character_device = accessible = False
    if not is_character_device or not accessible:
        reasons.append(f"{kvm_device} is missing or not accessible to this user")

    if require_nested:
        observed: list[tuple[Path, str]] = []
        for parameter in nested_parameters:
            try:
                value = parameter.read_text(encoding="ascii").strip().lower()
            except OSError:
                continue
            observed.append((parameter, value))
        enabled = [(path, value) for path, value in observed if value in {"y", "1"}]
        if not enabled:
            if observed:
                detail = ", ".join(f"{path}={value}" for path, value in observed)
                reasons.append(f"nested KVM is disabled ({detail})")
            else:
                reasons.append(
                    "cannot read /sys/module/kvm_intel/parameters/nested or "
                    "/sys/module/kvm_amd/parameters/nested"
                )

    return CapabilityResult(not reasons, tuple(reasons))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="also require the host KVM module's nested virtualization setting",
    )
    args = parser.parse_args()
    result = check_vm_capabilities(require_nested=args.recursive)
    if result.available:
        print("vm-preflight: required KVM capabilities are available")
        return 0
    print(
        "vm-preflight: infrastructure limitation: " + "; ".join(result.reasons),
        file=sys.stderr,
    )
    return INFRASTRUCTURE_LIMIT


if __name__ == "__main__":
    raise SystemExit(main())
