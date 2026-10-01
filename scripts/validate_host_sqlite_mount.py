#!/usr/bin/env python3
"""Exercise host/L1 SQLite writes on the VM runner's disposable OpenCode mount.

The no-argument host entry point is intended to be called only by
``scripts/run-vm-ci.sh vm``. It refuses host paths outside that runner's
temporary HOME/LIMA_HOME tree. The L1 worker opens the existing database with
SQLite URI ``mode=rw`` so a missing mount cannot create a guest-local database.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

RUNNER_TEST_PREFIX = "my-sandbox-vm-test."
SCRATCH_PREFIX = "issue-287-test-only-"
SCRATCH_NAME_PATTERN = re.compile(r"issue-287-test-only-[0-9a-f]{32}\Z")
DATABASE_NAME = "issue-287-test-only.sqlite3"
TABLE_NAME = "issue_287_test_only_rows"
MANIFEST_NAME = ".issue-287-test-only"
INBOUND_SENTINEL_NAME = "issue-287-mount-inbound"
INBOUND_SENTINEL_CONTENT = "host-to-L1\n"
L1_TEST_ENVIRONMENT = "MY_SANDBOX_DISPOSABLE_SQLITE_VALIDATION"
GUEST_REPO = "/workspace/src/my-sandbox"
GUEST_INSTANCE = "devbox"

ROWS_PER_WORKER = 10
BUSY_TIMEOUT_SECONDS = 2.0
BUSY_TIMEOUT_MILLISECONDS = 2_000
TRANSACTION_HOLD_SECONDS = 0.01
CONTENTION_BUSY_TIMEOUT_MILLISECONDS = 250
MIN_CONTENTION_WAIT_SECONDS = 0.15
MAX_CONTENTION_WAIT_SECONDS = 1.5
CONTENTION_TIMEOUT_SECONDS = 10.0
GUEST_WORKER_TIMEOUT_SECONDS = 35
WORKER_WAIT_TIMEOUT_SECONDS = 40
WORKER_ABORT_GRACE_SECONDS = 5
PROCESS_TERM_TIMEOUT_SECONDS = 2
PROCESS_KILL_TIMEOUT_SECONDS = 2
GUEST_VERIFY_TIMEOUT_SECONDS = 10
VERIFY_PROCESS_TIMEOUT_SECONDS = 15
MAX_DIAGNOSTIC_LOG_BYTES = 64 * 1024
ABORT_CONTENT = "coordinator abort\n"
PRESERVE_ROOT_MARKER = ".preserve-live-sqlite-worker"
PRESERVE_ROOT_EXIT_CODE = 75
LOG_DIRECTORY_NAME = "sqlite-validation-logs"
L1_STOP_TIMEOUT_SECONDS = 30
MAX_MARKER_BYTES = 4096
MAX_SNAPSHOT_FILE_BYTES = 16 * 1024 * 1024
SNAPSHOT_DIRECTORY_NAME = "host-db-snapshot"
_preserve_root_required = False
_received_signal: int | None = None


class SafePathError(RuntimeError):
    """The supplied HOME/LIMA_HOME paths are not the runner's disposable paths."""


@dataclass(frozen=True)
class DisposablePaths:
    runner_temp: Path
    test_root: Path
    host_home: Path
    lima_home: Path


@dataclass(frozen=True)
class ScratchDatabase:
    name: str
    directory: Path
    database: Path


