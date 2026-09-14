"""Fast tests for per-run unique launcher workspace naming.

These tests exercise ``tests.conftest.unique_workspace_dir`` and
``tests.conftest.devbox_container_name`` (issue #152): repeated runs must
never share a container name, and generated names must remain valid Podman
container names.
"""

import pytest

from tests.conftest import devbox_container_name, unique_workspace_dir

# Podman container names must match [a-zA-Z0-9][a-zA-Z0-9_.-]*
PODMAN_NAME_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
)


@pytest.mark.unit
def test_unique_workspace_dirs_never_share_container_names(tmp_path):
    names = {
        devbox_container_name(unique_workspace_dir(tmp_path, "test_workspace"))
        for _ in range(50)
    }

    assert len(names) == 50


@pytest.mark.unit
def test_unique_workspace_dirs_are_created_on_disk(tmp_path):
    test_dir = unique_workspace_dir(tmp_path, "test_workspace")

    assert test_dir.is_dir()
    assert test_dir.name.startswith("test_workspace-")


@pytest.mark.unit
def test_container_name_follows_launcher_naming_scheme(tmp_path):
    test_dir = unique_workspace_dir(tmp_path, "check_nested")

    assert devbox_container_name(test_dir) == f"devbox-{test_dir.name}"


@pytest.mark.unit
def test_generated_container_names_are_valid_podman_names(tmp_path):
    for label in ("test_workspace", "nested_build_ws", "docker_api_probe"):
        name = devbox_container_name(unique_workspace_dir(tmp_path, label))

        assert name[0].isalnum()
        assert set(name) <= PODMAN_NAME_CHARS


@pytest.mark.unit
def test_generated_container_names_never_equal_legacy_fixed_names(tmp_path):
    # Regression for issue #152: the pre-fix fixed names (e.g. the stale
    # `devbox-test_workspace` that poisoned re-runs) must never be produced
    # again, regardless of label.
    legacy_names = {
        "devbox-test_workspace",
        "devbox-check_nested",
        "devbox-nested_run_ws",
        "devbox-nested_build_ws",
        "devbox-docker_api_probe",
        "devbox-docker_api_port",
        "devbox-docker_api_network",
    }
    for label in ("test_workspace", "nested_run_ws", "docker_api_network"):
        names = {
            devbox_container_name(unique_workspace_dir(tmp_path, label))
            for _ in range(20)
        }

        assert names.isdisjoint(legacy_names)
