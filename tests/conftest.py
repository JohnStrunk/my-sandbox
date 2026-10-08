import errno
import fcntl
import getpass
import hashlib
import math
import os
import pwd
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts.vm_preflight import CapabilityResult, check_vm_capabilities

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_VM_START_TIMEOUT = 3600.0
VM_START_TIMEOUT_ENV_VAR = "DEVBOX_VM_START_TIMEOUT"


def user_runtime_offline_guard(runtime: str) -> str:
    """Return guest-shell setup that stops and restores one rootless runtime."""
    socket_path = {
        "docker": "docker.sock",
        "podman": "podman/podman.sock",
    }.get(runtime)
    if socket_path is None:
        raise ValueError(f"unsupported rootless runtime: {runtime}")

    script = r"""
runtime_name=__RUNTIME__
runtime_socket="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/__SOCKET_PATH__"
runtime_service_was_active=false
runtime_socket_was_active=false
if systemctl --user is-active --quiet "${runtime_name}.service"; then
  runtime_service_was_active=true
fi
if systemctl --user is-active --quiet "${runtime_name}.socket"; then
  runtime_socket_was_active=true
fi
restore_user_runtime() {
  local status="${1:-$?}"
  trap - EXIT HUP INT TERM
  if [[ "$runtime_service_was_active" == true ]]; then
    systemctl --user start "${runtime_name}.service" || status=1
  else
    systemctl --user stop "${runtime_name}.service" >/dev/null 2>&1 || status=1
  fi
  if [[ "$runtime_socket_was_active" == true ]]; then
    systemctl --user start "${runtime_name}.socket" || status=1
  else
    systemctl --user stop "${runtime_name}.socket" >/dev/null 2>&1 || true
  fi
  exit "$status"
}
verify_user_runtime_offline() {
  if systemctl --user is-active --quiet "${runtime_name}.service" \
    || systemctl --user is-active --quiet "${runtime_name}.socket"; then
    echo "${runtime_name} service/socket remained active during smoke" >&2
    return 1
  fi
  if [[ -S "$runtime_socket" ]] \
    && curl --fail --silent --show-error --max-time 3 \
      --unix-socket "$runtime_socket" http://d/_ping >/dev/null 2>&1; then
    echo "${runtime_name} API remained reachable during independent-backend smoke" >&2
    return 1
  fi
  return 0
}
trap restore_user_runtime EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
systemctl --user stop "${runtime_name}.socket" >/dev/null 2>&1 || true
systemctl --user stop "${runtime_name}.service"
verify_user_runtime_offline
"""
    return (
        script.replace("__RUNTIME__", runtime)
        .replace("__SOCKET_PATH__", socket_path)
        .strip()
    )


def expected_lima_system_script_sha256(
    repo_root: Path, guest_user: str | None = None
) -> str:
    """Hash the system provisioner after Lima's single ``{{.User}}`` render."""
    guest_user = guest_user or pwd.getpwuid(os.getuid()).pw_name
    script = (repo_root / "lima" / "provision-system.sh").read_text()
    if script.count("{{.User}}") != 1:
        raise AssertionError("expected one Lima user template in provision-system.sh")
    rendered = script.replace("{{.User}}", guest_user)
    return hashlib.sha256(rendered.encode()).hexdigest()


def expected_lima_provisioning_fingerprint(
    repo_root: Path, guest_user: str | None = None
) -> str:
    """Mirror the host/guest provisioning fingerprint for regression tests."""

    def sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    assets = (
        (repo_root / "lima" / "devbox-go", "/var/lib/devbox-vm/tool-assets/devbox-go"),
        (
            repo_root / "lima" / "check_toolchain.py",
            "/var/lib/devbox-vm/tool-assets/check_toolchain.py",
        ),
        (repo_root / "lima" / "semble", "/var/lib/devbox-vm/tool-assets/semble"),
    )
    asset_manifest = "".join(
        f"{sha(source)}  {guest_path}\n" for source, guest_path in assets
    )
    components = (
        sha(repo_root / "lima" / "tool-versions.json"),
        expected_lima_system_script_sha256(repo_root, guest_user),
        sha(repo_root / "lima" / "provision-user.sh"),
        sha(repo_root / "lima" / "seed-opencode-state.py"),
        sha(repo_root / "lima" / "provision-tools.sh"),
        hashlib.sha256(asset_manifest.encode()).hexdigest(),
    )
    return hashlib.sha256(("\n".join(components) + "\n").encode()).hexdigest()


