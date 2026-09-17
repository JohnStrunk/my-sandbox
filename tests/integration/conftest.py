"""Integration-tier conftest: session-wide Podman resource leak guard.

Silent cleanup failures used to let integration tests pass while leaking
`devbox-*` containers and volumes (issue #178). Cleanups now run through
`run_podman_isolated()` and fail loudly; this fixture is the net that also
catches leaks from any future path that bypasses the helper: it snapshots
`devbox-*` containers and volumes (via `podman ps -a` / `podman volume ls`)
before and after the integration session and fails if the session left new
ones behind.

Caveat: the delta is by name, so a concurrent session sharing this Podman
storage and creating `devbox-*` resources mid-run can be reported as a
leak; CI runs the suite with exclusive access to the runner's storage.
"""

import subprocess
from collections.abc import Iterator

import pytest

from tests.conftest import PodmanProbeResult

DEVBOX_PREFIX = "devbox"

# Persistent shared cache volumes the `devbox` launcher mounts and
# auto-creates (see `devbox`: --volume devbox-kb/devbox-go-cache/...). They
# are never removed by design, so they must not count as session leaks even
# when the session's storage was fresh and the launcher created them.
# Keep in sync with the launcher's --volume list.
SHARED_PERSISTENT_VOLUMES = frozenset(
    {
        "devbox-kb",
        "devbox-go-cache",
        "devbox-uv-cache",
        "devbox-precommit-cache",
        "devbox-semble-cache",
    }
)


def _devbox_resources() -> tuple[set[str], set[str]]:
    """Names of existing devbox-* containers and volumes on this runtime."""
    containers = subprocess.run(
        ["podman", "ps", "-a", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout.splitlines()
    volumes = subprocess.run(
        ["podman", "volume", "ls", "-q"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout.splitlines()
    return (
        {name for name in containers if name.startswith(DEVBOX_PREFIX)},
        {name for name in volumes if name.startswith(DEVBOX_PREFIX)},
    )


@pytest.fixture(scope="session", autouse=True)
def _no_devbox_resource_leaks(
    podman_probe_result: PodmanProbeResult,
) -> Iterator[None]:
    """Fail the session if it leaked devbox-* containers or volumes."""
    if not podman_probe_result.available:
        yield
        return
    before_containers, before_volumes = _devbox_resources()
    yield
    after_containers, after_volumes = _devbox_resources()
    leaked_containers = sorted(after_containers - before_containers)
    leaked_volumes = sorted(after_volumes - before_volumes - SHARED_PERSISTENT_VOLUMES)
    assert not leaked_containers, (
        "integration session left devbox-* containers behind "
        f"(podman ps -a): {leaked_containers}"
    )
    assert not leaked_volumes, (
        f"integration session left devbox-* volumes behind "
        f"(podman volume ls): {leaked_volumes}"
    )