def _resolved_directory(path: Path, description: str) -> Path:
    if not path.is_absolute():
        raise SafePathError(f"{description} must be an absolute path")
    if path.is_symlink():
        raise SafePathError(f"refusing symlinked {description}: {path}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise SafePathError(f"cannot resolve {description}: {path}: {error}") from error
    if not resolved.is_dir():
        raise SafePathError(f"{description} is not a directory: {path}")
    return resolved


def validate_disposable_paths(
    host_home: Path, lima_home: Path, runner_temp: Path
) -> DisposablePaths:
    """Accept only the sibling paths created by ``run-vm-ci.sh``."""
    runner_temp_path = runner_temp
    if not runner_temp_path.is_absolute():
        raise SafePathError("RUNNER_TEMP must be an absolute path")
    try:
        runner_temp_resolved = runner_temp_path.resolve(strict=True)
    except OSError as error:
        raise SafePathError(
            f"cannot resolve RUNNER_TEMP {runner_temp_path}: {error}"
        ) from error
    if not runner_temp_resolved.is_dir():
        raise SafePathError(f"RUNNER_TEMP is not a directory: {runner_temp_path}")

    if host_home.name != "home" or lima_home.name != "lima":
        raise SafePathError(
            "HOME and LIMA_HOME must be the runner's home/lima siblings"
        )
    if host_home.parent != lima_home.parent:
        raise SafePathError("HOME and LIMA_HOME must share one temporary test root")

    test_root_path = host_home.parent
    if not re.fullmatch(
        re.escape(RUNNER_TEST_PREFIX) + r"[A-Za-z0-9]{6,}", test_root_path.name
    ):
        raise SafePathError(
            f"temporary test root does not have the expected name: {test_root_path}"
        )

    test_root = _resolved_directory(test_root_path, "temporary test root")
    host_home_resolved = _resolved_directory(host_home, "temporary HOME")
    lima_home_resolved = _resolved_directory(lima_home, "temporary LIMA_HOME")
    if test_root.parent != runner_temp_resolved:
        raise SafePathError("temporary test root must be directly inside RUNNER_TEMP")
    if host_home_resolved != test_root / "home":
        raise SafePathError("HOME must be the home child of the temporary test root")
    if lima_home_resolved != test_root / "lima":
        raise SafePathError(
            "LIMA_HOME must be the lima child of the temporary test root"
        )

    for path, description in (
        (test_root, "temporary test root"),
        (host_home_resolved, "temporary HOME"),
    ):
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise SafePathError(f"{description} must not be accessible by group/others")

    return DisposablePaths(
        runner_temp=runner_temp_resolved,
        test_root=test_root,
        host_home=host_home_resolved,
        lima_home=lima_home_resolved,
    )


def _paths_from_environment() -> DisposablePaths:
    try:
        host_home = Path(os.environ["HOME"])
        lima_home = Path(os.environ["LIMA_HOME"])
    except KeyError as error:
        raise SafePathError(
            f"{error.args[0]} is required for host validation"
        ) from error
    runner_temp = Path(os.environ.get("RUNNER_TEMP", "/tmp"))
    return validate_disposable_paths(host_home, lima_home, runner_temp)


def _host_data_directory(host_home: Path) -> Path:
    """Require the disposable OpenCode data directory to be a real directory."""
    home = _resolved_directory(host_home, "temporary HOME")
    current = home
    for component in (".local", "share", "opencode"):
        current = current / component
        if current.is_symlink():
            raise SafePathError(
                f"refusing symlinked temporary OpenCode path: {current}"
            )
        try:
            resolved = current.resolve(strict=True)
        except OSError as error:
            raise SafePathError(
                f"temporary OpenCode data directory is missing: {current}: {error}"
            ) from error
        if not resolved.is_dir() or not resolved.is_relative_to(home):
            raise SafePathError(
                f"temporary OpenCode path is not a directory under HOME: {current}"
            )
        current = resolved
    return current


def _guest_data_directory() -> Path:
    """Return the L1's provisioned alias for the host OpenCode data mount."""
    return Path.home() / ".local" / "share" / "opencode"


def _require_inbound_sentinel(data_directory: Path, side: str) -> None:
    sentinel = data_directory / INBOUND_SENTINEL_NAME
    try:
        content = _read_nofollow_text(sentinel, "disposable mount sentinel", 256)
    except (OSError, RuntimeError) as error:
        raise RuntimeError(
            f"{side} cannot securely read the mount sentinel: {error}"
        ) from error
    if content != INBOUND_SENTINEL_CONTENT:
        raise RuntimeError(f"{side} saw unexpected disposable mount sentinel content")


def _open_nofollow_regular(path: Path, flags: int, mode: int = 0o600) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise RuntimeError("this validation requires O_NOFOLLOW support")
    descriptor = os.open(
        path,
        flags | nofollow | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0),
        mode,
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError(f"refusing non-regular file: {path.name}")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _read_nofollow_text(path: Path, description: str, max_bytes: int) -> str:
    try:
        descriptor = _open_nofollow_regular(path, os.O_RDONLY)
    except OSError as error:
        raise RuntimeError(f"cannot no-follow open {description}: {error}") from error
    with os.fdopen(descriptor, "rb") as source:
        content = source.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise RuntimeError(f"{description} exceeds its size limit")
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RuntimeError(f"{description} is not UTF-8") from error


def _write_private_file(path: Path, content: str) -> None:
    descriptor = _open_nofollow_regular(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(content)


def _manifest_content(scratch_name: str) -> str:
    return (
        f"scratch={scratch_name}\n"
        f"sentinel={INBOUND_SENTINEL_NAME}\n"
        f"content={INBOUND_SENTINEL_CONTENT}"
    )


def _copy_nofollow_file(
    source: Path, destination: Path, *, optional: bool = False
) -> bool:
    try:
        source_fd = _open_nofollow_regular(source, os.O_RDONLY)
    except FileNotFoundError as error:
        if optional:
            return False
        raise RuntimeError(
            f"required SQLite snapshot source is missing: {source.name}"
        ) from error
    except OSError as error:
        raise RuntimeError(
            f"cannot no-follow open SQLite snapshot source {source.name}: {error}"
        ) from error

    destination_fd: int | None = None
    try:
        source_stat = os.fstat(source_fd)
        if source_stat.st_size > MAX_SNAPSHOT_FILE_BYTES:
            raise RuntimeError(
                f"SQLite snapshot source is too large: {source.name} "
                f"({source_stat.st_size} bytes)"
            )
        destination_fd = _open_nofollow_regular(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        while True:
            chunk = os.read(source_fd, 64 * 1024)
            if not chunk:
                break
            remaining = memoryview(chunk)
            while remaining:
                written = os.write(destination_fd, remaining)
                if written <= 0:
                    raise OSError("short write while creating SQLite snapshot")
                remaining = remaining[written:]
        os.fsync(destination_fd)
    finally:
        os.close(source_fd)
        if destination_fd is not None:
            os.close(destination_fd)
    return True


def _create_database(database: Path) -> None:
    previous_umask = os.umask(0o077)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            database,
            timeout=BUSY_TIMEOUT_SECONDS,
            isolation_level=None,
        )
        journal_mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
        if journal_mode is None or str(journal_mode[0]).lower() != "wal":
            selected = None if journal_mode is None else journal_mode[0]
            raise RuntimeError(f"SQLite selected journal mode {selected!r}, not WAL")
        connection.execute(
            f"""CREATE TABLE {TABLE_NAME} (
                run_id TEXT NOT NULL,
                writer TEXT NOT NULL CHECK (writer IN ('host', 'l1')),
                sequence INTEGER NOT NULL CHECK (sequence >= 0),
                payload TEXT NOT NULL,
                PRIMARY KEY (run_id, writer, sequence)
            )"""
        )
        connection.execute("PRAGMA user_version=287")
    finally:
        if connection is not None:
            connection.close()
        os.umask(previous_umask)

    database.chmod(0o600)


def prepare_test_database(
    host_home: Path, lima_home: Path, runner_temp: Path
) -> ScratchDatabase:
    """Create a private test-only database under a validated disposable HOME."""
    paths = validate_disposable_paths(host_home, lima_home, runner_temp)
    data_directory = _host_data_directory(paths.host_home)
    _require_inbound_sentinel(data_directory, "host")
    data_directory.chmod(0o700)

    name = f"{SCRATCH_PREFIX}{secrets.token_hex(16)}"
    scratch_directory = data_directory / name
    scratch_directory.mkdir(mode=0o700, exist_ok=False)
    scratch_directory.chmod(0o700)
    _write_private_file(
        scratch_directory / MANIFEST_NAME,
        _manifest_content(name),
    )

    database = scratch_directory / DATABASE_NAME
    _create_database(database)
    return ScratchDatabase(name=name, directory=scratch_directory, database=database)


def _scratch_name_is_safe(name: str) -> bool:
    return SCRATCH_NAME_PATTERN.fullmatch(name) is not None


def _locate_scratch(data_directory: Path, name: str) -> ScratchDatabase:
    if not _scratch_name_is_safe(name):
        raise SafePathError("invalid test-only SQLite scratch directory name")
    if not data_directory.is_dir():
        raise RuntimeError("OpenCode data mount is not a directory")
    data_root = data_directory.resolve(strict=True)
    scratch_directory = data_directory / name
    if scratch_directory.is_symlink() or not scratch_directory.is_dir():
        raise RuntimeError(
            "test-only SQLite scratch directory is missing from the mount"
        )
    resolved_scratch = scratch_directory.resolve(strict=True)
    if not resolved_scratch.is_relative_to(data_root):
        raise SafePathError(
            "test-only SQLite directory escaped the OpenCode data mount"
        )

    manifest = scratch_directory / MANIFEST_NAME
    if _read_nofollow_text(manifest, "test-only SQLite manifest", 512) != (
        _manifest_content(name)
    ):
        raise RuntimeError("test-only SQLite manifest does not match this run")

    database = scratch_directory / DATABASE_NAME
    if database.is_symlink():
        raise SafePathError("refusing a symlinked test-only SQLite database")
    try:
        database_stat = database.lstat()
    except OSError as error:
        raise RuntimeError(f"test-only SQLite database is missing: {error}") from error
    if not stat.S_ISREG(database_stat.st_mode):
        raise SafePathError("test-only SQLite database is not a regular file")
    return ScratchDatabase(
        name=name,
        directory=resolved_scratch,
        database=resolved_scratch / DATABASE_NAME,
    )


def locate_prepared_test_database(
    paths: DisposablePaths, scratch_name: str
) -> ScratchDatabase:
    """Validate an existing prepare-action database without opening it."""
    data_directory = _host_data_directory(paths.host_home)
    _require_inbound_sentinel(data_directory, "host")
    scratch = _locate_scratch(data_directory, scratch_name)
    database_stat = scratch.database.lstat()
    if not stat.S_ISREG(database_stat.st_mode) or database_stat.st_size < 16:
        raise SafePathError("prepared SQLite database is not a complete regular file")
    if stat.S_IMODE(database_stat.st_mode) & 0o077:
        raise SafePathError("prepared SQLite database permissions are not restrictive")
    return scratch


def sqlite_rw_uri(database: Path) -> str:
    """Build a URI that opens only an existing database and never creates one."""
    return f"{database.resolve(strict=False).as_uri()}?mode=rw"


def connect_existing_database(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        sqlite_rw_uri(database),
        uri=True,
        timeout=BUSY_TIMEOUT_SECONDS,
        isolation_level=None,
    )
    try:
        busy_timeout = connection.execute(
            f"PRAGMA busy_timeout={BUSY_TIMEOUT_MILLISECONDS}"
        ).fetchone()
        if busy_timeout is None or busy_timeout[0] != BUSY_TIMEOUT_MILLISECONDS:
            raise RuntimeError("SQLite did not apply the requested busy timeout")
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
        if journal_mode is None or str(journal_mode[0]).lower() != "wal":
            selected = None if journal_mode is None else journal_mode[0]
            raise RuntimeError(f"test-only database is not in WAL mode: {selected!r}")
    except BaseException:
        connection.close()
        raise
    return connection


def _expected_rows(run_id: str) -> list[tuple[str, str, int, str]]:
    return [
        (
            run_id,
            writer,
            sequence,
            f"issue-287-test-only:{run_id}:{writer}:{sequence}",
        )
        for writer in ("host", "l1")
        for sequence in range(ROWS_PER_WORKER)
    ]


def verify_database(database: Path, run_id: str, observer: str) -> None:
    """Check exact test rows and SQLite integrity from the observing side."""
    connection = connect_existing_database(database)
    problems: list[str] = []
    try:
        try:
            tables = connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        except sqlite3.Error as error:
            tables = []
            problems.append(f"could not list test-only tables: {error}")
        if tables != [(TABLE_NAME,)]:
            problems.append(f"unexpected test-only tables: {tables!r}")

        if (TABLE_NAME,) in tables:
            try:
                actual = connection.execute(
                    f"SELECT run_id, writer, sequence, payload FROM {TABLE_NAME} "
                    "ORDER BY writer, sequence"
                ).fetchall()
                expected = _expected_rows(run_id)
                if actual != expected:
                    missing_rows = sorted(set(expected) - set(actual))
                    unexpected_rows = sorted(set(actual) - set(expected))
                    problems.append(
                        f"expected {len(expected)} exact rows but observed "
                        f"{len(actual)}; first missing={missing_rows[:1]!r}, "
                        f"first unexpected={unexpected_rows[:1]!r}"
                    )
            except sqlite3.Error as error:
                problems.append(f"could not read expected test-only rows: {error}")

        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchall()
            if integrity != [("ok",)]:
                problems.append(f"PRAGMA integrity_check returned {integrity!r}")
        except sqlite3.Error as error:
            problems.append(f"PRAGMA integrity_check failed: {error}")
    finally:
        connection.close()

    if problems:
        raise RuntimeError(
            f"{observer} database verification failed: " + "; ".join(problems)
        )


def _data_directory_for_side(side: str) -> Path:
    if side == "host":
        paths = _paths_from_environment()
        return _host_data_directory(paths.host_home)
    if side == "l1":
        if os.environ.get(L1_TEST_ENVIRONMENT) != "1":
            raise SafePathError(
                "L1 SQLite worker requires the runner's explicit disposable-test marker"
            )
        return _guest_data_directory()
    raise ValueError(f"unsupported worker side: {side}")


def _marker_matches(scratch: ScratchDatabase, name: str, content: str) -> bool:
    marker = scratch.directory / name
    try:
        descriptor = _open_nofollow_regular(marker, os.O_RDONLY)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise RuntimeError(
            f"cannot no-follow open SQLite synchronization marker {name}: {error}"
        ) from error
    except RuntimeError as error:
        raise RuntimeError(
            f"unsafe SQLite synchronization marker {name}: {error}"
        ) from error
    with os.fdopen(descriptor, "rb") as marker_file:
        raw_content = marker_file.read(MAX_MARKER_BYTES + 1)
    if len(raw_content) > MAX_MARKER_BYTES:
        raise RuntimeError(f"SQLite synchronization marker {name} is too large")
    try:
        actual = raw_content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RuntimeError(
            f"SQLite synchronization marker {name} is not UTF-8"
        ) from error
    if actual != content:
        raise RuntimeError(
            f"SQLite synchronization marker {name} has unexpected contents"
        )
    return True


def _write_barrier_file(scratch: ScratchDatabase, name: str, content: str) -> None:
    if _marker_matches(scratch, name, content):
        return
    marker = scratch.directory / name
    temporary = scratch.directory / f".{name}.{secrets.token_hex(8)}.tmp"
    _write_private_file(temporary, content)
    try:
        # Each protocol marker has one designated writer. Rename publishes its
        # complete contents atomically for the other side of the 9p mount.
        if not _marker_matches(scratch, name, content):
            os.replace(temporary, marker)
    finally:
        # Only this unpublished synchronization-marker temp is removed; SQLite
        # databases and WAL sidecars are left for run-vm-ci's root cleanup.
        temporary.unlink(missing_ok=True)


def _wait_for_marker(
    scratch: ScratchDatabase,
    name: str,
    content: str,
    side: str,
    deadline: float,
    phase: str,
) -> None:
    while True:
        if name != "abort" and _marker_matches(scratch, "abort", ABORT_CONTENT):
            raise RuntimeError(f"{side} worker observed the coordinator abort marker")
        if _marker_matches(scratch, name, content):
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{side} timed out waiting for {phase} marker {name!r}")
        time.sleep(0.02)


def _insert_test_row(
    connection: sqlite3.Connection, run_id: str, writer: str, sequence: int
) -> None:
    connection.execute(
        f"INSERT INTO {TABLE_NAME} "
        "(run_id, writer, sequence, payload) VALUES (?, ?, ?, ?)",
        (
            run_id,
            writer,
            sequence,
            f"issue-287-test-only:{run_id}:{writer}:{sequence}",
        ),
    )


def _run_host_lock_holder(
    connection: sqlite3.Connection, scratch: ScratchDatabase
) -> None:
    connection.execute("BEGIN IMMEDIATE")
    _insert_test_row(connection, scratch.name, "host", 0)
    _write_barrier_file(scratch, "lock-held", "host holds BEGIN IMMEDIATE\n")
    try:
        deadline = time.monotonic() + CONTENTION_TIMEOUT_SECONDS
        _wait_for_marker(
            scratch,
            "l1-attempting",
            "L1 is attempting BEGIN IMMEDIATE\n",
            "host",
            deadline,
            "L1 BEGIN IMMEDIATE attempt",
        )
        _wait_for_marker(
            scratch,
            "l1-contended",
            "L1 observed SQLITE_BUSY while host lock was held\n",
            "host",
            deadline,
            "demonstrated SQLite lock contention",
        )
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    _write_barrier_file(
        scratch, "lock-released", "host committed lock-holder transaction\n"
    )


def _run_l1_lock_contender(
    connection: sqlite3.Connection, scratch: ScratchDatabase
) -> tuple[float, int]:
    _wait_for_marker(
        scratch,
        "lock-held",
        "host holds BEGIN IMMEDIATE\n",
        "l1",
        time.monotonic() + CONTENTION_TIMEOUT_SECONDS,
        "host lock holder",
    )
    short_timeout = connection.execute(
        f"PRAGMA busy_timeout={CONTENTION_BUSY_TIMEOUT_MILLISECONDS}"
    ).fetchone()
    if short_timeout != (CONTENTION_BUSY_TIMEOUT_MILLISECONDS,):
        raise RuntimeError("SQLite did not apply the short contention busy timeout")

    _write_barrier_file(scratch, "l1-attempting", "L1 is attempting BEGIN IMMEDIATE\n")
    started = time.monotonic()
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as error:
        waited = time.monotonic() - started
        sqlite_error_code = getattr(error, "sqlite_errorcode", None)
        if sqlite_error_code is None:
            raise RuntimeError(
                f"L1 BEGIN IMMEDIATE error lacks sqlite_errorcode after {waited:.3f}s"
            ) from error
        if sqlite_error_code != sqlite3.SQLITE_BUSY:
            raise RuntimeError(
                "L1 BEGIN IMMEDIATE failed without SQLITE_BUSY: "
                f"code={sqlite_error_code!r}, "
                f"name={getattr(error, 'sqlite_errorname', None)!r}, "
                f"wait={waited:.3f}s"
            ) from error
        if not MIN_CONTENTION_WAIT_SECONDS <= waited <= MAX_CONTENTION_WAIT_SECONDS:
            raise RuntimeError(
                f"L1 SQLITE_BUSY wait was outside the expected range: {waited:.3f}s"
            ) from error
    else:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise RuntimeError(
            "L1 BEGIN IMMEDIATE succeeded while the host lock holder was active"
        )

    if connection.in_transaction:
        connection.execute("ROLLBACK")
    _write_barrier_file(
        scratch, "l1-contended", "L1 observed SQLITE_BUSY while host lock was held\n"
    )
    _wait_for_marker(
        scratch,
        "lock-released",
        "host committed lock-holder transaction\n",
        "l1",
        time.monotonic() + CONTENTION_TIMEOUT_SECONDS,
        "host lock release",
    )

    normal_timeout = connection.execute(
        f"PRAGMA busy_timeout={BUSY_TIMEOUT_MILLISECONDS}"
    ).fetchone()
    if normal_timeout != (BUSY_TIMEOUT_MILLISECONDS,):
        raise RuntimeError("L1 could not restore the normal SQLite busy timeout")
    connection.execute("BEGIN IMMEDIATE")
    try:
        _insert_test_row(connection, scratch.name, "l1", 0)
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise
    return waited, sqlite_error_code


def run_worker(side: str, scratch_name: str) -> None:
    """Run a bounded writer using only the mounted, pre-existing test database."""
    data_directory = _data_directory_for_side(side)
    _require_inbound_sentinel(data_directory, side)
    scratch = _locate_scratch(data_directory, scratch_name)

    previous_umask = os.umask(0o077)
    connection: sqlite3.Connection | None = None
    try:
        connection = connect_existing_database(scratch.database)
        contention_result = None
        if side == "host":
            _run_host_lock_holder(connection, scratch)
        else:
            contention_result = _run_l1_lock_contender(connection, scratch)

        for sequence in range(1, ROWS_PER_WORKER):
            if _marker_matches(scratch, "abort", ABORT_CONTENT):
                raise RuntimeError(
                    f"{side} worker observed the coordinator abort marker"
                )
            try:
                connection.execute("BEGIN IMMEDIATE")
                _insert_test_row(connection, scratch_name, side, sequence)
                time.sleep(TRANSACTION_HOLD_SECONDS)
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    try:
                        connection.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                raise
        message = f"{side} worker committed {ROWS_PER_WORKER} bounded transactions"
        if contention_result is not None:
            contention_wait, sqlite_error_code = contention_result
            message += (
                f"; observed SQLITE_BUSY (code {sqlite_error_code}) after "
                f"{contention_wait:.3f}s"
            )
        print(message, flush=True)
    except BaseException:
        if connection is not None and connection.in_transaction:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        _request_abort(scratch)
        raise
    finally:
        if connection is not None:
            connection.close()
        os.umask(previous_umask)


def _guest_command(action: str, scratch_name: str) -> list[str]:
    if not _scratch_name_is_safe(scratch_name):
        raise SafePathError("invalid test-only SQLite scratch directory name")
    guest_script = f"{GUEST_REPO}/scripts/{Path(__file__).name}"
    command = [
        "limactl",
        "shell",
        "--workdir",
        GUEST_REPO,
        GUEST_INSTANCE,
    ]
    command.extend(["env", f"{L1_TEST_ENVIRONMENT}=1"])
    if action == "worker":
        command.extend(
            [
                "timeout",
                "--signal=TERM",
                "--kill-after=2s",
                f"{GUEST_WORKER_TIMEOUT_SECONDS}s",
            ]
        )
    elif action == "verify":
        command.extend(
            [
                "timeout",
                "--signal=TERM",
                "--kill-after=2s",
                f"{GUEST_VERIFY_TIMEOUT_SECONDS}s",
            ]
        )
    else:
        raise ValueError(f"unsupported guest command action: {action}")
    command.extend(
        [
            "python3",
            "-I",
            guest_script,
            f"--{action}",
            "l1",
            "--scratch-name",
            scratch_name,
        ]
    )
    return command


def _request_abort(scratch: ScratchDatabase) -> None:
    try:
        _write_barrier_file(scratch, "abort", ABORT_CONTENT)
    except (OSError, RuntimeError) as error:
        print(f"could not publish SQLite worker abort marker: {error}", file=sys.stderr)


def _open_private_log(path: Path):
    descriptor = _open_nofollow_regular(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    return os.fdopen(descriptor, "wb", buffering=0)


def _read_log(path: Path) -> str:
    try:
        descriptor = _open_nofollow_regular(path, os.O_RDONLY)
        with os.fdopen(descriptor, "rb") as log:
            size = os.fstat(log.fileno()).st_size
            skipped = max(0, size - MAX_DIAGNOSTIC_LOG_BYTES)
            if skipped:
                log.seek(skipped)
            content = log.read()
    except (OSError, RuntimeError) as error:
        raise RuntimeError(
            f"could not no-follow read log {path.name}: {error}"
        ) from error
    prefix = f"[truncated {skipped} earlier log bytes]\n" if skipped else ""
    return prefix + content.decode("utf-8", errors="replace")


def _stop_and_reap(process: subprocess.Popen[bytes] | None, label: str) -> None:
    if process is None:
        return
    if process.poll() is not None:
        process.wait(timeout=0)
        return
    try:
        process.wait(timeout=WORKER_ABORT_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        print(f"{label} did not stop after abort; sending SIGTERM", file=sys.stderr)
        process.terminate()
    try:
        process.wait(timeout=PROCESS_TERM_TIMEOUT_SECONDS)
        return
    except subprocess.TimeoutExpired:
        print(f"{label} ignored SIGTERM; sending SIGKILL", file=sys.stderr)
        process.kill()
    try:
        process.wait(timeout=PROCESS_KILL_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        raise TimeoutError(f"could not reap {label} after SIGKILL") from error


def _process_diagnostic(
    label: str,
    returncode: int | None,
    stdout_path: Path,
    stderr_path: Path,
) -> str:
    lines = [f"{label} exited with status {returncode}"]
    stdout = _read_log(stdout_path)
    stderr = _read_log(stderr_path)
    if stdout.strip():
        lines.append(f"{label} stdout:\n{stdout.rstrip()}")
    if stderr.strip():
        lines.append(f"{label} stderr:\n{stderr.rstrip()}")
    return "\n".join(lines)


def _preserve_test_root(paths: DisposablePaths, reason: str) -> None:
    global _preserve_root_required
    _preserve_root_required = True
    marker = paths.test_root / PRESERVE_ROOT_MARKER
    try:
        _write_private_file(marker, f"{reason}\n")
    except FileExistsError:
        pass
    except OSError as error:
        print(
            f"could not mark temporary root for preservation: {error}", file=sys.stderr
        )


def _prepare_log_directory(paths: DisposablePaths) -> Path:
    """Keep coordinator logs in the private runner root, outside guest mounts."""
    log_directory = paths.test_root / LOG_DIRECTORY_NAME
    log_directory.mkdir(mode=0o700, exist_ok=False)
    log_directory.chmod(0o700)
    resolved = log_directory.resolve(strict=True)
    if resolved.parent != paths.test_root or resolved.is_relative_to(paths.host_home):
        raise SafePathError("host validation logs escaped the private test root")
    return resolved


def _wait_for_host_lock_held(
    process: subprocess.Popen[bytes], scratch: ScratchDatabase, deadline: float
) -> None:
    while True:
        if _marker_matches(scratch, "abort", ABORT_CONTENT):
            raise RuntimeError("host worker aborted before publishing lock-held")
        if _marker_matches(scratch, "lock-held", "host holds BEGIN IMMEDIATE\n"):
            return
        returncode = process.poll()
        if returncode is not None:
            raise RuntimeError(
                "host worker exited before acquiring its SQLite lock "
                f"(status {returncode})"
            )
        if time.monotonic() >= deadline:
            raise TimeoutError("host worker timed out before publishing lock-held")
        time.sleep(0.02)


def _run_workers(
    scratch: ScratchDatabase, paths: DisposablePaths, log_directory: Path
) -> None:
    worker_environment = {
        "HOME": str(paths.host_home),
        "LIMA_HOME": str(paths.lima_home),
        "RUNNER_TEMP": str(paths.runner_temp),
    }
    limactl_environment = {
        "HOME": str(paths.host_home),
        "LIMA_HOME": str(paths.lima_home),
        "PATH": os.environ.get("PATH", os.defpath),
    }
    host_command = [
        sys.executable,
        "-I",
        str(Path(__file__).resolve()),
        "--worker",
        "host",
        "--scratch-name",
        scratch.name,
    ]
    guest_command = _guest_command("worker", scratch.name)
    host_stdout_path = log_directory / "host-worker.stdout.log"
    host_stderr_path = log_directory / "host-worker.stderr.log"
    guest_stdout_path = log_directory / "l1-worker.stdout.log"
    guest_stderr_path = log_directory / "l1-worker.stderr.log"
    streams = []
    host_process: subprocess.Popen[bytes] | None = None
    guest_process: subprocess.Popen[bytes] | None = None
    try:
        host_stdout = _open_private_log(host_stdout_path)
        streams.append(host_stdout)
        host_stderr = _open_private_log(host_stderr_path)
        streams.append(host_stderr)
        guest_stdout = _open_private_log(guest_stdout_path)
        streams.append(guest_stdout)
        guest_stderr = _open_private_log(guest_stderr_path)
        streams.append(guest_stderr)
        deadline = time.monotonic() + WORKER_WAIT_TIMEOUT_SECONDS
        host_process = subprocess.Popen(
            host_command,
            stdin=subprocess.DEVNULL,
            stdout=host_stdout,
            stderr=host_stderr,
            env=worker_environment,
        )
        _wait_for_host_lock_held(host_process, scratch, deadline)
        guest_process = subprocess.Popen(
            guest_command,
            stdin=subprocess.DEVNULL,
            stdout=guest_stdout,
            stderr=guest_stderr,
            env=limactl_environment,
        )

        while True:
            exited = {
                "host": host_process.poll(),
                "l1": guest_process.poll(),
            }
            failed = [
                (side, code) for side, code in exited.items() if code not in (None, 0)
            ]
            if failed:
                side, code = failed[0]
                raise RuntimeError(
                    f"{side} SQLite writer exited early with status {code}"
                )
            if all(code == 0 for code in exited.values()):
                break
            if time.monotonic() >= deadline:
                running = [side for side, code in exited.items() if code is None]
                raise TimeoutError(
                    f"timed out after {WORKER_WAIT_TIMEOUT_SECONDS}s waiting for "
                    f"SQLite writers: {', '.join(running)} still running"
                )
            time.sleep(0.05)

        host_stdout_text = _read_log(host_stdout_path)
        host_stderr_text = _read_log(host_stderr_path)
        guest_stdout_text = _read_log(guest_stdout_path)
        guest_stderr_text = _read_log(guest_stderr_path)
        print(host_stdout_text, end="")
        print(guest_stdout_text, end="")
        if host_stderr_text.strip() or guest_stderr_text.strip():
            raise RuntimeError(
                "SQLite writers reported diagnostics despite successful exit:\n"
                f"host stderr:\n{host_stderr_text.rstrip()}\n"
                f"L1 stderr:\n{guest_stderr_text.rstrip()}"
            )
    except BaseException as error:
        _request_abort(scratch)
        cleanup_errors = []
        for process, label in (
            (host_process, "host worker"),
            (guest_process, "L1 worker transport"),
        ):
            try:
                _stop_and_reap(process, label)
            except TimeoutError as cleanup_error:
                cleanup_errors.append(str(cleanup_error))
        if cleanup_errors:
            _preserve_test_root(paths, "; ".join(cleanup_errors))
        # Never unlink SQLite/WAL files here. If the Lima shell transport had
        # to be terminated, run-vm-ci's EXIT cleanup stops/deletes the L1 before
        # it removes the temporary root that owns this scratch database.
        diagnostics = []
        for label, process, stdout_path, stderr_path in (
            ("host worker", host_process, host_stdout_path, host_stderr_path),
            (
                "L1 worker transport",
                guest_process,
                guest_stdout_path,
                guest_stderr_path,
            ),
        ):
            if process is not None and (
                process.returncode != 0
                or _read_log(stdout_path).strip()
                or _read_log(stderr_path).strip()
            ):
                diagnostics.append(
                    _process_diagnostic(
                        label, process.returncode, stdout_path, stderr_path
                    )
                )
        diagnostics.extend(cleanup_errors)
        details = "\n".join(diagnostics)
        if details:
            raise RuntimeError(f"{error}\n{details}") from error
        raise
    finally:
        for stream in streams:
            stream.close()


def _stop_l1(paths: DisposablePaths, log_directory: Path) -> None:
    stdout_path = log_directory / "limactl-stop.stdout.log"
    stderr_path = log_directory / "limactl-stop.stderr.log"
    environment = {
        "HOME": str(paths.host_home),
        "LIMA_HOME": str(paths.lima_home),
        "PATH": os.environ.get("PATH", os.defpath),
    }
    stdout_stream = _open_private_log(stdout_path)
    stderr_stream = _open_private_log(stderr_path)
    process: subprocess.Popen[bytes] | None = None
    timed_out = False
    try:
        process = subprocess.Popen(
            ["limactl", "stop", GUEST_INSTANCE],
            stdin=subprocess.DEVNULL,
            stdout=stdout_stream,
            stderr=stderr_stream,
            env=environment,
        )
        try:
            returncode = process.wait(timeout=L1_STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as error:
            timed_out = True
            try:
                _stop_and_reap(process, "L1 stop command")
            except TimeoutError as cleanup_error:
                _preserve_test_root(paths, str(cleanup_error))
                diagnostic = _process_diagnostic(
                    "L1 stop command", process.returncode, stdout_path, stderr_path
                )
                raise RuntimeError(f"{cleanup_error}\n{diagnostic}") from error
            diagnostic = _process_diagnostic(
                "L1 stop command", process.returncode, stdout_path, stderr_path
            )
            raise TimeoutError(
                f"limactl stop timed out after {L1_STOP_TIMEOUT_SECONDS}s\n{diagnostic}"
            ) from error
    except BaseException as error:
        if process is not None and process.poll() is None and not timed_out:
            try:
                _stop_and_reap(process, "L1 stop command")
            except TimeoutError as cleanup_error:
                _preserve_test_root(paths, str(cleanup_error))
                diagnostic = _process_diagnostic(
                    "L1 stop command", process.returncode, stdout_path, stderr_path
                )
                raise RuntimeError(f"{cleanup_error}\n{diagnostic}") from error
        raise
    finally:
        stdout_stream.close()
        stderr_stream.close()

    stdout = _read_log(stdout_path)
    stderr = _read_log(stderr_path)
    if returncode != 0:
        raise RuntimeError(
            _process_diagnostic("L1 stop command", returncode, stdout_path, stderr_path)
        )
    if stderr.strip():
        print(stderr, end="", file=sys.stderr)
    if stdout.strip():
        print(stdout, end="")


def snapshot_test_database(
    paths: DisposablePaths, scratch_name: str, log_directory: Path
) -> ScratchDatabase:
    """Copy stopped guest data into a private host-only verification snapshot."""
    expected_log_directory = paths.test_root / LOG_DIRECTORY_NAME
    if log_directory.is_symlink() or log_directory != expected_log_directory:
        raise SafePathError("SQLite snapshots must use the private host log directory")
    resolved_logs = log_directory.resolve(strict=True)
    if resolved_logs.parent != paths.test_root or resolved_logs.is_relative_to(
        paths.host_home
    ):
        raise SafePathError("SQLite snapshot destination is accessible to the guest")

    data_directory = _host_data_directory(paths.host_home)
    _require_inbound_sentinel(data_directory, "host")
    stopped_scratch = _locate_scratch(data_directory, scratch_name)
    snapshot_directory = resolved_logs / SNAPSHOT_DIRECTORY_NAME
    snapshot_directory.mkdir(mode=0o700, exist_ok=False)
    snapshot_directory.chmod(0o700)
    snapshot_database = snapshot_directory / DATABASE_NAME
    _copy_nofollow_file(stopped_scratch.database, snapshot_database)
    # SQLite rebuilds -shm. Copy the WAL only when it exists; never copy a stale
    # shared-memory file from the guest-writable mount.
    _copy_nofollow_file(
        stopped_scratch.database.with_name(stopped_scratch.database.name + "-wal"),
        snapshot_database.with_name(snapshot_database.name + "-wal"),
        optional=True,
    )
    return ScratchDatabase(
        name=stopped_scratch.name,
        directory=snapshot_directory,
        database=snapshot_database,
    )


def _run_guest_verification(
    scratch: ScratchDatabase, paths: DisposablePaths, log_directory: Path
) -> None:
    command = _guest_command("verify", scratch.name)
    environment = {
        "HOME": str(paths.host_home),
        "LIMA_HOME": str(paths.lima_home),
        "PATH": os.environ.get("PATH", os.defpath),
    }
    stdout_path = log_directory / "l1-verify.stdout.log"
    stderr_path = log_directory / "l1-verify.stderr.log"
    stdout_stream = _open_private_log(stdout_path)
    stderr_stream = _open_private_log(stderr_path)
    process: subprocess.Popen[bytes] | None = None
    timed_out = False
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=stdout_stream,
            stderr=stderr_stream,
            env=environment,
        )
        try:
            returncode = process.wait(timeout=VERIFY_PROCESS_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as error:
            timed_out = True
            try:
                _stop_and_reap(process, "L1 SQLite verification")
            except TimeoutError as cleanup_error:
                _preserve_test_root(paths, str(cleanup_error))
                diagnostic = _process_diagnostic(
                    "L1 SQLite verification",
                    process.returncode,
                    stdout_path,
                    stderr_path,
                )
                raise RuntimeError(f"{cleanup_error}\n{diagnostic}") from error
            diagnostic = _process_diagnostic(
                "L1 SQLite verification",
                process.returncode,
                stdout_path,
                stderr_path,
            )
            raise TimeoutError(
                f"L1 SQLite verification timed out after "
                f"{VERIFY_PROCESS_TIMEOUT_SECONDS}s\n"
                f"{diagnostic}"
            ) from error
    except BaseException as error:
        if process is not None and process.poll() is None and not timed_out:
            try:
                _stop_and_reap(process, "L1 SQLite verification")
            except TimeoutError as cleanup_error:
                _preserve_test_root(paths, str(cleanup_error))
                diagnostic = _process_diagnostic(
                    "L1 SQLite verification",
                    process.returncode,
                    stdout_path,
                    stderr_path,
                )
                raise RuntimeError(f"{cleanup_error}\n{diagnostic}") from error
        raise
    finally:
        stdout_stream.close()
        stderr_stream.close()

    stdout = _read_log(stdout_path)
    stderr = _read_log(stderr_path)
    if returncode != 0:
        raise RuntimeError(
            _process_diagnostic(
                "L1 SQLite verification",
                returncode,
                stdout_path,
                stderr_path,
            )
        )
    if stderr.strip():
        raise RuntimeError(f"L1 SQLite verification stderr:\n{stderr.rstrip()}")
    print(stdout, end="")


def run_verification(side: str, scratch_name: str) -> None:
    if side != "l1":
        raise SafePathError("mounted-path verification is reserved for the L1")
    data_directory = _data_directory_for_side(side)
    _require_inbound_sentinel(data_directory, side)
    scratch = _locate_scratch(data_directory, scratch_name)
    verify_database(scratch.database, scratch.name, side)
    print(
        f"{side} verification: {2 * ROWS_PER_WORKER} exact rows; "
        "journal_mode=wal; integrity_check=ok",
        flush=True,
    )


def orchestrate_validation(scratch_name: str) -> None:
    paths = _paths_from_environment()
    log_directory = _prepare_log_directory(paths)
    scratch = locate_prepared_test_database(paths, scratch_name)
    _run_workers(scratch, paths, log_directory)
    _run_guest_verification(scratch, paths, log_directory)
    # No L1 process remains to replace the guest-writable database path while
    # the final host connection opens it.
    _stop_l1(paths, log_directory)
    snapshot = snapshot_test_database(paths, scratch.name, log_directory)
    verify_database(snapshot.database, snapshot.name, "host snapshot")
    print(
        f"host snapshot verification: {2 * ROWS_PER_WORKER} exact rows; "
        "journal_mode=wal; integrity_check=ok",
        flush=True,
    )

    print(
        "disposable host/L1 SQLite concurrency validation passed "
        f"({2 * ROWS_PER_WORKER} rows, both integrity checks ok)"
    )


def _parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--prepare", action="store_true")
    action.add_argument("--worker", choices=("host", "l1"))
    action.add_argument("--verify", choices=("l1",))
    parser.add_argument("--scratch-name")
    arguments = parser.parse_args(argv)
    if arguments.prepare and arguments.scratch_name:
        parser.error("--scratch-name is not valid with --prepare")
    if (arguments.worker or arguments.verify) and not arguments.scratch_name:
        parser.error("--scratch-name is required for worker and verification modes")
    if not (arguments.prepare or arguments.worker or arguments.verify):
        if not arguments.scratch_name:
            parser.error("--scratch-name is required for host orchestration")
    return arguments


def _interrupt_with_cleanup(signum: int, _frame: object) -> None:
    global _received_signal
    _received_signal = signum
    raise KeyboardInterrupt(f"received signal {signum}")


def main(argv: list[str] | None = None) -> int:
    global _preserve_root_required, _received_signal
    _preserve_root_required = False
    _received_signal = None
    arguments = _parse_arguments(argv)
    if not arguments.prepare and not arguments.worker and not arguments.verify:
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
            signal.signal(signum, _interrupt_with_cleanup)
    try:
        if arguments.prepare:
            paths = _paths_from_environment()
            scratch = prepare_test_database(
                paths.host_home,
                paths.lima_home,
                paths.runner_temp,
            )
            print(scratch.name, flush=True)
        elif arguments.worker:
            run_worker(arguments.worker, arguments.scratch_name)
        elif arguments.verify:
            run_verification(arguments.verify, arguments.scratch_name)
        else:
            orchestrate_validation(arguments.scratch_name)
    except KeyboardInterrupt as error:
        print(f"host/L1 SQLite mount validation interrupted: {error}", file=sys.stderr)
        return (
            PRESERVE_ROOT_EXIT_CODE
            if _preserve_root_required
            else 128 + (_received_signal or signal.SIGINT)
        )
    except Exception as error:
        print(f"host/L1 SQLite mount validation failed: {error}", file=sys.stderr)
        return PRESERVE_ROOT_EXIT_CODE if _preserve_root_required else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
