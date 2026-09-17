"""Meta test for the Podman cleanup isolation contract (issue #178).

With the `isolated_env` fixture active, a raw podman call that inherits
`os.environ` must fail (it hits the fake `CONTAINERS_*` config paths that
`host_credentials` installs) — the exact state that let `check=False`
cleanups leak containers and volumes silently. `run_podman_isolated` is the
loud replacement: it works against the isolated runtime and raises
`AssertionError` on failures that matter.
"""

import subprocess
import uuid

import pytest

from tests.conftest import run_podman_isolated


@pytest.mark.integration
def test_raw_podman_under_isolated_env_fails_not_silently(
    isolated_env: dict[str, str],
    podman_probe_result,
):
    if not podman_probe_result.available:
        pytest.skip("Podman is not available in the environment")

    # The silent-leak mode is detectable: with the fake CONTAINERS_* config
    # in os.environ, a raw call exits non-zero instead of quietly doing
    # (or failing to do) host-side work.
    raw = subprocess.run(
        ["podman", "ps"],
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert raw.returncode != 0, (
        "raw podman unexpectedly succeeded under isolated_env; the "
        "silent-leak failure mode this guard exists to catch changed"
    )

    # The helper reaches the isolated runtime where the tests' containers
    # actually live, and fails loudly when a cleanup that matters fails.
    names = run_podman_isolated(isolated_env, ["ps", "-a", "--format", "{{.Names}}"])
    assert names.returncode == 0

    # A cleanup failure must surface as an AssertionError. `volume rm` of a
    # missing volume is a guaranteed non-zero exit ("no such volume"), even
    # though `rm -f` of a missing container is exit-0 on this Podman.
    missing = f"devbox-absent-{uuid.uuid4().hex[:8]}"
    with pytest.raises(AssertionError, match=missing):
        run_podman_isolated(isolated_env, ["volume", "rm", missing])

    # ...while an already-clean resource is an allowed cleanup outcome.
    result = run_podman_isolated(
        isolated_env, ["volume", "rm", missing], allow_absent=True
    )
    assert result.returncode != 0
