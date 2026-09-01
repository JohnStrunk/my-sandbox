"""Verify the credential-isolated test runner environment (issue #81).

These tests validate the `_isolated_test_environment` autouse fixture in
`tests/conftest.py`: provider/integration credentials must not affect
isolated tests by default, tests can opt into specific credentials, and
`e2e_inference`-marked tests are the documented exception that see real host
credentials.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import CREDENTIAL_ENV_VARS


@pytest.mark.unit
def test_credential_env_vars_are_absent_by_default() -> None:
    for name in CREDENTIAL_ENV_VARS:
        assert os.getenv(name) is None, f"{name} leaked into an isolated test"


@pytest.mark.unit
def test_opting_into_a_credential_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-value")  # pragma: allowlist secret
    assert os.environ["GEMINI_API_KEY"] == "test-value"  # pragma: allowlist secret


@pytest.mark.unit
def test_isolated_env_scrubs_credentials_and_home_state(
    isolated_env: dict[str, str], isolated_home: Path
) -> None:
    for name in CREDENTIAL_ENV_VARS:
        assert name not in isolated_env
    assert isolated_env["HOME"] == str(isolated_home)
    assert not any(isolated_home.iterdir())


@pytest.mark.unit
def test_isolation_holds_even_when_host_process_has_credentials(
    repo_root: Path,
) -> None:
    """Isolation must not merely rely on the outer runner's own environment
    already being clean: simulate a host process that has every known
    credential set and confirm a nested test session still sees none of
    them.
    """
    env = os.environ.copy()
    for name in CREDENTIAL_ENV_VARS:
        env[name] = f"host-{name.lower()}"

    res = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "tests/unit/test_isolated_test_environment.py::"
            "test_credential_env_vars_are_absent_by_default",
        ],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert res.returncode == 0, res.stdout + res.stderr
