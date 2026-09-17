"""Meta test for the Podman cleanup isolation contract (issue #178).

With the `isolated_env` fixture active, cleanup failures must never be
silent: a raw podman call surfaces a non-zero exit with visible stderr in
every environment (in a plain shell it hits the fake `CONTAINERS_*` config
paths `host_credentials` installs; under `sanitized-test.sh` its Podman
shim reaches the runtime and reports the real error), and
`run_podman_isolated` is the loud replacement: it works against the
isolated runtime and raises `AssertionError` on failures that matter.
"""

import os
import subprocess
import uuid

import pytest

from tests.conftest import run_podman_isolated


@pytest.mark.integration
def test_podman_cleanup_failures_are_never_silent(
    isolated_env: dict[str, str],
    podman_probe_result,
):
    if not podman_probe_result.available:
        pytest.skip("Podman is not available in the environment")

    # The isolation state the silent-leak bug lived in is active: the test
    # process env carries fake CONTAINERS_* config paths.
    assert os.environ.get("CONTAINERS_CONF", "").startswith("host-")

    # The raw cleanup pattern is never silent: removing a missing volume
    # exits non-zero with visible output — a config error in a plain shell,
    # a genuine "no such volume" under the sanitized runner's Podman shim.
    # check=False + capture_output=True hid exactly this state while tests
    # passed and resources leaked.
    missing = f"devbox-absent-{uuid.uuid4().hex[:8]}"
    raw = subprocess.run(
        ["podman", "volume", "rm", missing],
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert raw.returncode != 0
    assert (raw.stdout + raw.stderr).decode(errors="replace").strip()

    # The helper reaches the runtime where the tests' containers live...
    names = run_podman_isolated(isolated_env, ["ps", "-a", "--format", "{{.Names}}"])
    assert names.returncode == 0

    # ...turns the same cleanup failure into a loud AssertionError, even
    # though `rm -f` of a missing container is exit-0 on this Podman...
    with pytest.raises(AssertionError, match=missing):
        run_podman_isolated(isolated_env, ["volume", "rm", missing])

    # ...while an already-clean resource is an allowed cleanup outcome.
    result = run_podman_isolated(
        isolated_env, ["volume", "rm", missing], allow_absent=True
    )
    assert result.returncode != 0
