import os
import subprocess
import sys
from pathlib import Path

import pytest

SEED_SCRIPT = Path(__file__).resolve().parents[2] / "lima" / "seed-opencode-state.py"
MIGRATION_SCRIPT = SEED_SCRIPT.parent / "migrate-opencode-state.sh"


def run_seed(
    source: Path, destination: Path, *, replace_existing: bool = False
) -> subprocess.CompletedProcess[str]:
    mode = ["--replace-existing"] if replace_existing else []
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(SEED_SCRIPT),
            *mode,
            str(source),
            str(destination),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def install_fake_limactl(fake_bin: Path) -> None:
    fake_bin.mkdir(parents=True, exist_ok=True)
    fake_limactl = fake_bin / "limactl"
    fake_limactl.write_text(
        """#!/usr/bin/env python3
import os
import shutil
import sys
from pathlib import Path

args = sys.argv[1:]
mode = os.environ.get("DEVBOX_MIGRATION_MODE", "success")
source = Path(os.environ["DEVBOX_MIGRATION_SOURCE"])
instance = os.environ["DEVBOX_MIGRATION_INSTANCE"]
if args[0] == "list":
    status = "Stopped" if mode == "stopped" else "Running"
    print(instance + " " + status)
elif args[0] == "shell":
    if mode == "no-state":
        raise SystemExit(1)
    if mode == "service-running" and "python3" in args:
        raise SystemExit(1)
    raise SystemExit(0)
elif args[0] == "copy":
    assert args[1:3] == ["--backend=scp", "--recursive"]
    if mode == "copy-failure":
        print("fake copy failure", file=sys.stderr)
        raise SystemExit(1)
    destination = Path(args[-1]) / "opencode"
    shutil.copytree(source, destination)
else:
    raise SystemExit(f"unexpected fake limactl args: {args!r}")
"""
    )
    fake_limactl.chmod(0o755)


def run_migration(
    home: Path, fake_bin: Path, source: Path, *, mode: str = "success"
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(MIGRATION_SCRIPT), "test-vm"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "HOME": str(home),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "DEVBOX_MIGRATION_SOURCE": str(source),
            "DEVBOX_MIGRATION_INSTANCE": "test-vm",
            "DEVBOX_MIGRATION_MODE": mode,
        },
    )


@pytest.mark.unit
def test_seed_copies_preferences_but_not_volatile_state_or_symlinks(tmp_path: Path):
    source = tmp_path / "host-state"
    destination = tmp_path / "l1-state"
    source.mkdir()
    destination.mkdir()

    (source / "model.json").write_text('{"favorite": "model"}\n')
    (source / "session.json").write_text('{"pinned": []}\n')
    (source / "prompt-history.jsonl").write_text('{"text": "prompt"}\n')
    (source / "latest/tui").mkdir(parents=True)
    (source / "latest/tui/tabs.json").write_text('{"tabs": []}\n')
    (source / "latest/tui/plugin.view.json").write_text('{"view": "split"}\n')
    (source / "locks").mkdir()
    (source / "locks/session.lock").write_text("lock")
    (source / "latest/locks").mkdir()
    (source / "latest/locks/tabs.lock").write_text("lock")
    (source / "nested").mkdir()
    for name in (
        "service.json",
        "service-beta.json",
        "service.json.bak",
        "service.pid",
        "cache.tmp",
        "unknown-state.json",
    ):
        (source / name).write_text("volatile")
    (source / "nested/service-foo.json").write_text("volatile")
    (source / "nested/write.tmp").write_text("temporary")

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "must-not-copy").write_text("outside")
    (source / "linked-file").symlink_to(outside / "must-not-copy")
    (source / "linked-directory").symlink_to(outside, target_is_directory=True)

    result = run_seed(source, destination)

    assert result.returncode == 0, result.stderr
    assert (destination / "model.json").read_text() == '{"favorite": "model"}\n'
    assert (destination / "session.json").is_file()
    assert (destination / "prompt-history.jsonl").is_file()
    assert (destination / "latest/tui/tabs.json").is_file()
    assert (destination / "latest/tui/plugin.view.json").is_file()
    assert not (destination / "service.json").exists()
    assert not (destination / "service-beta.json").exists()
    assert not (destination / "service.json.bak").exists()
    assert not (destination / "service.pid").exists()
    assert not (destination / "unknown-state.json").exists()
    assert not (destination / "nested/service-foo.json").exists()
    assert not (destination / "cache.tmp").exists()
    assert not (destination / "nested/write.tmp").exists()
    assert not (destination / "locks").exists()
    assert not (destination / "latest/locks").exists()
    assert not (destination / "linked-file").exists()
    assert not (destination / "linked-directory").exists()
    assert (outside / "must-not-copy").read_text() == "outside"
    assert (destination / ".devbox-seeded-from-host").is_file()
    assert os.stat(destination).st_mode & 0o777 == 0o700
    assert os.stat(destination / "model.json").st_mode & 0o777 == 0o600


@pytest.mark.unit
def test_seed_is_once_only_and_never_overwrites_existing_l1_state(tmp_path: Path):
    source = tmp_path / "host-state"
    destination = tmp_path / "l1-state"
    source.mkdir()
    destination.mkdir()
    (source / "model.json").write_text("host model\n")
    (destination / "model.json").write_text("existing L1 model\n")

    first = run_seed(source, destination)
    assert first.returncode == 0, first.stderr
    assert (destination / "model.json").read_text() == "existing L1 model\n"

    (source / "new-preference.json").write_text("later host update\n")
    second = run_seed(source, destination)

    assert second.returncode == 0, second.stderr
    assert not (destination / "new-preference.json").exists()
    assert (destination / "model.json").read_text() == "existing L1 model\n"


