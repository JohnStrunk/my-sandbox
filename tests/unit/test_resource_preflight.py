"""Verify resource-aware preflight reporting and command gating."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

RESOURCE_SCRIPT = Path(__file__).parents[2] / "scripts" / "resource_preflight.py"
WRAPPER_SCRIPT = Path(__file__).parents[2] / "scripts" / "sanitized-test.sh"


def _write_cgroup_v2(
    root: Path,
    *,
    pids_limit: str,
    pids_current: int,
    cpu_max: str,
    memory_max: str,
    memory_current: int,
) -> None:
    root.mkdir()
    (root / "pids.max").write_text(f"{pids_limit}\n")
    (root / "pids.current").write_text(f"{pids_current}\n")
    (root / "cpu.max").write_text(f"{cpu_max}\n")
    (root / "memory.max").write_text(f"{memory_max}\n")
    (root / "memory.current").write_text(f"{memory_current}\n")


def _run_resource_preflight(
    cgroup_root: Path, *args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(RESOURCE_SCRIPT),
            "--cgroup-root",
            str(cgroup_root),
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.unit
def test_preflight_reports_limits_and_recommendation(tmp_path: Path) -> None:
    cgroup_root = tmp_path / "cgroup"
    _write_cgroup_v2(
        cgroup_root,
        pids_limit="2048",
        pids_current=64,
        cpu_max="200000 100000",
        memory_max=str(4 * 1024**3),
        memory_current=128 * 1024**2,
    )

    result = _run_resource_preflight(cgroup_root)

    assert result.returncode == 0
    assert "PID limit=2048 current=64 available=1984" in result.stderr
    assert "CPU limit=2.00 CPUs" in result.stderr
    assert "memory limit=4.00 GiB" in result.stderr
    assert "infrastructure limitation" in result.stderr
    assert "GOMAXPROCS=2 go test -p 1 ./..." in result.stderr
    assert "GOMAXPROCS=2 with --nodes=1" in result.stderr


@pytest.mark.unit
def test_strict_preflight_classifies_constrained_resources(tmp_path: Path) -> None:
    cgroup_root = tmp_path / "cgroup"
    _write_cgroup_v2(
        cgroup_root,
        pids_limit="2048",
        pids_current=64,
        cpu_max="200000 100000",
        memory_max="max",
        memory_current=128 * 1024**2,
    )

    result = _run_resource_preflight(cgroup_root, "--fail-on-constrained")

    assert result.returncode == 125
    assert "infrastructure limitation" in result.stderr


@pytest.mark.unit
def test_preflight_classifies_cpu_only_constraint(tmp_path: Path) -> None:
    cgroup_root = tmp_path / "cgroup"
    _write_cgroup_v2(
        cgroup_root,
        pids_limit="max",
        pids_current=64,
        cpu_max="100000 100000",
        memory_max="max",
        memory_current=128 * 1024**2,
    )

    result = _run_resource_preflight(cgroup_root, "--fail-on-constrained")

    assert result.returncode == 125
    assert "CPU limit=1.00 CPUs" in result.stderr
    assert "(CPU)" in result.stderr
    assert "GOMAXPROCS=1 go test -p 1 ./..." in result.stderr


@pytest.mark.unit
def test_preflight_classifies_memory_only_constraint(tmp_path: Path) -> None:
    cgroup_root = tmp_path / "cgroup"
    _write_cgroup_v2(
        cgroup_root,
        pids_limit="max",
        pids_current=64,
        cpu_max="200000 100000",
        memory_max=str(1024**3),
        memory_current=128 * 1024**2,
    )

    result = _run_resource_preflight(cgroup_root, "--fail-on-constrained")

    assert result.returncode == 125
    assert "memory limit=1.00 GiB" in result.stderr
    assert "(memory)" in result.stderr


@pytest.mark.unit
def test_preflight_blocks_when_a_limit_is_unreadable(tmp_path: Path) -> None:
    cgroup_root = tmp_path / "cgroup"
    _write_cgroup_v2(
        cgroup_root,
        pids_limit="not-a-limit",
        pids_current=64,
        cpu_max="max 100000",
        memory_max="max",
        memory_current=128 * 1024**2,
    )

    result = _run_resource_preflight(cgroup_root, "--fail-on-constrained")

    assert result.returncode == 125
    assert "PID limit=unknown" in result.stderr
    assert "unavailable limits: PID" in result.stderr


@pytest.mark.unit
@pytest.mark.parametrize(
    ("cpu_max", "memory_max", "unavailable"),
    [
        ("not-a-quota", "max", "CPU"),
        ("max 100000", "not-a-limit", "memory"),
    ],
)
def test_preflight_blocks_when_cpu_or_memory_is_malformed(
    tmp_path: Path, cpu_max: str, memory_max: str, unavailable: str
) -> None:
    cgroup_root = tmp_path / "cgroup"
    _write_cgroup_v2(
        cgroup_root,
        pids_limit="max",
        pids_current=64,
        cpu_max=cpu_max,
        memory_max=memory_max,
        memory_current=128 * 1024**2,
    )

    result = _run_resource_preflight(cgroup_root, "--fail-on-constrained")

    assert result.returncode == 125
    assert f"unavailable limits: {unavailable}" in result.stderr


@pytest.mark.unit
def test_preflight_blocks_when_finite_usage_is_unavailable(tmp_path: Path) -> None:
    cgroup_root = tmp_path / "cgroup"
    _write_cgroup_v2(
        cgroup_root,
        pids_limit="100000",
        pids_current=64,
        cpu_max="max 100000",
        memory_max=str(8 * 1024**3),
        memory_current=128 * 1024**2,
    )
    (cgroup_root / "pids.current").unlink()
    (cgroup_root / "memory.current").unlink()

    result = _run_resource_preflight(cgroup_root, "--fail-on-constrained")

    assert result.returncode == 125
    assert "PID usage unavailable" in result.stderr
    assert "memory usage unavailable" in result.stderr


@pytest.mark.unit
def test_preflight_blocks_when_cpuset_is_malformed(tmp_path: Path) -> None:
    cgroup_root = tmp_path / "cgroup"
    _write_cgroup_v2(
        cgroup_root,
        pids_limit="max",
        pids_current=64,
        cpu_max="max 100000",
        memory_max="max",
        memory_current=128 * 1024**2,
    )
    (cgroup_root / "cpuset.cpus.effective").write_text("0-1,garbage\n")

    result = _run_resource_preflight(cgroup_root, "--fail-on-constrained")

    assert result.returncode == 125
    assert "CPU limit=unknown" in result.stderr
    assert "unavailable limits: CPU" in result.stderr


@pytest.mark.unit
def test_preflight_blocks_empty_cpu_limit_even_with_cpuset(tmp_path: Path) -> None:
    cgroup_root = tmp_path / "cgroup"
    _write_cgroup_v2(
        cgroup_root,
        pids_limit="max",
        pids_current=64,
        cpu_max="",
        memory_max="max",
        memory_current=128 * 1024**2,
    )
    (cgroup_root / "cpuset.cpus.effective").write_text("0-3\n")

    result = _run_resource_preflight(cgroup_root, "--fail-on-constrained")

    assert result.returncode == 125
    assert "CPU limit=unknown" in result.stderr
    assert "unavailable limits: CPU" in result.stderr


@pytest.mark.unit
def test_preflight_blocks_when_cgroup_root_is_missing(tmp_path: Path) -> None:
    result = _run_resource_preflight(
        tmp_path / "missing-cgroup", "--fail-on-constrained"
    )

    assert result.returncode == 125
    assert "cgroup root unavailable" in result.stderr
    assert "PID limit=unknown" in result.stderr
    assert "memory limit=unknown" in result.stderr
    assert "GOMAXPROCS=1 go test -p 1 ./..." in result.stderr


@pytest.mark.unit
def test_preflight_supports_cgroup_v1_and_unlimited_values(tmp_path: Path) -> None:
    cgroup_root = tmp_path / "cgroup"
    (cgroup_root / "pids").mkdir(parents=True)
    (cgroup_root / "cpu").mkdir()
    (cgroup_root / "memory").mkdir()
    (cgroup_root / "pids" / "pids.max").write_text("max\n")
    (cgroup_root / "pids" / "pids.current").write_text("12\n")
    (cgroup_root / "cpu" / "cpu.cfs_quota_us").write_text("-1\n")
    (cgroup_root / "cpu" / "cpu.cfs_period_us").write_text("100000\n")
    (cgroup_root / "memory" / "memory.limit_in_bytes").write_text(
        f"{(1 << 63) - 4096}\n"
    )
    (cgroup_root / "memory" / "memory.usage_in_bytes").write_text("64\n")

    result = _run_resource_preflight(cgroup_root)

    assert result.returncode == 0
    assert "PID limit=unlimited current=12 available=unlimited" in result.stderr
    assert "memory limit=unlimited" in result.stderr
    assert "resource budget supports standard parallel test execution" in result.stderr


@pytest.mark.unit
def test_preflight_supports_v1_controller_mount_and_cpuset_limit(
    tmp_path: Path,
) -> None:
    cgroup_root = tmp_path / "cgroup"
    (cgroup_root / "pids").mkdir(parents=True)
    (cgroup_root / "cpu,cpuacct").mkdir()
    (cgroup_root / "cpuset").mkdir()
    (cgroup_root / "memory").mkdir()
    (cgroup_root / "pids" / "pids.max").write_text("max\n")
    (cgroup_root / "pids" / "pids.current").write_text("12\n")
    (cgroup_root / "cpu,cpuacct" / "cpu.cfs_quota_us").write_text("200000\n")
    (cgroup_root / "cpu,cpuacct" / "cpu.cfs_period_us").write_text("100000\n")
    (cgroup_root / "cpuset" / "cpuset.cpus").write_text("0-1\n")
    (cgroup_root / "memory" / "memory.limit_in_bytes").write_text(f"{4 * 1024**3}\n")
    (cgroup_root / "memory" / "memory.usage_in_bytes").write_text("64\n")

    result = _run_resource_preflight(cgroup_root)

    assert result.returncode == 0
    assert "CPU limit=2.00 CPUs" in result.stderr
    assert "source=cgroup quota+cpuset" in result.stderr
    assert "memory limit=4.00 GiB" in result.stderr


@pytest.mark.unit
def test_wrapper_blocks_command_before_constrained_parallel_run(tmp_path: Path) -> None:
    cgroup_root = tmp_path / "cgroup"
    _write_cgroup_v2(
        cgroup_root,
        pids_limit="2048",
        pids_current=64,
        cpu_max="200000 100000",
        memory_max=str(4 * 1024**3),
        memory_current=128 * 1024**2,
    )
    marker = tmp_path / "command-ran"
    result = subprocess.run(
        [
            str(WRAPPER_SCRIPT),
            "--resource-preflight",
            "--resource-cgroup-root",
            str(cgroup_root),
            "--",
            sys.executable,
            "-c",
            f"Path({str(marker)!r}).touch()",
        ],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 125
    assert "infrastructure limitation" in result.stderr
    assert not marker.exists()


@pytest.mark.unit
def test_wrapper_runs_healthy_command_without_credentials(tmp_path: Path) -> None:
    cgroup_root = tmp_path / "cgroup"
    _write_cgroup_v2(
        cgroup_root,
        pids_limit="max",
        pids_current=64,
        cpu_max="max 100000",
        memory_max="max",
        memory_current=128 * 1024**2,
    )
    env = os.environ.copy()
    env.update(
        {
            "GH_TOKEN": "host-secret-token",  # pragma: allowlist secret
            "UNRELATED_HOST_VALUE": "must-not-cross-boundary",
        }
    )
    result = subprocess.run(
        [
            str(WRAPPER_SCRIPT),
            "--resource-preflight",
            "--resource-cgroup-root",
            str(cgroup_root),
            "--",
            sys.executable,
            "-c",
            "import json, os; print(json.dumps(dict(os.environ)))",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    child_env = json.loads(result.stdout)
    assert "GH_TOKEN" not in child_env
    assert "UNRELATED_HOST_VALUE" not in child_env
    assert "resource budget supports standard parallel test execution" in result.stderr
