import os
import stat
import subprocess
import time
from pathlib import Path

import pytest


def _make_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.mark.unit
def test_entrypoint_waits_for_dynamic_subids_before_starting_api(
    repo_root: Path, tmp_path: Path
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    call_log = tmp_path / "podman-calls.log"
    socket_path = tmp_path / "docker.sock"
    marker_path = tmp_path / "subids-ready"

    _make_executable(
        bin_dir / "podman",
        f'''#!/usr/bin/env bash
printf '%s\\n' "$*" >> "{call_log}"
if [[ "$1" == system && "$2" == service ]]; then
    socket_path="${{@: -1}}"
    socket_path="${{socket_path#unix://}}"
    python3 - "$socket_path" <<'PY' &
import socket
import sys
import time

path = sys.argv[1]
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(path)
server.listen(1)
try:
    time.sleep(60)
finally:
    server.close()
PY
    child=$!
    trap 'kill "$child" 2>/dev/null || true
          wait "$child" 2>/dev/null || true
          exit 143' TERM INT
    wait "$child"
fi
''',
    )

    env = {"PATH": f"{bin_dir}:{os.environ.get('PATH', os.defpath)}"}
    env["DOCKER_HOST"] = f"unix://{socket_path}"
    env["DEVBOX_SUBID_READY_FILE"] = str(marker_path)
    env["PODMAN_API_LOG"] = str(tmp_path / "api.log")

    process = subprocess.Popen(
        [str(repo_root / "container" / "devbox-entry.sh")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(0.3)
        assert not call_log.exists()

        marker_path.touch()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not socket_path.exists():
            time.sleep(0.05)

        assert socket_path.exists()
        assert call_log.read_text().strip() == (
            f"system service --time=0 unix://{socket_path}"
        )
    finally:
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
    assert not socket_path.exists()


@pytest.mark.unit
def test_entrypoint_restarts_api_after_initial_service_failure(
    repo_root: Path, tmp_path: Path
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    call_log = tmp_path / "podman-calls.log"
    service_count = tmp_path / "service-count"
    socket_path = tmp_path / "docker.sock"
    marker_path = tmp_path / "subids-ready"
    marker_path.touch()

    _make_executable(
        bin_dir / "podman",
        f'''#!/usr/bin/env bash
printf '%s\\n' "$*" >> "{call_log}"
if [[ "$1" == system && "$2" == service ]]; then
    count=0
    if [[ -f "{service_count}" ]]; then
        count=$(<"{service_count}")
    fi
    count=$((count + 1))
    printf '%s\\n' "$count" > "{service_count}"
    if [[ "$count" -eq 1 ]]; then
        exit 0
    fi
    socket_path="${{@: -1}}"
    socket_path="${{socket_path#unix://}}"
    python3 - "$socket_path" <<'PY' &
import socket
import sys
import time

server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(sys.argv[1])
server.listen(1)
try:
    time.sleep(60)
finally:
    server.close()
PY
    child=$!
    trap 'kill "$child" 2>/dev/null || true
          wait "$child" 2>/dev/null || true
          exit 143' TERM INT
    wait "$child"
fi
''',
    )
    _make_executable(
        bin_dir / "curl",
        f'''#!/usr/bin/env bash
if [[ "$*" == *_ping* ]]; then
    count=$(<"{service_count}")
    if [[ "$count" -ge 2 ]]; then
        printf 'OK\\n'
        exit 0
    fi
    exit 7
fi
exit 0
''',
    )

    env = {"PATH": f"{bin_dir}:{os.environ.get('PATH', os.defpath)}"}
    env["DOCKER_HOST"] = f"unix://{socket_path}"
    env["DEVBOX_SUBID_READY_FILE"] = str(marker_path)
    env["PODMAN_API_LOG"] = str(tmp_path / "api.log")

    process = subprocess.Popen(
        [str(repo_root / "container" / "devbox-entry.sh")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (
                service_count.exists()
                and int(service_count.read_text()) >= 2
                and socket_path.exists()
            ):
                break
            time.sleep(0.05)

        assert service_count.read_text().strip() == "2"
        assert socket_path.exists()
        assert call_log.read_text().splitlines()[:2] == [
            f"system service --time=0 unix://{socket_path}",
            f"system service --time=0 unix://{socket_path}",
        ]
    finally:
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
    assert not socket_path.exists()
