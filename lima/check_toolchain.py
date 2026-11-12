#!/usr/bin/env python3
"""Verify the Lima VM's manifest-pinned tools and operator prerequisites."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import unicodedata
from collections.abc import Callable, Sequence
from functools import partial
from pathlib import Path

Command = tuple[str, ...]
Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]

# The minimal PATH every process can rely on. /usr/local/bin is on it, so the
# system-wide devbox-toolchain-check wrapper written by lima/provision-system.sh
# stays reachable without the login-shell toolchain profile (issue #494).
_SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


_VERSION_COMMANDS: dict[str, tuple[Command, re.Pattern[str]]] = {
    "hadolint": (("hadolint", "--version"), re.compile(r"\bv?(\d+\.\d+\.\d+)\b")),
    "go": (("go", "version"), re.compile(r"\bgo(\d+\.\d+\.\d+)\b")),
    "gocyclo": (
        ("go", "version", "-m"),
        # Keep this accepted version grammar in sync with lima/provision-tools.sh.
        re.compile(
            r"(?m)^[ \t]*mod[ \t]+github\.com/fzipp/gocyclo[ \t]+"
            r"v?(\d+\.\d+\.\d+(?:[+-][A-Za-z0-9.]+)?)(?=[ \t]|$)"
        ),
    ),
    "golangci_lint": (
        ("golangci-lint", "--version"),
        re.compile(r"\bgolangci-lint has version v?(\d+\.\d+\.\d+)\b"),
    ),
    "actionlint": (
        ("actionlint", "-version"),
        re.compile(r"\bv?(\d+\.\d+\.\d+)\b"),
    ),
    "yamllint": (
        ("yamllint", "--version"),
        re.compile(r"\bv?(\d+\.\d+\.\d+)\b"),
    ),
    "node": (("node", "--version"), re.compile(r"\bv?(\d+\.\d+\.\d+)\b")),
    "ruby": (("ruby", "--version"), re.compile(r"\bruby\s+(\d+\.\d+\.\d+)\b")),
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
    "minikube": (
        ("minikube", "version"),
        re.compile(r"\bminikube version:\s*v?(\d+\.\d+\.\d+)\b"),
    ),
    "kubectl": (("kubectl", "version", "--client", "-o", "json"), re.compile(r"")),
    "krew": (
        ("kubectl", "krew", "version"),
        re.compile(r"(?m)^GitTag\s*:?\s*v?(\d+\.\d+\.\d+)\s*$"),
    ),
    "pipenv": (("pipenv", "--version"), re.compile(r"\b(\d{4}\.\d+\.\d+)\b")),
    "repomix": (("repomix", "--version"), re.compile(r"\bv?(\d+\.\d+\.\d+)\b")),
    "ast_grep": (("ast-grep", "--version"), re.compile(r"\bv?(\d+\.\d+\.\d+)\b")),
    "semble": (("semble", "--version"), re.compile(r"\bv?(\d+\.\d+\.\d+)\b")),
    "acli": (("acli", "--version"), re.compile(r"\b(\d+\.\d+\.\d+(?:-[\w.]+)?)\b")),
    "google_workspace_cli": (
        ("gws", "--version"),
        re.compile(r"\bv?(\d+\.\d+\.\d+)\b"),
    ),
    "mermaid_cli": (
        ("mmdc", "--version"),
        re.compile(r"\bv?(\d+\.\d+\.\d+)\b"),
    ),
    "coderabbit_cli": (
        ("coderabbit", "--version"),
        re.compile(r"\bv?(\d+\.\d+\.\d+(?:[+-][A-Za-z0-9.]+)?)\b"),
    ),
    "pre_commit": (
        ("pre-commit", "--version"),
        re.compile(r"\bpre-commit\s+v?(\d+\.\d+\.\d+)\b"),
    ),
}

_OPERATOR_COMMANDS: dict[str, Command] = {
    "gcc": ("gcc", "--version"),
    "g++": ("g++", "--version"),
    "make": ("make", "--version"),
    "cmake": ("cmake", "--version"),
    "gdb": ("gdb", "--version"),
    "ninja": ("ninja", "--version"),
    "pkg-config": ("pkg-config", "--version"),
    "python3": ("python3", "--version"),
    "pip": ("python3", "-m", "pip", "--version"),
    "shellcheck": ("shellcheck", "--version"),
}


class _ManifestError(Exception):
    """The tool manifest could not be read."""


@dataclasses.dataclass(frozen=True)
class ToolRecord:
    """The check result for one entry (issue #494 machine-readable form)."""

    name: str
    command: Command | None
    pinned: str | None
    installed: str | None
    path: str | None
    status: str  # "ok", "missing", "mismatch", or "error"
    detail: str


def _home_directory() -> str:
    """Return the account's home directory even when HOME is unset."""
    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:
        return os.environ.get("HOME", "")


def canonical_search_path() -> str:
    """Return the PATH that finds every devbox-managed tool (issue #494).

    Login shells get this order from /etc/profile.d/devbox-toolchain.sh.
    The checker must reach the same directories without that profile, so the
    inherited PATH is kept and the canonical directories are added around it.
    """
    home = _home_directory()
    krew_root = os.environ.get("KREW_ROOT") or f"{home}/.krew"
    cargo_home = os.environ.get("CARGO_HOME") or f"{home}/.cargo"
    gopath = os.environ.get("GOPATH") or f"{home}/.cache/go"
    entries = [
        f"{krew_root}/bin",
        f"{home}/.local/bin",
        f"{cargo_home}/bin",
        "/var/lib/devbox-toolbuilder/.local/bin",
        "/var/lib/devbox-toolbuilder/.cargo/bin",
        "/usr/local/node/bin",
        "/usr/local/go/bin",
        f"{gopath}/bin",
        *os.environ.get("PATH", "").split(os.pathsep),
        *_SYSTEM_PATH.split(os.pathsep),
    ]
    return os.pathsep.join(dict.fromkeys(entry for entry in entries if entry))


def _normalize_tool_name(name: str) -> str:
    """Map a manifest name and its command spelling to one form."""
    return name.replace("-", "_")


def _accepted_tool_spellings() -> set[str]:
    """Every name --tool accepts: manifest, command, and operator names."""
    spellings: set[str] = set(_OPERATOR_COMMANDS)
    for name in _VERSION_COMMANDS:
        spellings.add(name)
        if "_" in name:
            spellings.add(name.replace("_", "-"))
    return spellings


def _default_runner(
    command: Sequence[str], search_path: str | None = None
) -> subprocess.CompletedProcess[str]:
    # Repair PATH so a command resolved through the canonical search path
    # also executes when the caller inherited a minimal PATH. An injected
    # search path keeps resolution and execution on the same directories.
    repaired = search_path if search_path is not None else canonical_search_path()
    env = {**os.environ, "PATH": repaired}
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


def _without_control_characters(text: str) -> str:
    """Strip control characters, keeping tabs and newlines (issue #494).

    Version output can contain terminal escape sequences or other control
    bytes; they must not reach record details or the JSON output.
    """
    return "".join(
        character
        for character in text
        if character in "\t\n" or unicodedata.category(character) != "Cc"
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


def _version_command(name: str, search_path: str) -> tuple[Command | None, str | None]:
    """Resolve the version command and the checked tool's executable path."""
    command = _VERSION_COMMANDS[name][0]
    if name == "gocyclo":
        # gocyclo has no version flag; `go version -m` reads its module
        # version from the binary itself, so its path must be resolved.
        executable = shutil.which("gocyclo", path=search_path)
        if executable is None:
            return None, None
        return (*command, executable), executable
    executable = shutil.which(command[0], path=search_path)
    if executable is None:
        return None, None
    return command, executable


def _check_manifest_tool(
    name: str, spec: object, runner: Runner, search_path: str
) -> ToolRecord:
    expected = spec.get("version") if isinstance(spec, dict) else None
    version_check = _VERSION_COMMANDS.get(name)
    if not isinstance(expected, str) or version_check is None:
        return ToolRecord(
            name=name,
            command=None,
            pinned=expected if isinstance(expected, str) else None,
            installed=None,
            path=None,
            status="error",
            detail="no version check is defined for this Lima tool",
        )
    pinned = expected.removeprefix("v")
    command, executable = _version_command(name, search_path)
    if command is None:
        return ToolRecord(
            name=name,
            # The record keeps the full version command, not only the
            # executable name (issue #494 review).
            command=version_check[0],
            pinned=pinned,
            installed=None,
            path=None,
            status="missing",
            detail="executable is not available on PATH",
        )
    try:
        result = runner(command)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ToolRecord(
            name=name,
            command=command,
            pinned=pinned,
            installed=None,
            path=executable,
            status="error",
            detail=f"could not run {' '.join(command)}: {exc}",
        )
    output = _without_control_characters(
        "\n".join((result.stdout or "", result.stderr or ""))
    )
    if result.returncode != 0:
        return ToolRecord(
            name=name,
            command=command,
            pinned=pinned,
            installed=None,
            path=executable,
            status="error",
            detail=(
                f"{' '.join(command)} exited {result.returncode}: {output.strip()}"
            ),
        )
    installed = _actual_version(name, output)
    if installed != pinned:
        return ToolRecord(
            name=name,
            command=command,
            pinned=pinned,
            installed=installed,
            path=executable,
            status="mismatch",
            detail=(
                f"installed version {installed or '<unrecognized>'}, expected {pinned}"
            ),
        )
    return ToolRecord(
        name=name,
        command=command,
        pinned=pinned,
        installed=installed,
        path=executable,
        status="ok",
        detail=installed,
    )


def _check_operator_command(
    name: str, command: Command, runner: Runner, search_path: str
) -> ToolRecord:
    executable = shutil.which(command[0], path=search_path)
    if executable is None:
        return ToolRecord(
            name=name,
            command=command,
            pinned=None,
            installed=None,
            path=None,
            status="missing",
            detail="executable is not available on PATH",
        )
    try:
        result = runner(command)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ToolRecord(
            name=name,
            command=command,
            pinned=None,
            installed=None,
            path=executable,
            status="error",
            detail=f"could not run {' '.join(command)}: {exc}",
        )
    output = _without_control_characters(
        "\n".join((result.stdout or "", result.stderr or ""))
    ).strip()
    if result.returncode != 0:
        return ToolRecord(
            name=name,
            command=command,
            pinned=None,
            installed=None,
            path=executable,
            status="error",
            detail=f"{' '.join(command)} exited {result.returncode}: {output}",
        )
    report = output.splitlines()[0] if output else "available"
    return ToolRecord(
        name=name,
        command=command,
        pinned=None,
        installed=report,
        path=executable,
        status="ok",
        detail=report,
    )


def check_toolchain_records(
    manifest_path: Path,
    runner: Runner = _default_runner,
    *,
    skip_tools: frozenset[str] = frozenset(),
    only_tools: frozenset[str] = frozenset(),
    search_path: str | None = None,
) -> list[ToolRecord]:
    """Check the toolchain and return one record per checked entry."""
    resolved_path = canonical_search_path() if search_path is None else search_path
    if runner is _default_runner:
        # Resolution and execution must use the same directories (issue #494).
        runner = partial(_default_runner, search_path=resolved_path)
    try:
        manifest = json.loads(manifest_path.read_text())
        tools = manifest["tools"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise _ManifestError(
            f"unable to read tool manifest {manifest_path}: {exc}"
        ) from exc
    if not isinstance(tools, dict):
        raise _ManifestError(f"tool manifest {manifest_path} has no tools object")

    requested = {_normalize_tool_name(name) for name in only_tools}
    declared = sorted(
        name
        for name, spec in tools.items()
        if isinstance(spec, dict)
        and isinstance(spec.get("consumers"), dict)
        and spec["consumers"].get("lima") is True
    )
    records: list[ToolRecord] = []
    for name in declared:
        if name in skip_tools:
            continue
        if requested and _normalize_tool_name(name) not in requested:
            continue
        records.append(_check_manifest_tool(name, tools[name], runner, resolved_path))
    for name, command in _OPERATOR_COMMANDS.items():
        if requested and _normalize_tool_name(name) not in requested:
            continue
        records.append(_check_operator_command(name, command, runner, resolved_path))
    if requested:
        matched = {_normalize_tool_name(record.name) for record in records}
        for name in sorted(requested - matched):
            # A requested name with no record must not exit 0 (issue #494).
            records.append(
                ToolRecord(
                    name=name,
                    command=None,
                    pinned=None,
                    installed=None,
                    path=None,
                    status="error",
                    detail="no checked entry matches this tool name",
                )
            )
    return records


def check_toolchain(
    manifest_path: Path,
    runner: Runner = _default_runner,
    *,
    skip_tools: frozenset[str] = frozenset(),
    only_tools: frozenset[str] = frozenset(),
    search_path: str | None = None,
) -> tuple[list[str], list[str]]:
    """Return human-readable successes and errors for the guest toolchain."""
    try:
        records = check_toolchain_records(
            manifest_path,
            runner,
            skip_tools=skip_tools,
            only_tools=only_tools,
            search_path=search_path,
        )
    except _ManifestError as exc:
        return [], [str(exc)]
    successes = [
        f"{record.name}: {record.detail}" for record in records if record.status == "ok"
    ]
    errors = [
        f"{record.name}: {record.detail}" for record in records if record.status != "ok"
    ]
    return successes, errors


def _records_payload(records: Sequence[ToolRecord], search_path: str) -> dict:
    inherited = os.environ.get("PATH", "")
    inherited_directories = set(inherited.split(os.pathsep))
    return {
        "search_path": search_path,
        "inherited_path": inherited,
        "missing_from_inherited_path": [
            directory
            for directory in search_path.split(os.pathsep)
            if directory not in inherited_directories
        ],
        "tools": [dataclasses.asdict(record) for record in records],
    }


def main(argv: Sequence[str] | None = None, runner: Runner = _default_runner) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("/etc/devbox/tool-versions.json"),
        help="manifest used by Lima provisioning",
    )
    parser.add_argument(
        "--skip-tool",
        action="append",
        default=[],
        choices=sorted(_VERSION_COMMANDS),
        help="skip a Lima-managed tool installed in a later provisioning phase",
    )
    parser.add_argument(
        "--tool",
        action="append",
        default=[],
        choices=sorted(_accepted_tool_spellings()),
        metavar="NAME",
        help="check only the named tool (manifest or command name)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print one JSON object with a record per checked entry",
    )
    args = parser.parse_args(argv)
    search_path = canonical_search_path()
    try:
        records = check_toolchain_records(
            args.manifest,
            runner,
            skip_tools=frozenset(args.skip_tool),
            only_tools=frozenset(_normalize_tool_name(name) for name in args.tool),
            search_path=search_path,
        )
    except _ManifestError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(_records_payload(records, search_path), indent=2))
    else:
        for record in records:
            if record.status == "ok":
                print(f"OK: {record.name}: {record.detail}")
        for record in records:
            if record.status != "ok":
                print(f"ERROR: {record.name}: {record.detail}", file=sys.stderr)
    return 1 if any(record.status != "ok" for record in records) else 0


if __name__ == "__main__":
    raise SystemExit(main())