def copy_repository_for_vm(source_root: Path, destination: Path) -> None:
    """Copy tracked and non-ignored checkout files, excluding external symlinks."""
    root = source_root.resolve()
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        capture_output=True,
        check=True,
        timeout=30,
    )
    destination.mkdir(parents=True, exist_ok=True)
    for raw_path in result.stdout.split(b"\0"):
        if not raw_path:
            continue
        relative = Path(os.fsdecode(raw_path))
        if relative.is_absolute() or ".." in relative.parts:
            raise AssertionError(f"git returned an unsafe repository path: {relative}")
        source = root / relative
        try:
            resolved = source.resolve(strict=False)
            metadata = source.lstat()
        except OSError:
            continue
        if not resolved.is_relative_to(root):
            continue
        target = destination / relative
        if stat.S_ISLNK(metadata.st_mode):
            link = os.readlink(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(link)
        elif stat.S_ISDIR(metadata.st_mode):
            target.mkdir(parents=True, exist_ok=True)
        elif stat.S_ISREG(metadata.st_mode):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def initialize_local_test_repository(repo_path: Path) -> None:
    """Create a local credential-free index for later filtered VM copies."""
    subprocess.run(["git", "init", "--quiet", str(repo_path)], check=True, timeout=30)
    subprocess.run(
        ["git", "-C", str(repo_path), "config", "user.name", "VM test"],
        check=True,
        timeout=30,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo_path),
            "config",
            "user.email",
            "vm-test@example.invalid",
        ],
        check=True,
        timeout=30,
    )
    subprocess.run(
        ["git", "-C", str(repo_path), "add", "--all"], check=True, timeout=30
    )


@dataclass(frozen=True)
class LimaVM:
    """A disposable, provisioned Lima VM owned by this test session."""

    name: str | None
    env: dict[str, str]
    repo_path: str
    guest_home: str
    guest_runtime_env: dict[str, str] | None = None

    def _lima(
        self, args: list[str], *, timeout: float = 120.0
    ) -> subprocess.CompletedProcess[str]:
        if self.name is None:
            raise AssertionError("the current guest VM has no external Lima instance")
        return run_in_process_group(["limactl", *args], timeout=timeout, env=self.env)

    def run(
        self,
        command: list[str],
        *,
        timeout: float = 120.0,
        use_guest_runtime: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        if self.name is None:
            env = self.env
            if use_guest_runtime and self.guest_runtime_env:
                env = {**self.env, **self.guest_runtime_env}
            return run_in_process_group(command, timeout=timeout, env=env)
        return run_in_process_group(
            ["limactl", "shell", self.name, "--", *command],
            timeout=timeout,
            env=self.env,
        )

    def snapshot(self, tag: str = "clean", *, timeout: float = 300.0) -> None:
        result = self._lima(
            ["snapshot", "create", self.name, "--tag", tag], timeout=timeout
        )
        if result.returncode != 0:
            raise AssertionError(
                f"Could not snapshot Lima VM {self.name}: {result.stderr.strip()}"
            )

    def restore_snapshot(self, tag: str = "clean", *, timeout: float = 600.0) -> None:
        """Restore the named clean-state snapshot and restart the VM."""
        for operation, args in (
            ("stop", ["stop", self.name]),
            ("restore snapshot", ["snapshot", "apply", self.name, "--tag", tag]),
            ("restart", ["start", self.name]),
        ):
            result = self._lima(args, timeout=timeout)
            if result.returncode != 0:
                raise AssertionError(
                    f"Could not {operation} Lima VM {self.name}: "
                    f"{result.stderr.strip()}"
                )

    def clone(self, name: str, *, start: bool = True, timeout: float = 600.0) -> str:
        """Create an independent VM copy for tests needing an isolated state."""
        args = ["clone", self.name, name]
        if start:
            args.append("--start")
        result = self._lima(args, timeout=timeout)
        if result.returncode != 0:
            raise AssertionError(
                f"Could not clone Lima VM {self.name} as {name}: "
                f"{result.stderr.strip()}"
            )
        return name


def vm_start_timeout() -> float:
    """Resolve the bounded Lima startup timeout in seconds."""
    raw_value = os.environ.get(VM_START_TIMEOUT_ENV_VAR)
    if not raw_value:
        return DEFAULT_VM_START_TIMEOUT
    try:
        value = float(raw_value)
    except ValueError:
        return DEFAULT_VM_START_TIMEOUT
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_VM_START_TIMEOUT
    return value


def vm_test_environment(home: Path) -> dict[str, str]:
    """Build the non-secret host environment used to control disposable VMs."""
    runtime_dir = home / "run"
    temp_dir = home / "tmp"
    cache_dir = home / "cache"
    for directory in (runtime_dir, temp_dir, cache_dir):
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
    kb_root = home / "kb"
    (kb_root / "notes").mkdir(parents=True, exist_ok=True)
    (kb_root / "kbase.py").write_text("# VM-test mount sentinel\n")
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(home),
        "LIMA_HOME": str(home / ".lima"),
        "TMPDIR": str(temp_dir),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(cache_dir),
        "XDG_RUNTIME_DIR": str(runtime_dir),
    }
    for name in ("LANG", "LC_ALL", "LC_CTYPE", "TERM", "CI", VM_START_TIMEOUT_ENV_VAR):
        if name in os.environ:
            env[name] = os.environ[name]
    return env


