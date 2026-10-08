#!/usr/bin/env python3
"""Verify the Lima VM's manifest-pinned tools and operator prerequisites."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

Command = tuple[str, ...]
Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


_VERSION_COMMANDS: dict[str, tuple[Command, re.Pattern[str]]] = {
    "hadolint": (("hadolint", "--version"), re.compile(r"\bv?(\d+\.\d+\.\d+)\b")),
    "go": (("go", "version"), re.compile(r"\bgo(\d+\.\d+\.\d+)\b")),
    "node": (("node", "--version"), re.compile(r"\bv?(\d+\.\d+\.\d+)\b")),
    "rust": (("rustc", "--version"), re.compile(r"\brustc\s+v?(\d+\.\d+\.\d+)\b")),
    "rustup": (("rustup", "--version"), re.compile(r"\brustup\s+v?(\d+\.\d+\.\d+)\b")),
    "uv": (("uv", "--version"), re.compile(r"\buv\s+(\d+\.\d+\.\d+)\b")),
    "markdownlint_cli2": (
        ("markdownlint-cli2", "--version"),
        re.compile(r"\bv?(\d+\.\d+\.\d+)\b"),
    ),
    "playwright_cli": (
        ("playwright-cli", "--version"),
        re.compile(r"\bv?(\d+\.\d+\.\d+)\b"),
    ),
    "antigravity_cli": (
        ("agy", "--version"),
        re.compile(r"\b(\d+\.\d+\.\d+)\b"),
    ),
    "opencode": (("opencode", "--version"), re.compile(r"\bv?(\d+\.\d+\.\d+)\b")),
    "limactl": (("limactl", "--version"), re.compile(r"\bv?(\d+\.\d+\.\d+)\b")),
    "helm": (("helm", "version", "--short"), re.compile(r"\bv?(\d+\.\d+\.\d+)\b")),
    "kind": (("kind", "version"), re.compile(r"\bkind\s+v?(\d+\.\d+\.\d+)\b")),
    "kubectl": (("kubectl", "version", "--client", "-o", "json"), re.compile(r"")),
    "pipenv": (("pipenv", "--version"), re.compile(r"\b(\d{4}\.\d+\.\d+)\b")),
    "repomix": (("repomix", "--version"), re.compile(r"\bv?(\d+\.\d+\.\d+)\b")),
    "ast_grep": (("ast-grep", "--version"), re.compile(r"\bv?(\d+\.\d+\.\d+)\b")),
    "semble": (("semble", "--version"), re.compile(r"\bv?(\d+\.\d+\.\d+)\b")),
    "acli": (("acli", "--version"), re.compile(r"\b(\d+\.\d+\.\d+(?:-[\w.]+)?)\b")),
    "docker_ce": (
        ("docker", "--version"),
        re.compile(r"\bDocker version v?(\d+\.\d+\.\d+)\b"),
    ),
    "containerd_io": (
        ("containerd", "--version"),
        re.compile(r"\bv?(\d+\.\d+\.\d+)\b"),
    ),
    "google_workspace_cli": (
        ("gws", "--version"),
        re.compile(r"\bv?(\d+\.\d+\.\d+)\b"),
    ),
    "pre_commit": (
        ("pre-commit", "--version"),
        re.compile(r"\bpre-commit\s+v?(\d+\.\d+\.\d+)\b"),
    ),
}

_OPERATOR_COMMANDS: dict[str, Command] = {
    "make": ("make", "--version"),
    "python3": ("python3", "--version"),
    "pip": ("python3", "-m", "pip", "--version"),
    "shellcheck": ("shellcheck", "--version"),
}


def _default_runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _actual_version(name: str, output: str) -> str | None:
    if name == "kubectl":
        try:
            payload = json.loads(output)
            value = payload["clientVersion"]["gitVersion"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return None
        return value.removeprefix("v") if isinstance(value, str) else None
    pattern = _VERSION_COMMANDS[name][1]
    match = pattern.search(output)
    return match.group(1) if match else None


def check_toolchain(
    manifest_path: Path,
    runner: Runner = _default_runner,
) -> tuple[list[str], list[str]]:
    """Return human-readable successes and errors for the guest toolchain."""

    try:
        manifest = json.loads(manifest_path.read_text())
        tools = manifest["tools"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        return [], [f"unable to read tool manifest {manifest_path}: {exc}"]
    if not isinstance(tools, dict):
        return [], [f"tool manifest {manifest_path} has no tools object"]

    successes: list[str] = []
    errors: list[str] = []
    declared = sorted(
        name
        for name, spec in tools.items()
        if isinstance(spec, dict)
        and isinstance(spec.get("consumers"), dict)
        and spec["consumers"].get("lima") is True
    )
    for name in declared:
        spec = tools[name]
        expected = spec.get("version")
        command_spec = _VERSION_COMMANDS.get(name)
        if not isinstance(expected, str) or command_spec is None:
            errors.append(f"no version check is defined for Lima tool '{name}'")
            continue
        command = command_spec[0]
        try:
            result = runner(command)
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"{name}: could not run {' '.join(command)}: {exc}")
            continue
        output = "\n".join((result.stdout or "", result.stderr or ""))
        if result.returncode != 0:
            errors.append(
                f"{name}: {' '.join(command)} exited {result.returncode}: "
                f"{output.strip()}"
            )
            continue
        actual = _actual_version(name, output)
        normalized_expected = expected.removeprefix("v")
        if actual != normalized_expected:
            errors.append(
                f"{name}: installed version {actual or '<unrecognized>'}, "
                f"expected {normalized_expected}"
            )
            continue
        successes.append(f"{name}: {actual}")

    for name, command in _OPERATOR_COMMANDS.items():
        try:
            result = runner(command)
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"{name}: could not run {' '.join(command)}: {exc}")
            continue
        output = "\n".join((result.stdout or "", result.stderr or "")).strip()
        if result.returncode != 0:
            errors.append(
                f"{name}: {' '.join(command)} exited {result.returncode}: {output}"
            )
        else:
            report = output.splitlines()[0] if output else "available"
            successes.append(f"{name}: {report}")

    return successes, errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("/etc/devbox/tool-versions.json"),
        help="manifest used by Lima provisioning",
    )
    args = parser.parse_args()
    successes, errors = check_toolchain(args.manifest)
    for line in successes:
        print(f"OK: {line}")
    for line in errors:
        print(f"ERROR: {line}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
