#!/usr/bin/env python3
"""Import safe OpenCode preferences into the persistent L1 state directory."""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

SEED_MARKER = ".devbox-seeded-from-host"
SAFE_STATE_FILES = frozenset(
    {
        "frecency.jsonl",
        "kv.json",
        "model.json",
        "prompt-history.jsonl",
        "prompt-stash.jsonl",
        "session.json",
        "tui.json",
    }
)


def _included_file(relative: Path) -> bool:
    if len(relative.parts) == 1:
        return relative.name in SAFE_STATE_FILES
    return (
        len(relative.parts) == 3
        and relative.parts[:2] == ("latest", "tui")
        and (
            relative.name == "tabs.json"
            or (relative.name.startswith("plugin.") and relative.suffix == ".json")
        )
    )


def _included_directory(relative: Path) -> bool:
    return relative in {Path("latest"), Path("latest/tui")}


def seed_state(
    source: Path, destination: Path, *, replace_existing: bool = False
) -> bool:
    """Copy allowlisted user state once, or refresh it during migration."""
    if source.is_symlink() or not source.is_dir():
        raise ValueError(f"seed source is not a mounted directory: {source}")
    if destination.is_symlink() or not destination.is_dir():
        raise ValueError(
            f"persistent state target is not a mounted directory: {destination}"
        )

    destination.chmod(0o700)
    marker = destination / SEED_MARKER
    if marker.is_symlink():
        raise ValueError(f"seed marker must not be a symlink: {marker}")
    marker_exists = marker.is_file()
    if marker_exists and not replace_existing:
        return False
    if marker.exists() and not marker_exists:
        raise ValueError(f"seed marker is not a regular file: {marker}")

    for source_root, directory_names, file_names in os.walk(source, followlinks=False):
        source_dir = Path(source_root)
        relative_dir = source_dir.relative_to(source)

        kept_directories = []
        for name in directory_names:
            relative = relative_dir / name
            source_child = source_dir / name
            destination_child = destination / relative
            if not _included_directory(relative) or source_child.is_symlink():
                continue
            if destination_child.is_symlink() or (
                destination_child.exists() and not destination_child.is_dir()
            ):
                continue
            destination_child.mkdir(mode=0o700, parents=True, exist_ok=True)
            destination_child.chmod(0o700)
            kept_directories.append(name)
        directory_names[:] = kept_directories

        for name in file_names:
            relative = relative_dir / name
            source_file = source_dir / name
            destination_file = destination / relative
            if not _included_file(relative) or source_file.is_symlink():
                continue
            try:
                source_mode = source_file.lstat().st_mode
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(source_mode):
                continue
            if destination_file.is_symlink():
                continue
            destination_exists = destination_file.exists()
            if destination_exists and not replace_existing:
                continue
            if destination_exists and not stat.S_ISREG(
                destination_file.lstat().st_mode
            ):
                continue

            destination_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            source_fd = os.open(
                source_file,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            )
            temporary_path: Path | None = None
            try:
                if not stat.S_ISREG(os.fstat(source_fd).st_mode):
                    continue
                destination_fd, temporary_name = tempfile.mkstemp(
                    prefix=f".{name}.", dir=destination_file.parent
                )
                temporary_path = Path(temporary_name)
                os.fchmod(destination_fd, 0o600)
                try:
                    with os.fdopen(source_fd, "rb", closefd=False) as source_stream:
                        with os.fdopen(destination_fd, "wb") as destination_stream:
                            shutil.copyfileobj(source_stream, destination_stream)
                            destination_stream.flush()
                            os.fsync(destination_stream.fileno())
                    os.replace(temporary_path, destination_file)
                except BaseException:
                    temporary_path.unlink(missing_ok=True)
                    raise
            finally:
                os.close(source_fd)

    if not marker_exists:
        marker_fd = os.open(
            marker,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        os.close(marker_fd)
    return True


def main() -> int:
    arguments = sys.argv[1:]
    replace_existing = arguments[:1] == ["--replace-existing"]
    if replace_existing:
        arguments = arguments[1:]
    if len(arguments) != 2:
        print(
            f"usage: {sys.argv[0]} [--replace-existing] SOURCE_DIR DESTINATION_DIR",
            file=sys.stderr,
        )
        return 2
    source, destination = map(Path, arguments)
    try:
        seeded = seed_state(source, destination, replace_existing=replace_existing)
    except (OSError, ValueError) as error:
        print(
            f"devbox: could not seed persistent OpenCode state: {error}",
            file=sys.stderr,
        )
        return 1
    if seeded:
        print("devbox: imported existing OpenCode preferences into persistent L1 state")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
