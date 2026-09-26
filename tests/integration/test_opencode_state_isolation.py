from pathlib import Path

import pytest

from tests.conftest import (
    run_bash_script,
    run_podman_isolated,
    unique_workspace_dir,
)

HOST_MODEL_PICKS = '{"recent":["anthropic/claude"]}'
CONTAINER_MODEL_PICKS = '{"recent":["openai/gpt-5.6"]}'
HOST_SERVICE_REGISTRATION = '{"id":"host-service-registration"}'


def _plant_host_opencode_state(home: Path) -> Path:
    """Populate a host OpenCode state directory for seeding assertions.

    The shareable files (model picks, pinned sessions, prompt history, TUI
    view state) must be seeded into a new per-container state directory;
    the volatile single-owner files (service registrations, atomic-write
    temp files, lock directories) must never be (issue #256).
    """
    host_state = home / ".local" / "state" / "opencode"
    (host_state / "latest" / "tui").mkdir(parents=True)
    (host_state / "latest" / "locks").mkdir()
    (host_state / "locks").mkdir()
    (host_state / "model.json").write_text(HOST_MODEL_PICKS)
    (host_state / "session.json").write_text('{"pinned":["ses_host"]}')
    (host_state / "prompt-history.jsonl").write_text('{"text":"host prompt"}\n')
    (host_state / "kv.json").write_text('{"theme":"dark"}')
    (host_state / "latest" / "tui" / "tabs.json").write_text('{"tabs":[]}')
    (host_state / "service.json").write_text(HOST_SERVICE_REGISTRATION)
    (host_state / "service-beta.json").write_text('{"id":"host-beta-service"}')
    (host_state / "atomic-write.tmp").write_text("")
    (host_state / "latest" / "tui" / "plugin.view.json.tmp").write_text("")
    (host_state / "locks" / "cli.json.lock").write_text("host-lock")
    (host_state / "latest" / "locks" / "tui.lock").write_text("host-lock")
    return host_state