def guest_runtime_environment(runtime_dir: Path | None = None) -> dict[str, str]:
    """Discover runtime sockets only for tests that explicitly need them."""
    runtime_dir = runtime_dir or Path(f"/run/user/{os.getuid()}")
    runtime: dict[str, str] = {}
    if runtime_dir.is_dir():
        runtime["XDG_RUNTIME_DIR"] = str(runtime_dir)
    bus = runtime_dir / "bus"
    if bus.exists():
        runtime["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
    return runtime


_PRIVATE_LIMA_ROOT = Path("/tmp/opencode")


def _guest_opencode_scratch_problem(root: Path) -> str | None:
    try:
        metadata = root.lstat()
    except OSError as error:
        return f"cannot inspect path: {error}"
    if stat.S_ISLNK(metadata.st_mode):
        return "is a symlink"
    if not stat.S_ISDIR(metadata.st_mode):
        return "is not a directory"

    try:
        with Path("/proc/self/mountinfo").open(encoding="utf-8") as mountinfo:
            for line in mountinfo:
                fields = line.split()
                if len(fields) < 5:
                    return "cannot parse /proc/self/mountinfo"
                # The checked paths are fixed names without mountinfo escapes.
                if fields[4] == str(root):
                    return "is a mountpoint"
    except UnicodeError:
        return "cannot decode /proc/self/mountinfo"
    except OSError as error:
        return f"cannot inspect /proc/self/mountinfo: {error}"

    mode = stat.S_IMODE(metadata.st_mode)
    if mode != 0o1777:
        return f"has mode {mode:04o}; expected 01777"
    if metadata.st_uid != 0 or metadata.st_gid != 0:
        return f"is owned by {metadata.st_uid}:{metadata.st_gid}; expected root:root"
    if not os.access(root, os.W_OK | os.X_OK):
        return "is not writable/searchable"
    return None


def _private_lima_home(*, in_guest: bool) -> Path:
    root = _PRIVATE_LIMA_ROOT if in_guest else Path(tempfile.gettempdir())
    user = getpass.getuser()
    if in_guest:
        # The root-owned entry beneath sticky /tmp cannot be swapped by either
        # unprivileged account between this check and mkdtemp below.
        problem = _guest_opencode_scratch_problem(root)
        if problem:
            raise RuntimeError(
                f"Lima VM tests require writable guest scratch at {root} for current "
                f"user '{user}' (expected root:root mode 01777): {problem}; "
                "recreate the VM from the current template and provisioner"
            )
    try:
        return Path(tempfile.mkdtemp(prefix="lima-", dir=root))
    except OSError as error:
        if in_guest:
            raise RuntimeError(
                f"Lima VM tests could not create scratch under {root} for current "
                f"user '{user}' (expected root:root mode 01777); recreate the VM "
                "from the current template and provisioner"
            ) from error
        raise RuntimeError(
            f"Lima VM tests could not create a private home under host temporary "
            f"directory {root} for current user '{user}': {error}"
        ) from error


def lima_vm_start_command(repo_root: Path, instance: str, timeout: float) -> list[str]:
    return [
        "limactl",
        "start",
        "--yes",
        "--name",
        instance,
        "--cpus",
        "4",
        "--memory",
        "10",
        "--timeout",
        f"{timeout:g}s",
        "--param",
        "SrcPath=/workspace/src",
        "--param",
        f"RepoPath=/workspace/src/{repo_root.name}",
        "--param",
        "KbPath=/workspace/kb",
        str(repo_root / "lima" / "devbox.yaml"),
    ]


def _cleanup_private_lima_home(env: dict[str, str]) -> list[str]:
    """Stop and remove every instance under this fixture's private Lima home."""
    listed = run_in_process_group(["limactl", "list", "-q"], timeout=30, env=env)
    if listed.returncode != 0:
        detail = listed.stderr.strip()
        if not listed.stdout.strip() and "no instance found" in detail.lower():
            return []
        return [
            f"limactl list failed (exit {listed.returncode})"
            + (f": {detail}" if detail else " without diagnostics")
        ]
    errors: list[str] = []
    for name in listed.stdout.splitlines():
        name = name.strip()
        if not name:
            continue
        stopped = run_in_process_group(["limactl", "stop", name], timeout=90, env=env)
        deleted = run_in_process_group(
            ["limactl", "delete", "--force", name], timeout=90, env=env
        )
        for operation, result in (("stop", stopped), ("delete", deleted)):
            if result.returncode != 0 and "not found" not in result.stderr.lower():
                errors.append(f"{operation} {name}: {result.stderr.strip()}")
    return errors


# Credential-isolated test runner (issue #81)
# ---------------------------------------------------------------------------
# Provider and integration credentials must never influence the outcome of an
# isolated test, and mock command logs/diagnostics must never contain host
# credential values. `CREDENTIAL_ENV_VARS` is the maintained scrub list of
# provider/integration environment variables that `devbox` and its supporting
# scripts read. The `_isolated_test_environment` autouse fixture below
# removes these from `os.environ` for every test by default, so unit and VM
# tests are deterministic whether or not the host happens to have any set.
#
# Tests that need to exercise credential passthrough behavior opt in
# explicitly (e.g. via `monkeypatch.setenv(...)`, or the `host_credentials`
# and `isolated_env` fixtures below).
#
# `tests/e2e_inference` is the intentional exception: those tests are
# end-to-end checks that require real provider credentials, so they are
# exempt from the automatic scrub (see the `e2e_inference` marker check in
# `_isolated_test_environment`).
CREDENTIAL_ENV_VARS = (
    "GEMINI_API_KEY",
    "GOOGLE_GENERATIVE_AI_API_KEY",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "CONTEXT7_API_KEY",
    "TAVILY_API_KEY",
    "IGLOO_MCP_COMMUNITY",
    "IGLOO_MCP_COMMUNITY_KEY",
    "IGLOO_MCP_APP_PASS",
    "IGLOO_MCP_APP_ID",
    "IGLOO_MCP_USERNAME",
    "IGLOO_MCP_PASSWORD",
    "GITLAB_HOST",
    "GITLAB_TOKEN",
    "LITEMAAS_API_KEY",
    "ENMAAS_URL",
    "ENMAAS_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "PRICETAG_HOSTED_URL",
    "PRICETAG_OPENAI_URL",
    "PRICETAG_API_KEY",
    "OPENROUTER_API_KEY",
    "GOOGLE_CLOUD_PROJECT",
    "VERTEX_LOCATION",
)

# Environment overrides that can make a CLI read configuration or credentials
# from a host path even when `$HOME` is isolated. Tests may set these
# explicitly when that behavior is what they are verifying.
HOST_CONFIG_ENV_VARS = (
    "AWS_CONFIG_FILE",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AZURE_CONFIG_DIR",
    "CLOUDSDK_CONFIG",
    "CONTAINERS_CONF",
    "CONTAINERS_REGISTRIES_CONF",
    "CONTAINERS_STORAGE_CONF",
    "DOCKER_CONFIG",
    "GH_CONFIG_DIR",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_SSH_COMMAND",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GLAB_CONFIG_DIR",
    "KUBECONFIG",
    "NETRC",
    "NPM_CONFIG_USERCONFIG",
    "OPENCODE_CONFIG",
    "OPENCODE_CONFIG_DIR",
    "PIP_CONFIG_FILE",
    "SSH_AUTH_SOCK",
)

ISOLATION_ENV_VARS = CREDENTIAL_ENV_VARS + HOST_CONFIG_ENV_VARS
SAFE_TEST_ENV_VARS = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "CI",
)
UNLISTED_SENSITIVE_ENV_VARS = (
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "PYTHONPATH",
    "BASH_ENV",
)


