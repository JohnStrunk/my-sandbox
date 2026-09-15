#!/usr/bin/env python3
"""Report resource limits before starting resource-intensive tests."""

from __future__ import annotations

import argparse
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path

INFRASTRUCTURE_EXIT = 125
DEFAULT_CGROUP_ROOT = Path("/sys/fs/cgroup")
MIN_PID_LIMIT = 4096
MIN_PID_HEADROOM = 512
MIN_CPU_LIMIT = 2.0
MIN_MEMORY_LIMIT = 2 * 1024**3
MIN_MEMORY_HEADROOM = 512 * 1024**2
UNLIMITED_MEMORY_SENTINEL = 1 << 60


@dataclass(frozen=True)
class ResourceReport:
    """The resource values used to classify a test environment."""

    cgroup_root_present: bool
    pids_limit: int | None
    pids_current: int | None
    pids_limit_known: bool
    cpu_limit: float
    cpu_limited: bool
    cpu_limit_known: bool
    cpu_source: str
    host_cpu_count: int
    memory_limit: int | None
    memory_current: int | None
    memory_limit_known: bool
    host_memory: int | None


def _read_first(root: Path, paths: tuple[str, ...]) -> str | None:
    for relative_path in paths:
        try:
            value = (root / relative_path).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            continue
        if value:
            return value
    return None


def _read_first_status(root: Path, paths: tuple[str, ...]) -> tuple[str | None, bool]:
    """Return the first value and whether a candidate file was found."""
    for relative_path in paths:
        try:
            value = (root / relative_path).read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError):
            return None, True
        return (value or None), True
    return None, False


def _read_first_from_roots(
    roots: tuple[Path, ...], paths: tuple[str, ...]
) -> str | None:
    for root in roots:
        value = _read_first(root, paths)
        if value is not None:
            return value
    return None


def _read_first_from_roots_status(
    roots: tuple[Path, ...], paths: tuple[str, ...]
) -> tuple[str | None, bool]:
    for root in roots:
        value, found = _read_first_status(root, paths)
        if found:
            return value, True
    return None, False


def _current_cgroup_paths() -> dict[str, tuple[str, ...]]:
    paths: dict[str, list[str]] = {}
    try:
        lines = Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return {}
    for line in lines:
        fields = line.split(":", 2)
        if len(fields) != 3:
            continue
        _, controllers, relative_path = fields
        key = "unified" if not controllers else controllers
        paths.setdefault(key, []).append(relative_path)
    return {key: tuple(values) for key, values in paths.items()}


def _join_cgroup_path(root: Path, relative_path: str) -> Path:
    if relative_path in ("", "/"):
        return root
    return root / relative_path.lstrip("/")


def _resource_roots(
    cgroup_root: Path, controller: str, *, include_fallback: bool = True
) -> tuple[Path, ...]:
    roots: list[Path] = []
    if cgroup_root == DEFAULT_CGROUP_ROOT:
        current_paths = _current_cgroup_paths()
        for relative_path in current_paths.get("unified", ()):
            roots.append(_join_cgroup_path(cgroup_root, relative_path))
        for mount, relative_paths in current_paths.items():
            if mount == "unified":
                continue
            mounted_controllers = set(mount.split(","))
            if controller not in mounted_controllers:
                continue
            for relative_path in relative_paths:
                roots.append(_join_cgroup_path(cgroup_root / mount, relative_path))
                roots.append(_join_cgroup_path(cgroup_root / controller, relative_path))
    if not roots:
        roots.extend((cgroup_root / controller, cgroup_root))
    if include_fallback:
        roots.append(cgroup_root / controller)
        if controller == "cpu":
            roots.extend((cgroup_root / "cpu,cpuacct", cgroup_root / "cpuset"))
        roots.append(cgroup_root)
    unique_roots: list[Path] = []
    for root in roots:
        if root not in unique_roots:
            unique_roots.append(root)
    return tuple(unique_roots)


def _parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value.split()[0])
    except (IndexError, ValueError):
        return None


def _host_memory() -> int | None:
    try:
        lines = Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    for line in lines:
        name, separator, value = line.partition(":")
        if separator and name == "MemTotal":
            amount = _parse_int(value)
            return amount * 1024 if amount is not None else None
    return None


def _parse_cpu_set(value: str | None) -> int | None:
    if not value:
        return None
    count = 0
    try:
        for item in value.split(","):
            item = item.strip()
            if "-" in item:
                start, end = item.split("-", 1)
                if not start.isdigit() or not end.isdigit():
                    return None
                start_value = int(start)
                end_value = int(end)
                if start_value > end_value:
                    return None
                count += end_value - start_value + 1
            else:
                if not item.isdigit():
                    return None
                count += 1
    except ValueError:
        return None
    return count if count > 0 else None


