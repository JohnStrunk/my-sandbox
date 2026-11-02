"""Behavioral tests for the shared Fedora Ruby package preflight."""

import re
import shlex
import subprocess
from pathlib import Path

import pytest

_RUBY_VERSION = "4.0.7"


def _write_fake_dnf(
    bin_dir: Path,
    *,
    query_stdout: str = "",
    query_stderr: str = "",
    query_exit: int = 0,
) -> Path:
    """Create a fake dnf that logs its arguments and scripts the repoquery result."""
    log_path = bin_dir / "dnf-invocations.log"
    fake_dnf = bin_dir / "dnf"
    fake_dnf.write_text(
        "#!/bin/bash\n"
        f"printf '%s\\n' \"$*\" >> {shlex.quote(str(log_path))}\n"
        'if [[ "${1:-}" == repoquery ]]; then\n'
        f"  printf '%s\\n' {shlex.quote(query_stdout)}\n"
        f"  printf '%s' {shlex.quote(query_stderr)} >&2\n"
        f"  exit {query_exit}\n"
        "fi\n"
        "exit 0\n"
    )
    fake_dnf.chmod(0o755)
    return log_path


def _extract_function(repo_root: Path, name: str) -> str:
    """Return only the top-level definition of `name` from the helper."""
    package_script = (repo_root / "lima" / "fedora-packages.sh").read_text()
    definition = re.search(
        rf"(?m)^[ \t]*(?:function[ \t]+)?{re.escape(name)}[ \t]*\(\)[ \t]*\{{",
        package_script,
    )
    assert definition, f"{name} must be defined in lima/fedora-packages.sh"
    remainder = package_script[definition.start() :]
    closing = re.search(r"(?m)^\}[ \t]*(?:#.*)?$", remainder)
    assert closing, f"{name} must have a top-level closing brace"
    return remainder[: closing.end()]


def _run_preflight(
    repo_root: Path, bin_dir: Path, version: str
) -> subprocess.CompletedProcess[str]:
    function = _extract_function(repo_root, "require_ruby_package_available")
    script = f'set -euo pipefail\n{function}\nrequire_ruby_package_available "$1"\n'
    return subprocess.run(
        ["/bin/bash", "-c", script, "ruby-preflight-test", version],
        capture_output=True,
        check=False,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "LC_ALL": "C"},
        text=True,
        timeout=30,
    )


def _invocations(log_path: Path) -> list[str]:
    return log_path.read_text().splitlines()


def _assert_no_install(invocations: list[str]) -> None:
    assert not any(line.startswith("install") for line in invocations)


def _assert_actionable_failure(
    result: subprocess.CompletedProcess[str], version: str
) -> None:
    assert result.returncode != 0
    stderr = result.stderr.strip()
    assert stderr, "the preflight must explain the failure on stderr"
    assert version in stderr or "ruby" in stderr.lower()


@pytest.mark.unit
def test_require_ruby_package_available_accepts_exact_pin(
    repo_root: Path, tmp_path: Path
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = _write_fake_dnf(bin_dir, query_stdout=f"ruby-{_RUBY_VERSION}")

    result = _run_preflight(repo_root, bin_dir, _RUBY_VERSION)

    assert result.returncode == 0, result.stderr
    invocations = _invocations(log_path)
    assert any(
        line.startswith("repoquery")
        and "--available" in line
        and f"ruby-{_RUBY_VERSION}" in line
        for line in invocations
    )
    _assert_no_install(invocations)


@pytest.mark.unit
@pytest.mark.parametrize(
    "query_stdout",
    [
        pytest.param("", id="dnf5-no-match-empty-output"),
        pytest.param("ruby-4.0.70", id="exact-line-collision"),
    ],
)
def test_require_ruby_package_available_rejects_missing_pin_before_install(
    repo_root: Path, tmp_path: Path, query_stdout: str
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = _write_fake_dnf(bin_dir, query_stdout=query_stdout)

    result = _run_preflight(repo_root, bin_dir, _RUBY_VERSION)

    _assert_actionable_failure(result, _RUBY_VERSION)
    assert re.search(
        r"not available|unavailable|no available|does not match|missing|"
        r"fedora-repos-archive|manifest",
        result.stderr,
        re.IGNORECASE,
    ), result.stderr
    invocations = _invocations(log_path)
    assert any(line.startswith("repoquery") for line in invocations)
    _assert_no_install(invocations)


@pytest.mark.unit
def test_require_ruby_package_available_rejects_non_xyz_pin_before_dnf(
    repo_root: Path, tmp_path: Path
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = _write_fake_dnf(bin_dir, query_stdout=f"ruby-{_RUBY_VERSION}")
    pinned_release = f"{_RUBY_VERSION}-39.fc44"

    result = _run_preflight(repo_root, bin_dir, pinned_release)

    _assert_actionable_failure(result, pinned_release)
    assert not log_path.exists(), "the pin must be validated before invoking dnf"


@pytest.mark.unit
def test_require_ruby_package_available_reports_repoquery_failure(
    repo_root: Path, tmp_path: Path
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = _write_fake_dnf(
        bin_dir,
        query_stderr="fake dnf: repository metadata is unavailable\n",
        query_exit=1,
    )

    result = _run_preflight(repo_root, bin_dir, _RUBY_VERSION)

    _assert_actionable_failure(result, _RUBY_VERSION)
    assert re.search(
        r"query|repositor|available|ruby",
        result.stderr,
        re.IGNORECASE,
    ), result.stderr
    invocations = _invocations(log_path)
    assert any(line.startswith("repoquery") for line in invocations)
    _assert_no_install(invocations)