@pytest.mark.unit
def test_seed_rejects_symlinked_mount_roots(tmp_path: Path):
    source = tmp_path / "host-state"
    destination = tmp_path / "l1-state"
    source.mkdir()
    actual_destination = tmp_path / "actual-destination"
    actual_destination.mkdir()
    destination.symlink_to(actual_destination, target_is_directory=True)

    result = run_seed(source, destination)

    assert result.returncode != 0
    assert "persistent state target is not a mounted directory" in result.stderr
    assert not (actual_destination / ".devbox-seeded-from-host").exists()


@pytest.mark.unit
def test_migration_replaces_safe_preferences_without_importing_service_state(
    tmp_path: Path,
):
    source = tmp_path / "old-vm-state"
    destination = tmp_path / "persistent-state"
    source.mkdir()
    destination.mkdir()
    (source / "model.json").write_text("old L1 model\n")
    (source / "prompt-history.jsonl").write_text("old L1 history\n")
    (source / "service.json").write_text("old registration\n")
    (destination / "model.json").write_text("initial host seed\n")
    (destination / ".devbox-seeded-from-host").touch()

    result = run_seed(source, destination, replace_existing=True)

    assert result.returncode == 0, result.stderr
    assert (destination / "model.json").read_text() == "old L1 model\n"
    assert (destination / "prompt-history.jsonl").read_text() == "old L1 history\n"
    assert not (destination / "service.json").exists()
    assert (destination / ".devbox-seeded-from-host").is_file()


@pytest.mark.unit
def test_migration_script_imports_legacy_l1_state_from_private_staging(
    tmp_path: Path,
):
    home = tmp_path / "home"
    source = tmp_path / "legacy-l1-state"
    fake_bin = tmp_path / "bin"
    home.mkdir()
    source.mkdir()
    (source / "model.json").write_text("legacy L1 model\n")
    (source / "service.json").write_text("volatile service registration\n")
    install_fake_limactl(fake_bin)

    state_target = home / ".local/state/devbox-opencode"
    state_target.mkdir(parents=True)
    (state_target / "model.json").write_text("host seed\n")
    (state_target / ".devbox-seeded-from-host").touch()
    stale_stage = home / ".local/state/devbox-opencode-migration/copy.stale"
    stale_stage.mkdir(parents=True)
    (stale_stage / "service.json").write_text("stale registration\n")
    result = run_migration(home, fake_bin, source)

    assert result.returncode == 0, result.stderr
    assert (state_target / "model.json").read_text() == "legacy L1 model\n"
    assert not (state_target / "service.json").exists()
    stage_root = home / ".local/state/devbox-opencode-migration"
    assert stage_root.stat().st_mode & 0o777 == 0o700
    assert {path.name for path in stage_root.iterdir()} == {"migration.lock"}
    assert (stage_root / "migration.lock").stat().st_mode & 0o777 == 0o600


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mode", "expected_returncode", "expected_message"),
    (
        ("stopped", 1, "start Lima VM 'test-vm'"),
        ("no-state", 0, "has no OpenCode state directory"),
        ("service-running", 1, "stop OpenCode and retry state migration"),
        ("copy-failure", 1, "keep the VM and resolve the copy error"),
    ),
)
def test_migration_script_reports_failure_and_no_state_paths(
    tmp_path: Path, mode: str, expected_returncode: int, expected_message: str
):
    home = tmp_path / "home"
    source = tmp_path / "legacy-l1-state"
    fake_bin = tmp_path / "bin"
    home.mkdir()
    source.mkdir()
    install_fake_limactl(fake_bin)

    result = run_migration(home, fake_bin, source, mode=mode)

    assert result.returncode == expected_returncode
    assert expected_message in result.stderr
    if mode == "no-state":
        assert not (home / ".local/state/devbox-opencode").exists()


@pytest.mark.unit
def test_migration_script_refuses_a_symlinked_lock(tmp_path: Path):
    home = tmp_path / "home"
    source = tmp_path / "legacy-l1-state"
    fake_bin = tmp_path / "bin"
    home.mkdir()
    source.mkdir()
    install_fake_limactl(fake_bin)

    stage_root = home / ".local/state/devbox-opencode-migration"
    stage_root.mkdir(parents=True)
    external_lock = tmp_path / "external-lock"
    external_lock.write_text("preserve\n")
    (stage_root / "migration.lock").symlink_to(external_lock)

    result = run_migration(home, fake_bin, source)

    assert result.returncode != 0
    assert "refusing unexpected OpenCode migration lock" in result.stderr
    assert external_lock.read_text() == "preserve\n"


@pytest.mark.unit
@pytest.mark.parametrize(
    "unsafe_path",
    (
        ".local/state/devbox-opencode",
        ".local/state/devbox-opencode-migration",
    ),
)
def test_migration_script_refuses_symlinked_state_paths(
    tmp_path: Path, unsafe_path: str
):
    home = tmp_path / "home"
    source = tmp_path / "legacy-l1-state"
    fake_bin = tmp_path / "bin"
    home.mkdir()
    source.mkdir()
    install_fake_limactl(fake_bin)
    external_path = tmp_path / "external"
    external_path.mkdir()
    symlink_path = home / unsafe_path
    symlink_path.parent.mkdir(parents=True)
    symlink_path.symlink_to(external_path, target_is_directory=True)

    result = run_migration(home, fake_bin, source)

    assert result.returncode != 0
    assert "refusing unexpected OpenCode migration path" in result.stderr
    assert list(external_path.iterdir()) == []