def _cpu_limit(
    roots: tuple[Path, ...], host_cpu_count: int
) -> tuple[float, bool, bool, str]:
    limits = [float(host_cpu_count)]
    sources: list[str] = []
    quota_known = False
    quota_invalid = False

    cgroup_v2, cgroup_v2_found = _read_first_from_roots_status(roots, ("cpu.max",))
    if cgroup_v2_found:
        if cgroup_v2 is not None:
            fields = cgroup_v2.split()
            period = _parse_int(fields[1]) if len(fields) >= 2 else None
            quota = _parse_int(fields[0]) if fields else None
            if period and (fields[0] == "max" or quota is not None and quota > 0):
                quota_known = True
                if fields[0] != "max" and quota is not None:
                    limits.append(quota / period)
                    sources.append("cgroup quota")
            else:
                quota_invalid = True
        else:
            quota_invalid = True
    else:
        quota, quota_found = _read_first_from_roots_status(
            roots, ("cpu.cfs_quota_us", "cpu/cpu.cfs_quota_us")
        )
        period, period_found = _read_first_from_roots_status(
            roots, ("cpu.cfs_period_us", "cpu/cpu.cfs_period_us")
        )
        parsed_quota = _parse_int(quota)
        parsed_period = _parse_int(period)
        if parsed_quota == -1 and parsed_period is not None and parsed_period > 0:
            quota_known = True
        elif parsed_quota is not None and parsed_period and parsed_quota > 0:
            quota_known = True
            limits.append(parsed_quota / parsed_period)
            sources.append("cgroup quota")
        elif quota_found or period_found:
            quota_invalid = True

    cpuset, cpuset_found = _read_first_from_roots_status(
        roots, ("cpuset.cpus.effective", "cpuset.cpus", "cpuset/cpuset.cpus")
    )
    parsed_cpuset = _parse_cpu_set(cpuset)
    if parsed_cpuset is not None:
        limits.append(float(parsed_cpuset))
        sources.append("cpuset")
        cpu_limit_known = not quota_invalid
    elif cpuset_found:
        cpu_limit_known = False
    else:
        cpu_limit_known = quota_known and not quota_invalid

    cpu_limit = min(limits)
    source = "+".join(sources) if sources else "host capacity"
    if not cpu_limit_known:
        source = "unknown; host fallback"
    return cpu_limit, bool(sources), cpu_limit_known, source


def _memory_limit(roots: tuple[Path, ...]) -> tuple[int | None, bool]:
    raw_value, found = _read_first_from_roots_status(
        roots, ("memory.max", "memory.limit_in_bytes", "memory/memory.limit_in_bytes")
    )
    if not found or raw_value is None:
        return None, False
    if raw_value.split()[0] == "max":
        return None, True
    value = _parse_int(raw_value)
    if value is None or value < 0:
        return None, False
    if value >= UNLIMITED_MEMORY_SENTINEL:
        return None, True
    return value, True


def inspect_resources(cgroup_root: Path = DEFAULT_CGROUP_ROOT) -> ResourceReport:
    """Read cgroup limits, falling back to host capacity when unlimited."""
    try:
        host_cpu_count = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        host_cpu_count = os.cpu_count() or 1
    host_cpu_count = max(1, host_cpu_count)
    pids_roots = _resource_roots(cgroup_root, "pids")
    cpu_roots = _resource_roots(cgroup_root, "cpu") + _resource_roots(
        cgroup_root, "cpuset"
    )
    memory_roots = _resource_roots(cgroup_root, "memory")
    pids_current_roots = _resource_roots(cgroup_root, "pids", include_fallback=False)
    memory_current_roots = _resource_roots(
        cgroup_root, "memory", include_fallback=False
    )
    pids_raw, pids_limit_known = _read_first_from_roots_status(
        pids_roots, ("pids.max",)
    )
    pids_limit = _parse_int(pids_raw)
    pids_limit_known = pids_limit_known and (
        pids_limit is not None or pids_raw == "max"
    )
    pids_current = _parse_int(
        _read_first_from_roots(pids_current_roots, ("pids.current",))
    )
    cpu_limit, cpu_limited, cpu_limit_known, cpu_source = _cpu_limit(
        cpu_roots, host_cpu_count
    )
    memory_limit, memory_limit_known = _memory_limit(memory_roots)
    memory_current = _parse_int(
        _read_first_from_roots(
            memory_current_roots, ("memory.current", "memory.usage_in_bytes")
        )
    )
    cgroup_root_present = cgroup_root.exists()
    return ResourceReport(
        cgroup_root_present=cgroup_root_present,
        pids_limit=pids_limit,
        pids_current=pids_current,
        pids_limit_known=pids_limit_known,
        cpu_limit=cpu_limit,
        cpu_limited=cpu_limited,
        cpu_limit_known=cpu_limit_known,
        cpu_source=cpu_source,
        host_cpu_count=host_cpu_count,
        memory_limit=memory_limit,
        memory_current=memory_current,
        memory_limit_known=memory_limit_known,
        host_memory=_host_memory(),
    )


