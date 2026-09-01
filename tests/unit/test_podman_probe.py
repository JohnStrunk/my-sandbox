"""Deterministic tests for the Podman availability probe.

These tests exercise ``tests.conftest.probe_podman_availability`` directly,
mocking ``shutil.which`` and ``subprocess.run`` so they run quickly and
without requiring a real Podman installation.
"""

import subprocess

import pytest

from tests.conftest import (
    DEFAULT_PODMAN_PROBE_TIMEOUT,
    PODMAN_PROBE_TIMEOUT_ENV_VAR,
    podman_probe_timeout,
    probe_podman_availability,
)


@pytest.mark.unit
def test_probe_reports_unavailable_when_binary_missing(monkeypatch):
    monkeypatch.setattr("tests.conftest.shutil.which", lambda name: None)

    result = probe_podman_availability(timeout=5)

    assert result.available is False
    assert "not found on PATH" in result.reason


@pytest.mark.unit
def test_probe_reports_timeout_without_hanging(monkeypatch):
    monkeypatch.setattr("tests.conftest.shutil.which", lambda name: "/usr/bin/podman")

    def fake_run(cmd, capture_output, timeout, check):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    monkeypatch.setattr("tests.conftest.subprocess.run", fake_run)

    result = probe_podman_availability(timeout=0.01)

    assert result.available is False
    assert "did not complete within" in result.reason
    assert "0.01" in result.reason


@pytest.mark.unit
def test_probe_succeeds_after_slow_start(monkeypatch):
    # Simulates a rootless Podman runtime that takes longer than a naive
    # short timeout would allow, but still completes successfully within
    # the configured (larger) bound.
    monkeypatch.setattr("tests.conftest.shutil.which", lambda name: "/usr/bin/podman")

    def fake_run(cmd, capture_output, timeout, check):
        assert timeout >= 28  # comfortably above the previously-observed slow start
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr("tests.conftest.subprocess.run", fake_run)

    result = probe_podman_availability(timeout=60)

    assert result.available is True
    assert result.reason == "podman is available"


@pytest.mark.unit
def test_probe_reports_command_error(monkeypatch):
    monkeypatch.setattr("tests.conftest.shutil.which", lambda name: "/usr/bin/podman")

    def fake_run(cmd, capture_output, timeout, check):
        return subprocess.CompletedProcess(
            cmd, returncode=125, stdout=b"", stderr=b"cannot connect to podman socket"
        )

    monkeypatch.setattr("tests.conftest.subprocess.run", fake_run)

    result = probe_podman_availability(timeout=5)

    assert result.available is False
    assert "exited with status 125" in result.reason
    assert "cannot connect to podman socket" in result.reason


@pytest.mark.unit
def test_probe_reports_execution_failure(monkeypatch):
    monkeypatch.setattr("tests.conftest.shutil.which", lambda name: "/usr/bin/podman")

    def fake_run(cmd, capture_output, timeout, check):
        raise OSError("permission denied")

    monkeypatch.setattr("tests.conftest.subprocess.run", fake_run)

    result = probe_podman_availability(timeout=5)

    assert result.available is False
    assert "failed to execute 'podman info'" in result.reason
    assert "permission denied" in result.reason


@pytest.mark.unit
def test_probe_timeout_defaults_when_env_var_unset(monkeypatch):
    monkeypatch.delenv(PODMAN_PROBE_TIMEOUT_ENV_VAR, raising=False)

    assert podman_probe_timeout() == DEFAULT_PODMAN_PROBE_TIMEOUT


@pytest.mark.unit
def test_probe_timeout_honors_env_var_override(monkeypatch):
    monkeypatch.setenv(PODMAN_PROBE_TIMEOUT_ENV_VAR, "120")

    assert podman_probe_timeout() == 120.0


@pytest.mark.unit
@pytest.mark.parametrize("bad_value", ["not-a-number", "-5", "0"])
def test_probe_timeout_falls_back_on_invalid_env_var(monkeypatch, bad_value):
    monkeypatch.setenv(PODMAN_PROBE_TIMEOUT_ENV_VAR, bad_value)

    assert podman_probe_timeout() == DEFAULT_PODMAN_PROBE_TIMEOUT
