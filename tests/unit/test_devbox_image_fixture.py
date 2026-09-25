"""Bounded devbox image build (issue #252).

The `devbox_image` fixture's Podman invocations must run bounded and in a
dedicated process group: a wedged nested `podman build` -- observed inside
devboxes as a fuse-overlayfs spin that ignores SIGTERM -- must fail the
session with a loud diagnostic instead of hanging it, and its whole process
tree must actually be terminated.
"""

import os
import shlex
from pathlib import Path

import pytest

from tests.conftest import (
    DEFAULT_IMAGE_BUILD_TIMEOUT,
    _wait_pid_gone,
    devbox_context_fingerprint,
    ensure_devbox_image,
    image_build_timeout,
)


def _fake_podman_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    return bin_dir


def _write_fake_podman(bin_dir: Path, body: str) -> None:
    podman = bin_dir / "podman"
    podman.write_text(f"#!/usr/bin/env bash\n{body}")
    podman.chmod(0o700)


def _dockerfile_context(tmp_path: Path) -> Path:
    context = tmp_path / "context"
    context.mkdir(exist_ok=True)
    (context / "Dockerfile").write_text("FROM scratch\n")
    return context


def _use_fake_podman(monkeypatch: pytest.MonkeyPatch, bin_dir: Path) -> None:
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")


@pytest.mark.unit
def test_existing_image_short_circuits_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_marker = tmp_path / "build-ran"
    _write_fake_podman(
        _fake_podman_bin(tmp_path),
        f"""
if [[ "$1 $2" == "image exists" ]]; then
  exit 0
fi
if [[ "$1" == "build" ]]; then
  touch {shlex.quote(str(build_marker))}
  exit 0
fi
exit 0
""",
    )
    _use_fake_podman(monkeypatch, _fake_podman_bin(tmp_path))
    context = _dockerfile_context(tmp_path)

    tag = ensure_devbox_image(context / "Dockerfile")

    assert tag == f"localhost/devbox:test-{devbox_context_fingerprint(context)}"
    assert not build_marker.exists(), "build ran even though the image existed"


@pytest.mark.unit
def test_successful_build_returns_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_marker = tmp_path / "build-ran"
    _write_fake_podman(
        _fake_podman_bin(tmp_path),
        f"""
if [[ "$1 $2" == "image exists" ]]; then
  exit 1
fi
if [[ "$1" == "build" ]]; then
  touch {shlex.quote(str(build_marker))}
  exit 0
fi
exit 0
""",
    )
    _use_fake_podman(monkeypatch, _fake_podman_bin(tmp_path))
    context = _dockerfile_context(tmp_path)

    tag = ensure_devbox_image(context / "Dockerfile")

    assert tag == f"localhost/devbox:test-{devbox_context_fingerprint(context)}"
    assert build_marker.exists()


@pytest.mark.unit
def test_failed_build_reports_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_fake_podman(
        _fake_podman_bin(tmp_path),
        """
if [[ "$1 $2" == "image exists" ]]; then
  exit 1
fi
if [[ "$1" == "build" ]]; then
  echo "Error: mock build failure" >&2
  exit 125
fi
exit 0
""",
    )
    _use_fake_podman(monkeypatch, _fake_podman_bin(tmp_path))
    context = _dockerfile_context(tmp_path)

    with pytest.raises(pytest.fail.Exception, match="mock build failure"):
        ensure_devbox_image(context / "Dockerfile")


@pytest.mark.unit
def test_wedged_build_times_out_and_terminates_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wedged nested build must fail loudly and leave nothing spinning.

    The fake `podman build` models the wedge from issue #252: the build and
    its child both ignore SIGTERM, so only the runner's escalated group
    SIGKILL can stop them. Before the fix the fixture's unbounded
    `subprocess.run` hung the whole session on exactly this shape.
    """
    pidfile = tmp_path / "wedge-pids"
    # SIG_IGN survives fork and exec, so making the ignore explicit in the
    # child (not just inherited from the parent) keeps the model honest for
    # readers: both processes really do ignore SIGTERM, and only the
    # runner's escalated group SIGKILL can stop them.
    _write_fake_podman(
        _fake_podman_bin(tmp_path),
        f"""