@pytest.fixture(autouse=True)
def _isolated_test_environment(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scrub provider/integration credentials from every test by default.

    This is the standard, reusable isolation applied to the whole suite
    (issue #81): unit and VM tests must produce the
    same result whether or not the host happens to have credentials set.
    Tests under `tests/e2e_inference` are intentional end-to-end tests that
    need real credentials, so they're exempt via the `e2e_inference` marker.
    """
    if request.node.get_closest_marker("e2e_inference") is not None:
        return
    for name in ISOLATION_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def host_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate a host machine with credentials and config overrides set.

    Used to verify that isolated tests/launchers don't accidentally forward
    host state they weren't explicitly given.
    """
    for name in ISOLATION_ENV_VARS:
        monkeypatch.setenv(name, f"host-{name.lower()}")
    for name in UNLISTED_SENSITIVE_ENV_VARS:
        monkeypatch.setenv(name, f"host-{name.lower()}")


@pytest.fixture
def isolated_home(tmp_path: Path) -> Path:
    """A fresh, empty directory to use as `$HOME` for a subprocess.

    Prevents host CLI configuration and credential files (e.g. `gh`'s auth
    config, gcloud application-default credentials, or OpenCode state) from
    being discovered by scripts under test unless a test explicitly
    populates this directory.
    """
    home = tmp_path / "isolated-home"
    home.mkdir(exist_ok=True)
    return home


@pytest.fixture
def isolated_env(host_credentials: None, isolated_home: Path) -> dict[str, str]:
    """A deterministic environment for launching `devbox` (or similar
    scripts) as a subprocess: no provider/integration credentials and no
    host home-directory state, unless a test adds them explicitly.
    """
    env = {name: os.environ[name] for name in SAFE_TEST_ENV_VARS if name in os.environ}
    env.setdefault("PATH", os.defpath)
    env["HOME"] = str(isolated_home)
    isolated_xdg = isolated_home.parent / "isolated-xdg"
    for xdg_var, subdir in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_DATA_HOME", "share"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_CACHE_HOME", "cache"),
    ):
        env[xdg_var] = str(isolated_xdg / subdir)

    return env


