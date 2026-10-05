import re
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

import pytest

from scripts import validate_host_sqlite_mount as validation


def make_disposable_paths(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    runner_temp = tmp_path / "runner-temp"
    test_root = runner_temp / "my-sandbox-vm-test.ABC123"
    host_home = test_root / "home"
    lima_home = test_root / "lima"
    data_directory = host_home / ".local/share/opencode"
    data_directory.mkdir(parents=True, mode=0o700)
    host_home.chmod(0o700)
    test_root.chmod(0o700)
    lima_home.mkdir(mode=0o700)
    (data_directory / validation.INBOUND_SENTINEL_NAME).write_text(
        validation.INBOUND_SENTINEL_CONTENT,
        encoding="utf-8",
    )
    return runner_temp, host_home, lima_home, data_directory


class SyntheticSQLiteError(sqlite3.OperationalError):
    def __init__(self, code: int, name: str):
        super().__init__("synthetic SQLite failure")
        self.sqlite_errorcode = code
        self.sqlite_errorname = name


class TransactionConnection:
    def __init__(
        self,
        fail_operation: str,
        error: sqlite3.Error,
        failures: int,
        *,
        rollback_error: sqlite3.Error | None = None,
    ):
        self.fail_operation = fail_operation
        self.error = error
        self.failures = failures
        self.rollback_error = rollback_error
        self.statements: list[str] = []
        self._in_transaction = False

    @property
    def in_transaction(self) -> bool:
        return self._in_transaction

    def execute(self, operation: str, _parameters: Any = None) -> None:
        self.statements.append(operation)
        normalized_operation = (
            "INSERT" if operation.startswith("INSERT ") else operation
        )
        if normalized_operation == self.fail_operation and self.failures:
            self.failures -= 1
            raise self.error
        if operation == "ROLLBACK" and self.rollback_error is not None:
            raise self.rollback_error
        if operation == "BEGIN IMMEDIATE":
            self._in_transaction = True
        elif operation in ("COMMIT", "ROLLBACK"):
            self._in_transaction = False


@pytest.mark.unit
def test_disposable_path_validation_rejects_a_real_home_layout(tmp_path: Path):
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    lima_home = runner_temp / "my-sandbox-vm-test.ABC123/lima"
    lima_home.mkdir(parents=True)

    with pytest.raises(validation.SafePathError, match="home/lima siblings"):
        validation.validate_disposable_paths(real_home, lima_home, runner_temp)

    assert list(real_home.iterdir()) == []


@pytest.mark.unit
def test_l1_worker_requires_the_explicit_runner_marker(tmp_path: Path, monkeypatch):
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.delenv(validation.L1_TEST_ENVIRONMENT, raising=False)

    with pytest.raises(
        validation.SafePathError, match="explicit disposable-test marker"
    ):
        validation._data_directory_for_side("l1")

    assert list(real_home.iterdir()) == []


@pytest.mark.unit
def test_database_setup_rejects_a_symlinked_opencode_data_directory(tmp_path: Path):
    runner_temp, host_home, lima_home, data_directory = make_disposable_paths(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (data_directory / validation.INBOUND_SENTINEL_NAME).unlink()
    data_directory.rmdir()
    data_directory.symlink_to(outside, target_is_directory=True)

    with pytest.raises(validation.SafePathError, match="symlinked temporary OpenCode"):
        validation.prepare_test_database(host_home, lima_home, runner_temp)

    assert list(outside.iterdir()) == []


@pytest.mark.unit
def test_prepare_cli_prints_only_the_validated_scratch_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    runner_temp, host_home, lima_home, data_directory = make_disposable_paths(tmp_path)
    monkeypatch.setenv("HOME", str(host_home))
    monkeypatch.setenv("LIMA_HOME", str(lima_home))
    monkeypatch.setenv("RUNNER_TEMP", str(runner_temp))

    assert validation.main(["--prepare"]) == 0

    captured = capsys.readouterr()
    scratch_name = captured.out.strip()
    assert validation._scratch_name_is_safe(scratch_name)
    assert captured.out == f"{scratch_name}\n"
    assert not captured.err
    assert (data_directory / scratch_name / validation.DATABASE_NAME).is_file()
    paths = validation.validate_disposable_paths(host_home, lima_home, runner_temp)
    assert (
        validation.locate_prepared_test_database(paths, scratch_name).name
        == scratch_name
    )


@pytest.mark.unit
def test_no_argument_cli_fails_closed_without_a_prepared_scratch_name(capsys):
    with pytest.raises(SystemExit) as error:
        validation.main([])

    assert error.value.code == 2
    assert (
        "--scratch-name is required for host orchestration" in capsys.readouterr().err
    )


@pytest.mark.unit
def test_coordinator_lookup_never_creates_an_unprepared_database(tmp_path: Path):
    runner_temp, host_home, lima_home, data_directory = make_disposable_paths(tmp_path)
    paths = validation.validate_disposable_paths(host_home, lima_home, runner_temp)
    scratch_name = f"{validation.SCRATCH_PREFIX}{'9' * 32}"

    with pytest.raises(RuntimeError, match="scratch directory is missing"):
        validation.locate_prepared_test_database(paths, scratch_name)

    assert {path.name for path in data_directory.iterdir()} == {
        validation.INBOUND_SENTINEL_NAME
    }


@pytest.mark.unit
def test_test_database_is_wal_private_and_has_only_a_test_schema(tmp_path: Path):
    runner_temp, host_home, lima_home, data_directory = make_disposable_paths(tmp_path)

    scratch = validation.prepare_test_database(host_home, lima_home, runner_temp)

    assert scratch.directory.parent == data_directory
    assert scratch.name.startswith(validation.SCRATCH_PREFIX)
    assert scratch.database.name == validation.DATABASE_NAME
    assert stat.S_IMODE(scratch.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(scratch.database.stat().st_mode) == 0o600
    assert (scratch.directory / validation.MANIFEST_NAME).read_text() == (
        validation._manifest_content(scratch.name)
    )
    assert not (data_directory / "opencode.db").exists()
    connection = sqlite3.connect(scratch.database)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall() == [(validation.TABLE_NAME,)]
        assert connection.execute("PRAGMA user_version").fetchone() == (287,)
    finally:
        connection.close()


@pytest.mark.unit
def test_mode_rw_refuses_to_create_a_missing_database(tmp_path: Path):
    missing_database = tmp_path / "missing-test-only.sqlite3"

    assert validation.sqlite_rw_uri(missing_database).endswith("?mode=rw")
    with pytest.raises(sqlite3.OperationalError):
        validation.connect_existing_database(missing_database)

    assert list(tmp_path.iterdir()) == []


@pytest.mark.unit
def test_ordinary_transaction_retries_busy_insert_and_restarts_transaction(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    connection = TransactionConnection(
        "INSERT",
        SyntheticSQLiteError(sqlite3.SQLITE_BUSY, "SQLITE_BUSY"),
        failures=1,
    )
    delays = []
    monkeypatch.setattr(validation.time, "sleep", delays.append)

    validation._run_ordinary_transaction(
        cast(sqlite3.Connection, connection),
        "run",
        "l1",
        0,
        "l1",
        "post-contention",
    )

    insert_statement = (
        f"INSERT INTO {validation.TABLE_NAME} "
        "(run_id, writer, sequence, payload) VALUES (?, ?, ?, ?)"
    )
    assert connection.statements == [
        "BEGIN IMMEDIATE",
        insert_statement,
        "ROLLBACK",
        "BEGIN IMMEDIATE",
        insert_statement,
        "COMMIT",
    ]
    assert delays == [validation.ORDINARY_TRANSACTION_RETRY_DELAY_SECONDS]
    retry_output = capsys.readouterr().out
    assert "l1 worker phase=post-contention transaction=0" in retry_output
    assert "SQL operation=INSERT" in retry_output
    assert f"sqlite_errorcode={sqlite3.SQLITE_BUSY}" in retry_output
    assert "sqlite_errorname='SQLITE_BUSY'" in retry_output


@pytest.mark.unit
def test_ordinary_transaction_retries_busy_begin_without_rollback(
    monkeypatch: pytest.MonkeyPatch,
):
    connection = TransactionConnection(
        "BEGIN IMMEDIATE",
        SyntheticSQLiteError(sqlite3.SQLITE_BUSY, "SQLITE_BUSY"),
        failures=1,
    )
    delays = []
    monkeypatch.setattr(validation.time, "sleep", delays.append)

    validation._run_ordinary_transaction(
        cast(sqlite3.Connection, connection),
        "run",
        "host",
        1,
        "host",
        "concurrent-writes",
    )

    assert connection.statements == [
        "BEGIN IMMEDIATE",
        "BEGIN IMMEDIATE",
        f"INSERT INTO {validation.TABLE_NAME} "
        "(run_id, writer, sequence, payload) VALUES (?, ?, ?, ?)",
        "COMMIT",
    ]
    assert delays == [validation.ORDINARY_TRANSACTION_RETRY_DELAY_SECONDS]


@pytest.mark.unit
def test_insert_failure_includes_worker_sqlite_context():
    error = SyntheticSQLiteError(sqlite3.SQLITE_CONSTRAINT, "SQLITE_CONSTRAINT")
    connection = TransactionConnection("INSERT", error, failures=1)

    with pytest.raises(RuntimeError) as failure:
        validation._insert_test_row(
            cast(sqlite3.Connection, connection),
            "run",
            "host",
            3,
            side="host",
            phase="concurrent-writes",
        )

    message = str(failure.value)
    assert "host worker phase=concurrent-writes transaction=3" in message
    assert "SQL operation=INSERT" in message
    assert f"sqlite_errorcode={sqlite3.SQLITE_CONSTRAINT}" in message
    assert "sqlite_errorname='SQLITE_CONSTRAINT'" in message


@pytest.mark.unit
def test_ordinary_transaction_busy_exhaustion_reports_context_and_sqlite_error(
    monkeypatch: pytest.MonkeyPatch,
):
    error = SyntheticSQLiteError(sqlite3.SQLITE_BUSY, "SQLITE_BUSY")
    connection = TransactionConnection("INSERT", error, failures=10)
    monkeypatch.setattr(validation.time, "sleep", lambda _delay: None)

    with pytest.raises(RuntimeError) as failure:
        validation._run_ordinary_transaction(
            cast(sqlite3.Connection, connection),
            "run",
            "l1",
            0,
            "l1",
            "post-contention",
        )

    message = str(failure.value)
    assert "l1 worker phase=post-contention transaction=0" in message
    assert "SQL operation=INSERT" in message
    assert f"sqlite_errorcode={sqlite3.SQLITE_BUSY}" in message
    assert "sqlite_errorname='SQLITE_BUSY'" in message
    assert "attempt 5/5" in message
    assert connection.statements.count("BEGIN IMMEDIATE") == 5
    assert (
        sum(statement.startswith("INSERT ") for statement in connection.statements) == 5
    )
    assert connection.statements.count("ROLLBACK") == 5


@pytest.mark.unit
def test_ordinary_transaction_retry_budget_failure_reports_last_sqlite_error(
    monkeypatch: pytest.MonkeyPatch,
):
    error = SyntheticSQLiteError(sqlite3.SQLITE_BUSY, "SQLITE_BUSY")
    connection = TransactionConnection("BEGIN IMMEDIATE", error, failures=1)
    monotonic_values = iter((1.0,))
    monkeypatch.setattr(validation.time, "monotonic", lambda: next(monotonic_values))

    with pytest.raises(RuntimeError) as failure:
        validation._run_ordinary_transaction(
            cast(sqlite3.Connection, connection),
            "run",
            "l1",
            0,
            "l1",
            "post-contention",
            retry_deadline=0.5,
        )

    message = str(failure.value)
    assert "l1 worker phase=post-contention transaction=0" in message
    assert "SQL operation=BEGIN IMMEDIATE" in message
    assert f"sqlite_errorcode={sqlite3.SQLITE_BUSY}" in message
    assert "ordinary-write retry budget exhausted before next retry" in message
    assert connection.statements == ["BEGIN IMMEDIATE"]


@pytest.mark.unit
def test_ordinary_transaction_does_not_fail_slow_success_after_retry_deadline(
    monkeypatch: pytest.MonkeyPatch,
):
    connection = TransactionConnection(
        "BEGIN IMMEDIATE",
        SyntheticSQLiteError(sqlite3.SQLITE_BUSY, "SQLITE_BUSY"),
        failures=0,
    )
    monkeypatch.setattr(validation.time, "monotonic", lambda: 1.0)

    validation._run_ordinary_transaction(
        cast(sqlite3.Connection, connection),
        "run",
        "host",
        1,
        "host",
        "concurrent-writes",
        retry_deadline=0.5,
    )

    assert connection.statements[0] == "BEGIN IMMEDIATE"
    assert connection.statements[-1] == "COMMIT"


@pytest.mark.unit
def test_ordinary_transaction_does_not_retry_non_busy_sqlite_errors(
    monkeypatch: pytest.MonkeyPatch,
):
    error = SyntheticSQLiteError(sqlite3.SQLITE_LOCKED, "SQLITE_LOCKED")
    connection = TransactionConnection("BEGIN IMMEDIATE", error, failures=1)
    delays = []
    monkeypatch.setattr(validation.time, "sleep", delays.append)

    with pytest.raises(RuntimeError, match="SQL operation=BEGIN IMMEDIATE"):
        validation._run_ordinary_transaction(
            cast(sqlite3.Connection, connection),
            "run",
            "host",
            3,
            "host",
            "concurrent-writes",
        )

    assert connection.statements == ["BEGIN IMMEDIATE"]
    assert not delays


@pytest.mark.unit
def test_ordinary_transaction_retries_busy_commit(
    monkeypatch: pytest.MonkeyPatch,
):
    connection = TransactionConnection(
        "COMMIT",
        SyntheticSQLiteError(sqlite3.SQLITE_BUSY, "SQLITE_BUSY"),
        failures=1,
    )
    monkeypatch.setattr(validation.time, "sleep", lambda _delay: None)

    validation._run_ordinary_transaction(
        cast(sqlite3.Connection, connection),
        "run",
        "host",
        2,
        "host",
        "concurrent-writes",
    )

    insert_statement = (
        f"INSERT INTO {validation.TABLE_NAME} "
        "(run_id, writer, sequence, payload) VALUES (?, ?, ?, ?)"
    )
    assert connection.statements == [
        "BEGIN IMMEDIATE",
        insert_statement,
        "COMMIT",
        "ROLLBACK",
        "BEGIN IMMEDIATE",
        insert_statement,
        "COMMIT",
    ]


@pytest.mark.unit
def test_ordinary_transaction_reports_rollback_failure_with_original_error():
    busy_error = SyntheticSQLiteError(sqlite3.SQLITE_BUSY, "SQLITE_BUSY")
    rollback_error = SyntheticSQLiteError(sqlite3.SQLITE_IOERR, "SQLITE_IOERR")
    connection = TransactionConnection(
        "INSERT", busy_error, failures=1, rollback_error=rollback_error
    )

    with pytest.raises(RuntimeError) as failure:
        validation._run_ordinary_transaction(
            cast(sqlite3.Connection, connection),
            "run",
            "host",
            4,
            "host",
            "concurrent-writes",
        )

    message = str(failure.value)
    assert connection.statements == [
        "BEGIN IMMEDIATE",
        f"INSERT INTO {validation.TABLE_NAME} "
        "(run_id, writer, sequence, payload) VALUES (?, ?, ?, ?)",
        "ROLLBACK",
    ]
    assert "SQL operation=ROLLBACK" in message
    assert f"sqlite_errorcode={sqlite3.SQLITE_IOERR}" in message
    assert "; while recovering from host worker phase=concurrent-writes" in message
    assert "SQL operation=INSERT" in message
    assert f"sqlite_errorcode={sqlite3.SQLITE_BUSY}" in message
    assert failure.value.__cause__ is rollback_error


@pytest.mark.unit
def test_existing_barrier_marker_must_have_exact_contents(tmp_path: Path):
    runner_temp, host_home, lima_home, _ = make_disposable_paths(tmp_path)
    scratch = validation.prepare_test_database(host_home, lima_home, runner_temp)
    marker = scratch.directory / "lock-held"
    marker.write_text("unexpected marker\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unexpected contents"):
        validation._write_barrier_file(
            scratch, "lock-held", "host holds BEGIN IMMEDIATE\n"
        )

    assert marker.read_text(encoding="utf-8") == "unexpected marker\n"


@pytest.mark.unit
def test_host_logs_are_private_and_no_follow_reads_reject_symlinks(tmp_path: Path):
    runner_temp, host_home, lima_home, _ = make_disposable_paths(tmp_path)
    paths = validation.validate_disposable_paths(host_home, lima_home, runner_temp)
    scratch = validation.prepare_test_database(host_home, lima_home, runner_temp)
    log_directory = validation._prepare_log_directory(paths)
    external_file = tmp_path / "external-file"
    external_file.write_text("must not be followed\n", encoding="utf-8")
    log_link = log_directory / "host-worker.stdout.log"
    marker_link = scratch.directory / "l1-contended"
    log_link.symlink_to(external_file)
    marker_link.symlink_to(external_file)

    assert log_directory.parent == paths.test_root
    assert not log_directory.is_relative_to(host_home)
    assert stat.S_IMODE(log_directory.stat().st_mode) == 0o700
    with pytest.raises(RuntimeError, match="no-follow"):
        validation._read_log(log_link)
    with pytest.raises(RuntimeError, match="no-follow"):
        validation._marker_matches(
            scratch,
            "l1-contended",
            "L1 observed SQLITE_BUSY while host lock was held\n",
        )
    assert external_file.read_text(encoding="utf-8") == "must not be followed\n"


@pytest.mark.unit
@pytest.mark.parametrize("sidecar", ("database", "wal"))
def test_host_snapshot_rejects_symlinked_database_or_wal(tmp_path: Path, sidecar: str):
    runner_temp, host_home, lima_home, _ = make_disposable_paths(tmp_path)
    paths = validation.validate_disposable_paths(host_home, lima_home, runner_temp)
    scratch = validation.prepare_test_database(host_home, lima_home, runner_temp)
    log_directory = validation._prepare_log_directory(paths)
    outside_file = tmp_path / "outside-db-file"
    outside_file.write_text("preserve external content\n", encoding="utf-8")
    source = (
        scratch.database
        if sidecar == "database"
        else scratch.database.with_name(scratch.database.name + "-wal")
    )
    if sidecar == "database":
        source.unlink()
    source.symlink_to(outside_file)

    with pytest.raises(RuntimeError, match="symlink|no-follow"):
        validation.snapshot_test_database(paths, scratch.name, log_directory)

    assert outside_file.read_text(encoding="utf-8") == "preserve external content\n"


@pytest.mark.unit
def test_host_snapshot_preserves_committed_rows_from_wal(tmp_path: Path):
    runner_temp, host_home, lima_home, _ = make_disposable_paths(tmp_path)
    paths = validation.validate_disposable_paths(host_home, lima_home, runner_temp)
    scratch = validation.prepare_test_database(host_home, lima_home, runner_temp)
    log_directory = validation._prepare_log_directory(paths)
    connection = sqlite3.connect(scratch.database, isolation_level=None)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("BEGIN IMMEDIATE")
        connection.executemany(
            f"INSERT INTO {validation.TABLE_NAME} "
            "(run_id, writer, sequence, payload) VALUES (?, ?, ?, ?)",
            validation._expected_rows(scratch.name),
        )
        connection.execute("COMMIT")

        source_wal = scratch.database.with_name(scratch.database.name + "-wal")
        assert source_wal.is_file() and source_wal.stat().st_size > 0

        snapshot = validation.snapshot_test_database(paths, scratch.name, log_directory)

        snapshot_wal = snapshot.database.with_name(snapshot.database.name + "-wal")
        snapshot_shm = snapshot.database.with_name(snapshot.database.name + "-shm")
        assert snapshot_wal.is_file() and snapshot_wal.stat().st_size > 0
        assert not snapshot_shm.exists()
        validation.verify_database(snapshot.database, scratch.name, "host snapshot")
    finally:
        connection.close()


@pytest.mark.unit
@pytest.mark.parametrize(
    "scratch_name",
    (
        "../outside",
        f"{validation.SCRATCH_PREFIX}{'a' * 32};touch /tmp/not-a-test",
        f"{validation.SCRATCH_PREFIX}{'b' * 32}$(id)",
    ),
)
def test_guest_command_rejects_unsafe_scratch_names_before_construction(
    scratch_name: str, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        validation,
        "Path",
        lambda *_args, **_kwargs: pytest.fail("guest command construction started"),
    )

    with pytest.raises(validation.SafePathError, match="invalid test-only"):
        validation._guest_command("worker", scratch_name)


@pytest.mark.unit
def test_vm_runner_stages_host_helper_outside_guest_mounts():
    repo_root = Path(__file__).resolve().parents[2]
    runner = (repo_root / "scripts/run-vm-ci.sh").read_text(encoding="utf-8")
    helper_source = (repo_root / "scripts/validate_host_sqlite_mount.py").read_text(
        encoding="utf-8"
    )
    guest_argv = validation._guest_command(
        "worker", f"{validation.SCRATCH_PREFIX}{'d' * 32}"
    )

    archive_at = runner.index('git -C "$workspace" archive --format=tar HEAD')
    stage_command = (
        "install -m 0600 \\\n"
        '    "$repo_copy/scripts/validate_host_sqlite_mount.py" "$host_sqlite_helper"'
    )
    stage_at = runner.index(stage_command)
    lima_home_create_at = runner.index('mkdir -p "$lima_home"')
    lima_home_chmod_at = runner.index('chmod 700 "$lima_home"')
    prepare_at = runner.index(
        'if sqlite_scratch_name="$(timeout --signal=TERM --kill-after=5s 30s \\\n'
        '    python3 -I "$host_sqlite_helper" --prepare)"; then'
    )
    host_start_at = runner.index("limactl start \\\n  --yes")
    host_invoke_at = runner.index(
        'python3 -I "$host_sqlite_helper" --scratch-name "$sqlite_scratch_name"'
    )
    helper_done_at = runner.index("sqlite_helper_started=false", host_invoke_at)
    guest_restart_at = runner.index("limactl start --yes --timeout 60m devbox")
    suite_at = runner.index("# Run the test wrapper directly in the guest.")

    assert 'host_sqlite_helper="$test_root/validate_host_sqlite_mount.py"' in runner
    assert (
        archive_at
        < stage_at
        < lima_home_create_at
        < lima_home_chmod_at
        < prepare_at
        < host_start_at
    )
    assert host_start_at < host_invoke_at < helper_done_at < guest_restart_at < suite_at
    assert 'python3 -I "$repo_copy/scripts/validate_host_sqlite_mount.py"' not in runner
    for signal_name, status in (
        ("INT", 130),
        ("TERM", 143),
        ("HUP", 129),
        ("QUIT", 131),
    ):
        assert f"trap 'signal_exit {signal_name} {status}' {signal_name}" in runner
    assert '[[ "$helper_status" -eq 124' in runner
    assert '[[ "$helper_status" -ge 128' in runner
    assert "str(Path(__file__).resolve())" in helper_source
    assert (
        f"{validation.GUEST_REPO}/scripts/validate_host_sqlite_mount.py" in guest_argv
    )


@pytest.mark.unit
def test_unreaped_worker_requires_preservation_even_if_marker_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    runner_temp, host_home, lima_home, _ = make_disposable_paths(tmp_path)
    paths = validation.validate_disposable_paths(host_home, lima_home, runner_temp)

    def fail_to_write_marker(_path: Path, _content: str) -> None:
        raise OSError("simulated full temporary filesystem")

    def fail_to_reap_worker(_scratch_name: str) -> None:
        validation._preserve_test_root(paths, "worker could not be reaped")
        raise RuntimeError("worker could not be reaped")

    monkeypatch.setattr(validation, "_write_private_file", fail_to_write_marker)
    monkeypatch.setattr(validation, "orchestrate_validation", fail_to_reap_worker)
    monkeypatch.setattr(validation.signal, "signal", lambda *_args: None)

    assert (
        validation.main(["--scratch-name", f"{validation.SCRATCH_PREFIX}{'e' * 32}"])
        == validation.PRESERVE_ROOT_EXIT_CODE
    )
    assert not (paths.test_root / validation.PRESERVE_ROOT_MARKER).exists()


@pytest.mark.unit
@pytest.mark.parametrize("signum", (signal.SIGHUP, signal.SIGQUIT))
def test_coordinator_signal_exits_with_signal_status(
    signum: int, monkeypatch: pytest.MonkeyPatch
):
    handlers = {}
    monkeypatch.setattr(
        validation.signal,
        "signal",
        lambda received, handler: handlers.__setitem__(received, handler),
    )

    def interrupt_orchestration(_scratch_name: str) -> None:
        handlers[signum](signum, None)

    monkeypatch.setattr(validation, "orchestrate_validation", interrupt_orchestration)

    assert (
        validation.main(["--scratch-name", f"{validation.SCRATCH_PREFIX}{'f' * 32}"])
        == 128 + signum
    )


@pytest.mark.unit
@pytest.mark.parametrize("stop_fails", (False, True))
def test_host_verification_waits_for_successful_l1_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop_fails: bool
):
    runner_temp, host_home, lima_home, data_directory = make_disposable_paths(tmp_path)
    paths = validation.validate_disposable_paths(host_home, lima_home, runner_temp)
    scratch = validation.ScratchDatabase(
        f"{validation.SCRATCH_PREFIX}{'c' * 32}",
        data_directory / f"{validation.SCRATCH_PREFIX}{'c' * 32}",
        data_directory / validation.DATABASE_NAME,
    )
    events = []

    monkeypatch.setattr(validation, "_paths_from_environment", lambda: paths)
    monkeypatch.setattr(
        validation, "_prepare_log_directory", lambda _paths: paths.test_root
    )
    monkeypatch.setattr(
        validation,
        "locate_prepared_test_database",
        lambda *_args: scratch,
    )
    monkeypatch.setattr(
        validation, "_run_workers", lambda *_args: events.append("workers")
    )
    monkeypatch.setattr(
        validation,
        "_run_guest_verification",
        lambda *_args: events.append("guest verification"),
    )

    def stop_l1(*_args) -> None:
        events.append("stop L1")
        if stop_fails:
            raise RuntimeError("L1 stop failed")

    monkeypatch.setattr(validation, "_stop_l1", stop_l1)
    monkeypatch.setattr(
        validation,
        "snapshot_test_database",
        lambda *_args: (events.append("snapshot"), scratch)[1],
    )
    monkeypatch.setattr(
        validation,
        "verify_database",
        lambda _database, _name, observer: events.append(f"{observer} verification"),
    )

    if stop_fails:
        with pytest.raises(RuntimeError, match="L1 stop failed"):
            validation.orchestrate_validation(scratch.name)
        assert events == ["workers", "guest verification", "stop L1"]
    else:
        validation.orchestrate_validation(scratch.name)
        assert events == [
            "workers",
            "guest verification",
            "stop L1",
            "snapshot",
            "host snapshot verification",
        ]


@pytest.mark.unit
def test_host_and_l1_workers_contend_and_verify_private_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    runner_temp, host_home, lima_home, _ = make_disposable_paths(tmp_path)
    scratch = validation.prepare_test_database(host_home, lima_home, runner_temp)
    paths = validation.validate_disposable_paths(host_home, lima_home, runner_temp)
    log_directory = validation._prepare_log_directory(paths)
    helper = Path(validation.__file__).resolve()
    worker_environment = {
        "HOME": str(host_home),
        "LIMA_HOME": str(lima_home),
        "RUNNER_TEMP": str(runner_temp),
        validation.L1_TEST_ENVIRONMENT: "1",
    }
    processes: list[subprocess.Popen[bytes]] = []
    streams = []
    log_paths = {}
    try:
        for side in ("host",):
            stdout_path = log_directory / f"unit-{side}.stdout.log"
            stderr_path = log_directory / f"unit-{side}.stderr.log"
            stdout_stream = validation._open_private_log(stdout_path)
            stderr_stream = validation._open_private_log(stderr_path)
            streams.extend((stdout_stream, stderr_stream))
            log_paths[side] = (stdout_path, stderr_path)
            processes.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-I",
                        str(helper),
                        "--worker",
                        side,
                        "--scratch-name",
                        scratch.name,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_stream,
                    stderr=stderr_stream,
                    env=worker_environment,
                )
            )
        try:
            validation._wait_for_host_lock_held(
                processes[0],
                scratch,
                time.monotonic() + validation.WORKER_WAIT_TIMEOUT_SECONDS,
            )
        except (RuntimeError, TimeoutError) as error:
            stdout_path, stderr_path = log_paths["host"]
            stdout = validation._read_log(stdout_path)
            stderr = validation._read_log(stderr_path)
            raise AssertionError(
                f"host worker failed before publishing lock-held: {error}\n"
                f"stdout:\n{stdout}\nstderr:\n{stderr}"
            ) from error
        side = "l1"
        stdout_path = log_directory / f"unit-{side}.stdout.log"
        stderr_path = log_directory / f"unit-{side}.stderr.log"
        stdout_stream = validation._open_private_log(stdout_path)
        stderr_stream = validation._open_private_log(stderr_path)
        streams.extend((stdout_stream, stderr_stream))
        log_paths[side] = (stdout_path, stderr_path)
        processes.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    str(helper),
                    "--worker",
                    side,
                    "--scratch-name",
                    scratch.name,
                ],
                stdin=subprocess.DEVNULL,
                stdout=stdout_stream,
                stderr=stderr_stream,
                env=worker_environment,
            )
        )

        for side, process in zip(("host", "l1"), processes, strict=True):
            try:
                process.wait(timeout=validation.WORKER_WAIT_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired as error:
                stdout_path, stderr_path = log_paths[side]
                stdout = validation._read_log(stdout_path)
                stderr = validation._read_log(stderr_path)
                raise AssertionError(
                    f"{side} worker timed out after "
                    f"{validation.WORKER_WAIT_TIMEOUT_SECONDS}s\n"
                    f"stdout:\n{stdout}\nstderr:\n{stderr}"
                ) from error
        output_by_side = {}
        for side, process in zip(("host", "l1"), processes, strict=True):
            stdout_path, stderr_path = log_paths[side]
            stdout = validation._read_log(stdout_path)
            stderr = validation._read_log(stderr_path)
            assert process.returncode == 0, (
                f"{side} worker exited with {process.returncode}\n"
                f"stdout:\n{stdout}\nstderr:\n{stderr}"
            )
            assert "committed 10 bounded transactions" in stdout, (
                f"{side} worker did not report all transactions:\n{stdout}"
            )
            assert not stderr, f"{side} worker wrote to stderr:\n{stderr}"
            output_by_side[side] = stdout

        contention = re.search(
            r"observed SQLITE_BUSY \(code (\d+)\) after ([0-9.]+)s",
            output_by_side["l1"],
        )
        assert contention, (
            f"L1 intentional contention diagnostic missing: {output_by_side['l1']}"
        )
        assert int(contention.group(1)) == sqlite3.SQLITE_BUSY, (
            f"L1 intentional contention returned the wrong SQLite code: "
            f"{output_by_side['l1']}"
        )
        assert (
            validation.MIN_CONTENTION_WAIT_SECONDS
            <= float(contention.group(2))
            <= validation.MAX_CONTENTION_WAIT_SECONDS
        ), (
            "L1 intentional SQLITE_BUSY timing was outside bounds: "
            f"{output_by_side['l1']}"
        )

        monkeypatch.setenv("HOME", str(host_home))
        monkeypatch.setenv(validation.L1_TEST_ENVIRONMENT, "1")
        validation.run_verification("l1", scratch.name)
        source_shm = scratch.database.with_name(scratch.database.name + "-shm")
        source_shm.write_text(
            "do not copy guest shared-memory state\n", encoding="utf-8"
        )
        snapshot = validation.snapshot_test_database(paths, scratch.name, log_directory)
        assert snapshot.directory.parent == log_directory
        assert stat.S_IMODE(snapshot.directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(snapshot.database.stat().st_mode) == 0o600
        assert not snapshot.database.with_name(snapshot.database.name + "-shm").exists()
        validation.verify_database(snapshot.database, scratch.name, "host snapshot")
        assert (scratch.directory / "lock-held").read_text() == (
            "host holds BEGIN IMMEDIATE\n"
        )
        assert (scratch.directory / "l1-attempting").read_text() == (
            "L1 is attempting BEGIN IMMEDIATE\n"
        )
        assert (scratch.directory / "l1-contended").read_text() == (
            "L1 observed SQLITE_BUSY while host lock was held\n"
        )
        assert (scratch.directory / "lock-released").read_text() == (
            "host committed lock-holder transaction\n"
        )
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
        for stream in streams:
            stream.close()


@pytest.mark.unit
def test_lock_holder_timeout_reports_missing_l1_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    runner_temp, host_home, lima_home, _ = make_disposable_paths(tmp_path)
    scratch = validation.prepare_test_database(host_home, lima_home, runner_temp)
    monkeypatch.setenv("HOME", str(host_home))
    monkeypatch.setenv("LIMA_HOME", str(lima_home))
    monkeypatch.setenv("RUNNER_TEMP", str(runner_temp))
    monkeypatch.setattr(validation, "CONTENTION_TIMEOUT_SECONDS", 0.05)

    with pytest.raises(TimeoutError, match="l1-attempting"):
        validation.run_worker("host", scratch.name)


@pytest.mark.unit
def test_verification_rejects_missing_expected_rows(tmp_path: Path):
    runner_temp, host_home, lima_home, _ = make_disposable_paths(tmp_path)
    scratch = validation.prepare_test_database(host_home, lima_home, runner_temp)

    with pytest.raises(RuntimeError, match="expected 20 exact rows"):
        validation.verify_database(scratch.database, scratch.name, "host-test")
