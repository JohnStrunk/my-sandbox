#!/usr/bin/env python3
"""Open the shared VM test lock without following a symlink, then re-exec."""

from __future__ import annotations

import argparse
import errno
import os
import stat
import sys
from pathlib import Path

LOCK_FILE_NAME = "my-sandbox-vm-tests.lock"
LOCK_FD_ENV = "MY_SANDBOX_VM_TEST_LOCK_FD"


def fail(message: str) -> None:
    print(f"sanitized-test: {message}", file=sys.stderr)
    print(
        "sanitized-test: this is an infrastructure/runtime configuration failure, "
        "not a product test failure.",
        file=sys.stderr,
    )
    raise SystemExit(125)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock-directory", required=True, type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("a command is required after --")

    lock_directory = args.lock_directory
    lock_path = lock_directory / LOCK_FILE_NAME
    directory_fd = -1
    lock_fd = -1
    try:
        directory_fd = os.open(
            lock_directory,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        directory_stat = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or directory_stat.st_uid != os.getuid()
            or stat.S_IMODE(directory_stat.st_mode) & 0o022
        ):
            fail(
                "shared-VM lock directory must be owned by the current uid "
                f"and not group/world-writable: {lock_directory}"
            )

        try:
            entry_stat = os.stat(
                LOCK_FILE_NAME, dir_fd=directory_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(entry_stat.st_mode):
                fail(
                    "shared-VM lock path must be a regular file, not a symlink: "
                    f"{lock_path}"
                )

        try:
            lock_fd = os.open(
                LOCK_FILE_NAME,
                os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=directory_fd,
            )
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                fail(
                    "shared-VM lock path must be a regular file, not a symlink: "
                    f"{lock_path}"
                )
            fail(f"could not open shared-VM test lock {lock_path}: {exc}")

        lock_stat = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(lock_stat.st_mode)
            or lock_stat.st_uid != os.getuid()
            or lock_stat.st_nlink != 1
        ):
            fail(
                "shared-VM lock file must be a regular file owned by the current uid "
                f"with one link: {lock_path}"
            )
        os.fchmod(lock_fd, 0o600)
        os.close(directory_fd)
        directory_fd = -1

        os.set_inheritable(lock_fd, True)
        child_environment = os.environ.copy()
        child_environment[LOCK_FD_ENV] = str(lock_fd)
        try:
            os.execvpe(command[0], command, child_environment)
        except OSError as exc:
            fail(f"could not re-exec shared-VM test command {command[0]}: {exc}")
    except OSError as exc:
        fail(f"could not safely open shared-VM test lock {lock_path}: {exc}")
    finally:
        if directory_fd >= 0:
            os.close(directory_fd)
        if lock_fd >= 0:
            os.close(lock_fd)


if __name__ == "__main__":
    main()