@pytest.mark.integration
def test_opencode_state_isolated_and_seeded_per_container(
    devbox_path: Path,
    devbox_image: str,
    tmp_path: Path,
    isolated_env: dict[str, str],
):
    # Issue #256: OpenCode v2's state directory is single-owner (its
    # background service shuts down whenever service.json stops describing
    # itself), so sharing it across containers makes concurrently running
    # services mutually restart. The launcher must give each container its
    # own state directory seeded from the host, never write the host's
    # state directory from the container, and keep the per-container state
    # across --recreate.
    home = Path(isolated_env["HOME"])
    host_state = _plant_host_opencode_state(home)
    test_dir = unique_workspace_dir(tmp_path, "opencode_state")
    container_name = f"devbox-{test_dir.name}"
    per_container = home / ".local" / "state" / "devbox" / test_dir.name
    marker = "written-by-container"

    try:
        # 1. Creation seeds the per-container state directory and the
        #    container sees it at OpenCode's default state path. The exact
        #    model.json equality below relies on the creation-time
        #    `opencode models` warm start not rewriting model.json (it
        #    only lists the catalog; model.json changes on selection), so
        #    this also proves the seeded state survived the warm start.
        res = run_bash_script(
            devbox_path,
            [
                "bash",
                "-c",
                "set -eu\n"
                'test "$(cat /sandbox/.local/state/opencode/model.json)" = '
                f"'{HOST_MODEL_PICKS}'\n"
                "test -f /sandbox/.local/state/opencode/session.json\n"
                "test -f /sandbox/.local/state/opencode/prompt-history.jsonl\n"
                "test -f /sandbox/.local/state/opencode/latest/tui/tabs.json\n"
                "test ! -e /sandbox/.local/state/opencode/atomic-write.tmp\n"
                "test ! -e /sandbox/.local/state/opencode/service-beta.json\n"
                "echo seeded-state-ok",
            ],
            env=isolated_env,
            cwd=test_dir,
            # A sanitized runner rebuilds the image in its isolated Podman
            # store, so the initial image build needs a larger time budget.
            timeout=600,
        )
        assert res.returncode == 0, f"{res.stdout}\n{res.stderr}"
        assert "seeded-state-ok" in res.stdout
        assert "Seeding per-container OpenCode state" in res.stdout

        # 2. Host side: the seeded copy lives in the per-container host
        #    directory, minus the volatile single-owner files.
        assert (per_container / "model.json").read_text() == HOST_MODEL_PICKS
        assert (per_container / "latest" / "tui" / "tabs.json").is_file()
        assert not (per_container / "atomic-write.tmp").exists()
        assert not (per_container / "latest" / "tui" / "plugin.view.json.tmp").exists()
        assert not (per_container / "service-beta.json").exists()
        # Any service registration in the per-container directory is the
        # container's own (written by its `opencode models` warm start),
        # never the host's.
        container_service = per_container / "service.json"
        if container_service.exists():
            assert container_service.read_text() != HOST_SERVICE_REGISTRATION

        # 3. Container writes land in the per-container host directory,
        #    never in the host's shared state directory.
        res = run_bash_script(
            devbox_path,
            ["bash", "-c", f"touch /sandbox/.local/state/opencode/{marker}"],
            env=isolated_env,
            cwd=test_dir,
            timeout=120,
        )
        assert res.returncode == 0, f"{res.stdout}\n{res.stderr}"
        assert (per_container / marker).is_file()
        assert not (host_state / marker).exists()

        # 4. The per-container state persists across --recreate and is not
        #    re-seeded from the host.
        res = run_bash_script(
            devbox_path,
            [
                "bash",
                "-c",
                "echo '"
                + CONTAINER_MODEL_PICKS
                + "' > /sandbox/.local/state/opencode/model.json",
            ],
            env=isolated_env,
            cwd=test_dir,
            timeout=120,
        )
        assert res.returncode == 0, f"{res.stdout}\n{res.stderr}"
        res = run_bash_script(
            devbox_path,
            [
                "--recreate",
                "bash",
                "-c",
                "set -eu\n"
                'test "$(cat /sandbox/.local/state/opencode/model.json)" = '
                f"'{CONTAINER_MODEL_PICKS}'\n"
                f"test -f /sandbox/.local/state/opencode/{marker}\n"
                "echo persisted-state-ok",
            ],
            env=isolated_env,
            cwd=test_dir,
            timeout=600,
        )
        assert res.returncode == 0, f"{res.stdout}\n{res.stderr}"
        assert "persisted-state-ok" in res.stdout
        assert "Seeding per-container OpenCode state" not in res.stdout

        # 5. The per-container state also persists across --remove: the
        #    launcher never deletes it, and a later creation reuses it
        #    without re-seeding.
        res = run_bash_script(
            devbox_path,
            ["--remove"],
            env=isolated_env,
            cwd=test_dir,
            timeout=120,
        )
        assert res.returncode == 0, f"{res.stdout}\n{res.stderr}"
        assert (per_container / marker).is_file()
        res = run_bash_script(
            devbox_path,
            [
                "bash",
                "-c",
                "set -eu\n"
                f"test -f /sandbox/.local/state/opencode/{marker}\n"
                'test "$(cat /sandbox/.local/state/opencode/model.json)" = '
                f"'{CONTAINER_MODEL_PICKS}'\n"
                "echo remove-persistence-ok",
            ],
            env=isolated_env,
            cwd=test_dir,
            timeout=600,
        )
        assert res.returncode == 0, f"{res.stdout}\n{res.stderr}"
        assert "remove-persistence-ok" in res.stdout
        assert "Seeding per-container OpenCode state" not in res.stdout

        # 6. The host's state directory was never written by any container
        #    process: every planted file is exactly as it was.
        for name in (
            "model.json",
            "session.json",
            "prompt-history.jsonl",
            "kv.json",
            "service.json",
            "service-beta.json",
            "atomic-write.tmp",
            "latest/tui/tabs.json",
            "latest/tui/plugin.view.json.tmp",
            "locks/cli.json.lock",
            "latest/locks/tui.lock",
        ):
            assert (host_state / name).is_file(), name
        assert (host_state / "model.json").read_text() == HOST_MODEL_PICKS
        assert (host_state / "service.json").read_text() == HOST_SERVICE_REGISTRATION
    finally:
        run_podman_isolated(
            isolated_env, ["rm", "-f", container_name], allow_absent=True
        )