@pytest.fixture
def shared_process_signal_test_lock() -> Iterator[None]:
    """Bound cross-worktree interference in timing-sensitive signal tests.

    Each sanitized test invocation has a private TMPDIR, so use a stable,
    per-UID lock under a private directory in /tmp for the few tests that send
    signals to subprocess groups. This serializes only those tests across
    worktrees.
    """
    fd, lock_path = _open_shared_process_signal_test_lock()
    locked = False
    try:
        _wait_for_shared_process_signal_test_lock(fd, lock_path)
        locked = True

        yield
    finally:
        if locked:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _open_shared_process_signal_test_lock(
    root: Path = Path("/tmp"),
) -> tuple[int, Path]:
    """Create/open the cross-worktree signal lock without following links."""
    uid = os.getuid()
    lock_directory = root / f"my-sandbox-process-signals-{uid}"
    lock_path = lock_directory / "process-signals.lock"
    try:
        os.mkdir(lock_directory, 0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        pytest.skip(
            "infrastructure limitation: could not create process-signal lock "
            f"directory {lock_directory}: {exc}"
        )

    try:
        directory_fd = os.open(
            lock_directory,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except OSError as exc:
        pytest.skip(
            "infrastructure limitation: could not safely open process-signal lock "
            f"directory {lock_directory}: {exc}"
        )

    lock_fd = -1
    try:
        directory_stat = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or directory_stat.st_uid != uid
            or stat.S_IMODE(directory_stat.st_mode) & 0o077
        ):
            pytest.skip(
                "infrastructure limitation: process-signal lock directory must be "
                f"owned by uid {uid} and private: {lock_directory}"
            )

        try:
            lock_fd = os.open(
                "process-signals.lock",
                os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=directory_fd,
            )
        except OSError as exc:
            pytest.skip(
                "infrastructure limitation: could not safely open process-signal "
                f"test lock {lock_path}: {exc}"
            )

        file_stat = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_uid != uid
            or file_stat.st_nlink != 1
        ):
            pytest.skip(
                "infrastructure limitation: process-signal test lock must be a "
                f"regular file owned by uid {uid} with one link: {lock_path}"
            )
        os.fchmod(lock_fd, 0o600)
        result_fd = lock_fd
        lock_fd = -1
        return result_fd, lock_path
    except OSError as exc:
        pytest.skip(
            "infrastructure limitation: could not secure process-signal test "
            f"lock {lock_path}: {exc}"
        )
    finally:
        os.close(directory_fd)
        if lock_fd >= 0:
            os.close(lock_fd)


