import os
import subprocess
import uuid
from pathlib import Path

import pytest

from tests.conftest import remove_devbox, run_bash_script, unique_workspace_dir


@pytest.mark.integration
def test_kind_create_use_delete(
    devbox_path: Path,
    tmp_path: Path,
    isolated_env: dict[str, str],
):
    test_dir = unique_workspace_dir(tmp_path, "kind_cluster")
    cluster_name = f"devbox-{uuid.uuid4().hex[:8]}"
    try:
        preflight = run_bash_script(
            devbox_path,
            ["--kind", "devbox-kind", "preflight"],
            env=isolated_env,
            cwd=test_dir,
            timeout=180,
        )
    except subprocess.TimeoutExpired as exc:
        remove_devbox(devbox_path, test_dir, isolated_env)
        raise AssertionError(f"kind runtime preflight timed out: {exc}") from exc
    if preflight.returncode != 0:
        remove_devbox(devbox_path, test_dir, isolated_env)
    preflight_output = preflight.stdout + preflight.stderr
    capability_failure_marker = "kind runtime preflight failed; no cluster was created."
    if preflight.returncode == 125 and capability_failure_marker in preflight_output:
        message = (
            "kind runtime capability preflight unavailable in this host/container "
            f"environment:\n{preflight.stdout}\n{preflight.stderr}"
        )
        if os.environ.get("DEVBOX_KIND_REQUIRE") == "1":
            pytest.fail(message)
        pytest.skip(message)
    if preflight.returncode != 0:
        pytest.fail(
            f"kind runtime preflight failed unexpectedly:\n"
            f"{preflight.stdout}\n{preflight.stderr}"
        )

    cluster_attempted = False
    try:
        cluster_attempted = True
        create = run_bash_script(
            devbox_path,
            [
                "--kind",
                "devbox-kind",
                "create",
                "cluster",
                "--name",
                cluster_name,
                "--wait",
                "5m",
            ],
            env=isolated_env,
            cwd=test_dir,
            timeout=600,
        )
        assert create.returncode == 0, (
            f"kind cluster creation failed:\n{create.stdout}\n{create.stderr}"
        )
        nodes = run_bash_script(
            devbox_path,
            [
                "--kind",
                "kubectl",
                "get",
                "nodes",
                "--context",
                f"kind-{cluster_name}",
                "--no-headers",
            ],
            env=isolated_env,
            cwd=test_dir,
            timeout=60,
        )
        assert nodes.returncode == 0, nodes.stderr
        assert " Ready " in f" {nodes.stdout} "

        api = run_bash_script(
            devbox_path,
            [
                "--kind",
                "kubectl",
                "get",
                "--raw=/version",
                "--context",
                f"kind-{cluster_name}",
            ],
            env=isolated_env,
            cwd=test_dir,
            timeout=60,
        )
        assert api.returncode == 0, api.stderr
        assert '"gitVersion"' in api.stdout
    finally:
        try:
            if cluster_attempted:
                delete = run_bash_script(
                    devbox_path,
                    [
                        "--kind",
                        "devbox-kind",
                        "delete",
                        "cluster",
                        "--name",
                        cluster_name,
                    ],
                    env=isolated_env,
                    cwd=test_dir,
                    timeout=180,
                )
                assert delete.returncode == 0, (
                    f"kind cluster cleanup failed:\n{delete.stdout}\n{delete.stderr}"
                )

                leftovers = run_bash_script(
                    devbox_path,
                    [
                        "--kind",
                        "podman",
                        "ps",
                        "-a",
                        "--filter",
                        f"label=io.x-k8s.kind.cluster={cluster_name}",
                        "--format",
                        "{{.ID}}",
                    ],
                    env=isolated_env,
                    cwd=test_dir,
                    timeout=60,
                )
                assert leftovers.returncode == 0, leftovers.stderr
                assert "==> Entering container" in leftovers.stdout
                _, _, entered = leftovers.stdout.partition("==> Entering container")
                _, _, command_output = entered.partition("\n")
                command_output = command_output.strip()
                assert not command_output, leftovers.stdout
        finally:
            remove_devbox(devbox_path, test_dir, isolated_env)