if [[ "$1 $2" == "image exists" ]]; then
  exit 1
fi
if [[ "$1" == "build" ]]; then
  trap '' TERM INT
  (trap '' TERM INT; exec sleep 300) &
  child=$!
  printf '%s\\n%s\\n' $$ "$child" > {shlex.quote(str(pidfile))}
  wait
fi
exit 0
""",
    )
    _use_fake_podman(monkeypatch, _fake_podman_bin(tmp_path))
    monkeypatch.setenv("DEVBOX_IMAGE_BUILD_TIMEOUT", "2")
    context = _dockerfile_context(tmp_path)

    with pytest.raises(pytest.fail.Exception) as excinfo:
        ensure_devbox_image(context / "Dockerfile")

    message = str(excinfo.value)
    assert "Timed out after 2s building the devbox image" in message
    assert "process group was terminated" in message
    assert "issue #252" in message
    assert "DEVBOX_IMAGE_BUILD_TIMEOUT" in message

    build_pid, child_pid = (int(v) for v in pidfile.read_text().split())
    assert _wait_pid_gone(build_pid), (
        f"wedged build pid {build_pid} survived the timeout cleanup"
    )
    assert _wait_pid_gone(child_pid), (
        f"wedged build child {child_pid} survived the timeout cleanup"
    )


@pytest.mark.unit
def test_wedged_image_exists_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A runtime wedged on `image exists` must fail loudly, not hang."""
    pidfile = tmp_path / "wedge-pids"
    # SIG_IGN survives fork and exec: with the ignore set explicitly in the
    # child, both processes really do ignore SIGTERM, and only the runner's
    # escalated group SIGKILL can stop them.
    _write_fake_podman(
        _fake_podman_bin(tmp_path),
        f"""
if [[ "$1 $2" == "image exists" ]]; then
  trap '' TERM INT
  (trap '' TERM INT; exec sleep 300) &
  child=$!
  printf '%s\\n%s\\n' $$ "$child" > {shlex.quote(str(pidfile))}
  wait
fi
exit 0
""",
    )
    _use_fake_podman(monkeypatch, _fake_podman_bin(tmp_path))
    monkeypatch.setattr("tests.conftest.IMAGE_EXISTS_TIMEOUT", 2.0)
    context = _dockerfile_context(tmp_path)

    with pytest.raises(pytest.fail.Exception) as excinfo:
        ensure_devbox_image(context / "Dockerfile")

    message = str(excinfo.value)
    assert "did not complete within 2s" in message
    assert "infrastructure failure" in message

    exists_pid, child_pid = (int(v) for v in pidfile.read_text().split())
    assert _wait_pid_gone(exists_pid), (
        f"wedged image-exists pid {exists_pid} survived the timeout cleanup"
    )
    assert _wait_pid_gone(child_pid), (
        f"wedged image-exists child {child_pid} survived the timeout cleanup"
    )


@pytest.mark.unit
def test_image_build_timeout_default_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEVBOX_IMAGE_BUILD_TIMEOUT", raising=False)
    assert image_build_timeout() == DEFAULT_IMAGE_BUILD_TIMEOUT


@pytest.mark.unit
def test_image_build_timeout_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEVBOX_IMAGE_BUILD_TIMEOUT", "123.5")
    assert image_build_timeout() == 123.5


@pytest.mark.unit
def test_image_build_timeout_invalid_values_fall_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for bad_value in ("not-a-number", "0", "-10", "", "inf", "nan"):
        monkeypatch.setenv("DEVBOX_IMAGE_BUILD_TIMEOUT", bad_value)
        assert image_build_timeout() == DEFAULT_IMAGE_BUILD_TIMEOUT, bad_value