def _constrained_resources(report: ResourceReport) -> tuple[str, ...]:
    constrained: list[str] = []
    if not report.cgroup_root_present:
        constrained.append("cgroup root unavailable")
    unavailable = []
    if not report.pids_limit_known:
        unavailable.append("PID")
    if not report.cpu_limit_known:
        unavailable.append("CPU")
    if not report.memory_limit_known:
        unavailable.append("memory")
    if unavailable:
        constrained.append("unavailable limits: " + ", ".join(unavailable))
    if report.pids_limit is not None:
        headroom = (
            report.pids_limit - report.pids_current
            if report.pids_current is not None
            else None
        )
        if report.pids_limit <= MIN_PID_LIMIT or (
            headroom is not None and headroom <= MIN_PID_HEADROOM
        ):
            constrained.append("PID")
    if report.cpu_limit < MIN_CPU_LIMIT:
        constrained.append("CPU")
    if report.memory_limit is not None:
        headroom = (
            report.memory_limit - report.memory_current
            if report.memory_current is not None
            else None
        )
        if report.memory_limit < MIN_MEMORY_LIMIT or (
            headroom is not None and headroom <= MIN_MEMORY_HEADROOM
        ):
            constrained.append("memory")
    if report.pids_limit is not None and report.pids_current is None:
        constrained.append("PID usage unavailable")
    if report.memory_limit is not None and report.memory_current is None:
        constrained.append("memory usage unavailable")
    return tuple(constrained)


def _recommended_parallelism(
    report: ResourceReport, constrained: tuple[str, ...]
) -> tuple[int, int, int]:
    if not report.cpu_limit_known:
        return 1, 1, 1
    gomaxprocs = max(1, min(2, math.floor(report.cpu_limit)))
    if constrained:
        return gomaxprocs, 1, 1
    return gomaxprocs, gomaxprocs, gomaxprocs


def _format_bytes(value: int | None) -> str:
    if value is None:
        return "unlimited"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.2f} {unit}"
        amount /= 1024
    return f"{value} B"


def _format_limit(value: int | None, known: bool) -> str:
    return _format_bytes(value) if known else "unknown"


def _print_report(report: ResourceReport, constrained: tuple[str, ...]) -> None:
    pids_limit = (
        str(report.pids_limit)
        if report.pids_limit is not None and report.pids_limit_known
        else "unknown"
        if not report.pids_limit_known
        else "unlimited"
    )
    if report.pids_current is None:
        pids_current = "unknown"
        pids_available = "unknown"
    else:
        pids_current = str(report.pids_current)
        pids_available = (
            str(report.pids_limit - report.pids_current)
            if report.pids_limit is not None
            else "unlimited"
            if report.pids_limit_known
            else "unknown"
        )
    cpu_limit = f"{report.cpu_limit:.2f}" if report.cpu_limit_known else "unknown"
    memory_current = (
        _format_bytes(report.memory_current)
        if report.memory_current is not None
        else "unknown"
    )
    host_memory = _format_bytes(report.host_memory)

    print(
        f"resource-preflight: PID limit={pids_limit} current={pids_current} "
        f"available={pids_available}",
        file=sys.stderr,
    )
    print(
        f"resource-preflight: CPU limit={cpu_limit} CPUs "
        f"(host={report.host_cpu_count}, source={report.cpu_source})",
        file=sys.stderr,
    )
    print(
        "resource-preflight: memory limit="
        f"{_format_limit(report.memory_limit, report.memory_limit_known)} "
        f"current={memory_current} host={host_memory}",
        file=sys.stderr,
    )

    gomaxprocs, go_parallelism, ginkgo_nodes = _recommended_parallelism(
        report, constrained
    )
    if constrained:
        resources = ", ".join(constrained)
        print(
            "resource-preflight: constrained resource budget detected "
            f"({resources}); this is an infrastructure limitation.",
            file=sys.stderr,
        )
        print(
            "resource-preflight: recommended command: "
            f"env GOMAXPROCS={gomaxprocs} go test -p {go_parallelism} ./...",
            file=sys.stderr,
        )
        print(
            "resource-preflight: recommended Ginkgo setting: "
            f"GOMAXPROCS={gomaxprocs} with --nodes={ginkgo_nodes}",
            file=sys.stderr,
        )
    else:
        print(
            "resource-preflight: resource budget supports standard parallel "
            "test execution.",
            file=sys.stderr,
        )


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Report cgroup resources before running parallel tests."
    )
    parser.add_argument(
        "--cgroup-root",
        type=Path,
        default=DEFAULT_CGROUP_ROOT,
        help="cgroup hierarchy to inspect (default: /sys/fs/cgroup)",
    )
    parser.add_argument(
        "--fail-on-constrained",
        action="store_true",
        help="return status 125 without starting the test command",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    report = inspect_resources(args.cgroup_root)
    constrained = _constrained_resources(report)
    _print_report(report, constrained)
    return INFRASTRUCTURE_EXIT if constrained and args.fail_on_constrained else 0


if __name__ == "__main__":
    sys.exit(main())