def _wait_for_shared_process_signal_test_lock(
    fd: int,
    lock_path: Path,
    *,
    timeout: float = 300,
    try_lock: Callable[[int, int], None] = fcntl.flock,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Wait for the shared lock, skipping when shared-guest contention is excessive."""
    deadline = monotonic() + timeout
    while True:
        try:
            try_lock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if monotonic() >= deadline:
                pytest.skip(
                    "infrastructure limitation: timed out waiting for process-signal "
                    f"test lock {lock_path}"
                )
            sleep(0.05)
        except OSError as exc:
            if exc.errno not in {errno.ENOLCK, errno.EOPNOTSUPP, errno.ENOSYS}:
                raise
            pytest.skip(
                "infrastructure limitation: process-signal test lock is unavailable "
                f"on this filesystem: {exc}"
            )


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def devbox_path(repo_root: Path) -> Path:
    return repo_root / "devbox"


@pytest.fixture(scope="session")
def vm_capability_result() -> CapabilityResult:
    """Check KVM once so VM tiers can skip as infrastructure-limited."""
    return check_vm_capabilities()


@pytest.fixture(scope="session")
def devbox_vm(
    repo_root: Path,
    tmp_path_factory: pytest.TempPathFactory,
    vm_capability_result: CapabilityResult,
) -> Iterator[LimaVM]:
    """Use the current Lima guest or start a disposable provisioned VM.

    The source mount contains only a working-tree copy of this repository. This
    avoids stacking the host's whole project mount into another 9p mount. A
    disposable VM gets empty credential/config mounts and a private HOME. When
    tests reuse the current guest, the environment and HOME are isolated, but
    same-UID processes can still read files under the mounted `.host-config`;
    guest mode is for trusted source only. The fixture reserves a private Lima
    home for recursive L2 instances.
    """
    if not vm_capability_result.available:
        pytest.skip(
            "VM infrastructure limitation: " + "; ".join(vm_capability_result.reasons)
        )
    if not shutil.which("limactl"):
        pytest.skip("VM infrastructure limitation: limactl is not installed")

    in_guest = (
        os.environ.get("MY_SANDBOX_VM_TEST_IN_GUEST") == "1"
        or Path("/etc/devbox/tool-versions.json").is_file()
    )
    if in_guest:
        home = tmp_path_factory.mktemp("my-sandbox-vm-test-home")
        env = vm_test_environment(home)
        runtime_env = guest_runtime_environment()
        guest_home = pwd.getpwuid(os.getuid()).pw_dir
        env.update(
            {
                "TMPDIR": "/tmp",
                "PATH": ":".join(
                    (
                        f"{guest_home}/.local/bin",
                        f"{guest_home}/.cargo/bin",
                        "/var/lib/devbox-toolbuilder/.local/bin",
                        "/var/lib/devbox-toolbuilder/.cargo/bin",
                        "/usr/local/node/bin",
                        "/usr/local/go/bin",
                        "/usr/local/bin",
                        "/usr/bin",
                        "/bin",
                    )
                ),
                "CARGO_HOME": f"{guest_home}/.cargo",
                "RUSTUP_HOME": "/var/lib/devbox-toolbuilder/.rustup",
                "SEMBLE_BIN": f"{guest_home}/.local/bin/semble-bin",
            }
        )
        lima_home = _private_lima_home(in_guest=True)
        env["LIMA_HOME"] = str(lima_home)
        try:
            yield LimaVM(
                None,
                env,
                str(repo_root),
                guest_home,
                runtime_env,
            )
        finally:
            cleanup_errors = _cleanup_private_lima_home(env)
            if cleanup_errors:
                raise AssertionError(
                    "Could not clean up nested Lima VM(s): " + "; ".join(cleanup_errors)
                )
            shutil.rmtree(lima_home)
        return

    home = tmp_path_factory.mktemp("my-sandbox-vm-home")
    source_root = home / "src"
    source_root.mkdir()
    staged_repo = source_root / repo_root.name
    copy_repository_for_vm(repo_root, staged_repo)
    initialize_local_test_repository(staged_repo)
    (home / "kb").mkdir()
    for relative in (
        ".agents",
        ".config/opencode",
        ".local/share/opencode",
        ".local/state/opencode",
        ".local/state/devbox-opencode",
        ".config/gh",
        ".config/gcloud",
        ".config/acli",
        ".config/gws",
    ):
        (home / relative).mkdir(parents=True, exist_ok=True)
    instance = f"my-sandbox-vm-{uuid.uuid4().hex[:10]}"
    env = vm_test_environment(home)
    lima_home = _private_lima_home(in_guest=False)
    env["LIMA_HOME"] = str(lima_home)

    start_timeout = vm_start_timeout()
    start_command = lima_vm_start_command(repo_root, instance, start_timeout)
    start_result: subprocess.CompletedProcess[str] | None = None
    try:
        start_result = run_in_process_group(
            start_command,
            timeout=start_timeout,
            env=env,
        )
        if start_result.returncode != 0:
            pytest.fail(
                f"Could not start disposable Lima VM {instance} "
                f"(exit {start_result.returncode}).\n"
                f"stdout: {start_result.stdout}\nstderr: {start_result.stderr}"
            )
        vm = LimaVM(
            instance,
            env,
            f"/workspace/src/{repo_root.name}",
            f"/home/{getpass.getuser()}.guest",
        )
        vm.snapshot()
        yield vm
    finally:
        # This fixture owns its private Lima home; it never sees user-owned VMs.
        cleanup_errors = _cleanup_private_lima_home(env)
        if cleanup_errors:
            details = "; ".join(cleanup_errors)
            raise AssertionError(
                f"Could not clean up disposable Lima VM {instance}: {details}"
            )
        shutil.rmtree(lima_home)


# Process-group bounded command runner (issue #211)
# ---------------------------------------------------------------------------
# `subprocess.run(timeout=...)` kills only the direct child on timeout, so a
# timed-out test command leaks its descendants (e.g. Lima startup and its QEMU
# or provisioning children), which keep consuming CPU and storage after the
# command "returned". These runners launch each bounded
# command in a dedicated process group (`start_new_session=True`) and, on
# timeout, terminate/reap the entire group, reporting any cleanup failure.
TERM_GRACE_SECONDS = 2.0
KILL_GRACE_SECONDS = 2.0
# The wrapper (scripts/sanitized-test.sh) uses 10s/10s for the same two
# constants: it forwards interruption to whole test commands (pytest and
# its fixtures), which legitimately need longer to unwind than a single
# leaf build. Keep the values in sync deliberately, not accidentally.

# In-flight process groups started by `run_in_process_group`. Commands run
# in their own sessions (`start_new_session=True`), so a build survives any
# group-wide signal aimed at the suite itself. When the suite is terminated
# (SIGTERM/SIGHUP), the handlers below kill these groups first so an
# interrupted run cannot orphan a wedged, CPU-spinning build (issue #252).
_tracked_process_groups: set[int] = set()


def _kill_tracked_process_groups() -> None:
    """SIGKILL every in-flight ``run_in_process_group`` command group."""
    while True:
        try:
            pgids = tuple(_tracked_process_groups)
        except RuntimeError:  # a worker thread mutated the set mid-copy
            continue
        for pgid in pgids:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass  # the group already exited
        return


def _terminate_tracked_groups_and_exit(signum: int, frame: object) -> None:
    """SIGTERM/SIGHUP handler: kill tracked groups, then die by the signal.

    Python's default SIGTERM disposition kills the interpreter without
    unwinding, which would orphan builds running in their own sessions.
    Killing the tracked groups first keeps interrupting the run (or its
    wrapper) from leaving CPU-spinning processes behind (issue #252). The
    signal is then re-delivered with its default disposition so the process
    still exits with the correct 128+signal status.

    The re-delivery runs in ``finally`` so a failure inside the kill loop
    cannot leave the interpreter alive with the signal effectively
    swallowed.
    """
    try:
        _kill_tracked_process_groups()
    finally:
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)


def install_termination_handlers() -> None:
    """Install the suite's SIGTERM/SIGHUP termination handlers."""
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _terminate_tracked_groups_and_exit)


def pytest_sessionstart(session: pytest.Session) -> None:
    # SIGINT needs no handler of its own: KeyboardInterrupt raised in the
    # main thread unwinds through run_in_process_group, whose BaseException
    # cleanup terminates the group. Commands started from worker threads
    # cannot see that exception, but every runner call is timeout-bounded,
    # so a lingering worker command still dies with its own escalation.
    install_termination_handlers()


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    """Separate capability-only skips from product failures in VM tiers."""
    skips = terminalreporter.stats.get("skipped", [])
    capability_skips = []
    for entry in skips:
        report = entry[0] if isinstance(entry, tuple) else entry
        if "VM infrastructure limitation:" in str(getattr(report, "longrepr", "")):
            capability_skips.append(report)
    if capability_skips:
        terminalreporter.write_sep(
            "=",
            f"VM infrastructure limitations: {len(capability_skips)} test(s) "
            "skipped because required host capabilities were unavailable",
        )


def _process_group_alive(pgid: int) -> bool:
    """True if any live member of process group ``pgid`` still exists.

    Zombies count as gone: a killed member whose parent dies without
    reaping it (e.g. under an init-less PID 1) can linger as a zombie
    forever, and killpg(2) treats zombies as live members. /proc is used
    where available to see through zombies; otherwise fall back to
    killpg(2) semantics.
    """
    try:
        entries = os.listdir("/proc")
    except OSError:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            # PermissionError or anything unexpected: assume alive, escalate.
            return True
        return True

    for name in entries:
        if not name.isdigit():
            continue
        try:
            stat = Path("/proc", name, "stat").read_text()
        except OSError:
            continue  # exited mid-scan
        # Fields after comm (which may contain spaces/parens):
        # state, ppid, pgrp, session, ...
        fields = stat[stat.rfind(")") + 1 :].split()
        if len(fields) >= 3 and fields[2] == str(pgid) and fields[0] != "Z":
            return True
    return False


def _wait_process_group_gone(
    pgid: int, proc: "subprocess.Popen[str]", grace: float
) -> bool:
    deadline = time.monotonic() + grace
    while True:
        # Reap the leader as soon as it dies: a zombie leader still counts
        # as a live group member for killpg(2), which would mask cleanup.
        proc.poll()
        if not _process_group_alive(pgid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def _process_gone(pid: int) -> bool:
    """True once ``pid`` no longer runs. A zombie counts as gone: the killed
    child is reparented once its parent dies, and hosts without a reaping
    init (e.g. pytest as PID 1 in a minimal guest) keep zombies whose
    ``kill(pid, 0)`` still succeeds.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    # The state field follows the comm field's last closing paren.
    state = stat[stat.rfind(")") + 1 :].split()[0]
    return state == "Z"


def _wait_pid_gone(pid: int, deadline_seconds: float = 15.0) -> bool:
    """Poll until ``pid`` no longer runs (zombies count as gone)."""
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        if _process_gone(pid):
            return True
        time.sleep(0.05)
    return False


def _terminate_process_group(
    proc: "subprocess.Popen[str]",
    term_grace: float = TERM_GRACE_SECONDS,
    kill_grace: float = KILL_GRACE_SECONDS,
) -> str:
    """Terminate process group ``proc.pid`` (a session leader).

    Escalates SIGTERM to SIGKILL and reaps the leader. Returns an empty
    string on clean cleanup, or a description of what could not be
    terminated/reaped.
    """
    pgid = proc.pid  # start_new_session=True makes the child a group leader
    if os.getpgid(pgid) != pgid:
        return f"pid {pgid} is not a process group leader; refusing to signal it"
    failures: list[str] = []

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError as exc:
        failures.append(f"SIGTERM to process group {pgid} failed: {exc}")

    if not _wait_process_group_gone(pgid, proc, term_grace):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            failures.append(f"SIGKILL to process group {pgid} failed: {exc}")
        if not _wait_process_group_gone(pgid, proc, kill_grace):
            failures.append(
                f"process group {pgid} still has live members after SIGKILL"
            )

    if proc.poll() is None:
        try:
            proc.wait(timeout=kill_grace)
        except subprocess.TimeoutExpired:
            failures.append(f"command process {proc.pid} could not be reaped")

    return "; ".join(failures)


def _force_kill_group(pgid: int) -> None:
    """SIGKILL a process group, ignoring an already-dead group."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass  # the group already exited


def run_in_process_group(
    cmd: list[str],
    timeout: float,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``cmd`` in a dedicated process group, bounded by ``timeout``.

    Mirrors ``subprocess.run(..., capture_output=True, text=True,
    timeout=..., check=False)`` for the success path, but on timeout the
    *entire* process group is terminated (SIGTERM, escalating to SIGKILL)
    so no descendants survive. The raised ``TimeoutExpired`` carries a
    ``process_group_cleanup`` attribute: an empty string when cleanup was
    clean, otherwise a description of the failure (also reported to
    stderr).
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=str(cwd) if cwd else None,
        start_new_session=True,
    )
    # start_new_session=True makes the child a group leader, so its pgid is
    # its pid. Tracking it lets the suite's termination handlers kill this
    # group even though it lives in its own session (issue #252).
    _tracked_process_groups.add(proc.pid)
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            try:
                cleanup_error = _terminate_process_group(proc)
                try:
                    proc.communicate(timeout=KILL_GRACE_SECONDS)  # reap + drain pipes
                except subprocess.TimeoutExpired:
                    # A descendant escaped the group and holds the pipes
                    # open; do not turn the bounded timeout into an
                    # unbounded hang.
                    for stream in (proc.stdout, proc.stderr):
                        if stream is not None:
                            stream.close()
                    cleanup_error = cleanup_error or (
                        "output pipes stayed open after group termination "
                        "(a descendant escaped the process group)"
                    )
            except BaseException:
                # An interrupt during timeout cleanup must not orphan a
                # half-terminated, SIGTERM-ignoring command: force the kill
                # before unwinding (issue #252).
                _force_kill_group(proc.pid)
                raise
            exc.process_group_cleanup = cleanup_error  # type: ignore[attr-defined]
            if cleanup_error:
                print(
                    f"WARNING: process-group cleanup for {cmd[0]!r} failed: "
                    f"{cleanup_error}",
                    file=sys.stderr,
                )
            raise
        except BaseException:
            # An interrupt (SIGINT raises KeyboardInterrupt, which unwinds
            # through communicate()) must not orphan the command: terminate
            # its whole process group on the way out, then keep unwinding
            # (issue #252).
            try:
                _terminate_process_group(proc)
            except BaseException:
                # A second interrupt during cleanup (impatient double
                # Ctrl-C) must not orphan a half-terminated,
                # SIGTERM-ignoring command either: force the kill.
                _force_kill_group(proc.pid)
                raise
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
            raise
    finally:
        _tracked_process_groups.discard(proc.pid)
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def run_bash_script(
    script_path: Path,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    cmd = [str(script_path)] + (args or [])
    return run_in_process_group(cmd, timeout=timeout, env=env, cwd=cwd)
