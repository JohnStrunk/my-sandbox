"""Meta tests for `run_podman_isolated` (issue #178).

A raw `subprocess.run(["podman", ...])` without `env=isolated_env` fails on
the fake `CONTAINERS_*` config paths that `isolated_env` installs and, with
`check=False` + `capture_output=True`, leaks resources silently. These tests
pin down that `run_podman_isolated` always passes the isolated env and turns
every real cleanup failure into a loud `AssertionError`. `subprocess.run` is
monkeypatched, so no Podman runtime is required.
"""

import subprocess
from collections.abc import Iterator

import pytest

from tests.conftest import run_podman_isolated

FAKE_ENV = {"PATH": "/isolated/bin", "HOME": "/isolated/home"}


class FakeRun(list):
    """Captured `subprocess.run` calls, queued as `CompletedProcess` in
    `.responses`; a call with no queued response succeeds."""

    responses: list[subprocess.CompletedProcess[str]]


@pytest.fixture
def podman_runs() -> Iterator[FakeRun]:
    calls = FakeRun()
    calls.responses = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if calls.responses:
            response = calls.responses.pop(0)
            return subprocess.CompletedProcess(
                cmd, response.returncode, response.stdout, response.stderr
            )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    original = subprocess.run
    subprocess.run = fake_run  # type: ignore[assignment]
    try:
        yield calls
    finally:
        subprocess.run = original  # type: ignore[assignment]


def completed(returncode: int, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["podman"], returncode, "", stderr)


@pytest.mark.unit
def test_helper_passes_isolated_env_verbatim(podman_runs):
    run_podman_isolated(FAKE_ENV, ["rm", "-f", "devbox-test"])
    cmd, kwargs = podman_runs[0]
    assert cmd == ["podman", "rm", "-f", "devbox-test"]
    assert kwargs["env"] is FAKE_ENV
    assert kwargs["check"] is False
    assert kwargs["capture_output"] is True
    assert kwargs["timeout"] > 0


@pytest.mark.unit
def test_failure_raises_loudly_with_detail(podman_runs):
    podman_runs.responses.append(
        completed(125, "Failed to obtain podman configuration")
    )
    with pytest.raises(AssertionError) as excinfo:
        run_podman_isolated(FAKE_ENV, ["rm", "-f", "devbox-test"])
    message = str(excinfo.value)
    assert "podman rm -f devbox-test" in message
    assert "exit" in message and "125" in message
    assert "Failed to obtain podman configuration" in message


@pytest.mark.unit
def test_absent_resource_is_clean_only_when_allowed(podman_runs):
    podman_runs.responses.append(completed(1, "Error: no such container devbox-test"))
    result = run_podman_isolated(
        FAKE_ENV, ["rm", "-f", "devbox-test"], allow_absent=True
    )
    assert result.returncode == 1
    assert len(podman_runs) == 1

    podman_runs.responses.append(completed(1, "no such volume devbox-venv-x"))
    with pytest.raises(AssertionError):
        run_podman_isolated(FAKE_ENV, ["volume", "rm", "devbox-venv-x"])


@pytest.mark.unit
def test_unexpected_error_never_absent_tolerated(podman_runs):
    podman_runs.responses.append(
        completed(125, "Failed to obtain podman configuration")
    )
    with pytest.raises(AssertionError):
        run_podman_isolated(
            FAKE_ENV, ["volume", "rm", "devbox-venv-x"], allow_absent=True
        )


@pytest.mark.unit
def test_retries_are_bounded(podman_runs):
    podman_runs.responses.extend(
        [completed(2, "volume is being used"), completed(2, "volume is being used")]
    )
    result = run_podman_isolated(
        FAKE_ENV,
        ["volume", "rm", "devbox-venv-x"],
        retries=2,
        retry_delay=0,
    )
    assert result.returncode == 0
    assert len(podman_runs) == 3


@pytest.mark.unit
def test_retry_exhaustion_raises(podman_runs):
    podman_runs.responses.extend([completed(2, "volume is being used")] * 3)
    with pytest.raises(AssertionError) as excinfo:
        run_podman_isolated(
            FAKE_ENV,
            ["volume", "rm", "devbox-venv-x"],
            retries=2,
            retry_delay=0,
        )
    assert len(podman_runs) == 3
    assert "after 3 attempt" in str(excinfo.value)
