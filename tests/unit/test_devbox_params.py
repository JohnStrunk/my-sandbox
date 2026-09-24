import fcntl
import json
import os
import shutil
import stat
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.conftest import CREDENTIAL_ENV_VARS, run_bash_script

SEMBLE_MCP = {
    "type": "local",
    "command": ["semble"],
    "enabled": True,
}
GITHUB_MCP = {
    "type": "local",
    "command": ["github-mcp-server-proxy"],
    "enabled": True,
    "environment": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "{env:GH_TOKEN}",
        "GITHUB_TOOLSETS": "context,repos,issues,pull_requests,users",
    },
}
TAVILY_MCP = {
    "type": "remote",
    "url": "https://mcp.tavily.com/mcp/",
    "headers": {
        "Authorization": "Bearer {env:TAVILY_API_KEY}",
    },
    "enabled": True,
}


@pytest.fixture
def mock_podman_env(tmp_path: Path, isolated_env):
    bin_dir = tmp_path / "mock_bin"
    bin_dir.mkdir()
    log_file = tmp_path / "podman_calls.jsonl"

    mock_script = bin_dir / "podman"
    mock_script.write_text(f"""#!/usr/bin/env bash
python3 -c '
import sys, json
with open(sys.argv[1], "a") as f:
    f.write(json.dumps(sys.argv[2:]) + "\\n")
' "{log_file}" "$@"

if [ "$1" = "run" ] && [ "$2" = "--rm" ]; then
    if [ "$3" = "-i" ] \
        && [[ "$4" == devbox:* || "$4" == localhost/devbox:* ]] \
        && [ "$5" = "jq" ]; then
        if echo "$*" | grep -q 'select(.id'; then
            python3 -c '
import json
import sys

value = json.load(sys.stdin)
print(json.dumps({{
    item["id"]: {{
        "name": item.get("display_name") or item["id"],
        "limit": {{"context": 262144, "output": 8192}},
        "reasoning": True,
        "variants": {{
            "low": {{"effort": "low"}},
            "medium": {{"effort": "medium"}},
            "xhigh": {{"effort": "xhigh"}},
        }},
    }}
    for item in value.get("data", [])
    if isinstance(item.get("id"), str) and item["id"]
}}))
'
            exit 0
        fi
        python3 -c '
import json
import sys

def merge(left, right):
    result = left.copy()
    for key in right:
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(right[key], dict)
        ):
            result[key] = merge(result[key], right[key])
        else:
            result[key] = right[key]
    return result

result = dict()
text = sys.stdin.read()
index = 0
decoder = json.JSONDecoder()
while index < len(text):
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text):
        break
    value, index = decoder.raw_decode(text, index)
    result = merge(result, value)
print(json.dumps(result, separators=(",", ":")))
'
        exit 0
    fi
    if [ "$4" = "id" ] && [ "$5" = "-u" ]; then
        echo "1000"
        exit 0
    fi
    if [ "$4" = "id" ] && [ "$5" = "-g" ]; then
        echo "1000"
        exit 0
    fi
fi

if [ "$1" = "image" ] && [ "$2" = "exists" ]; then
    [ "${{MOCK_CONTEXT_IMAGE_EXISTS:-}}" = "1" ] && exit 0
    exit 1
fi

if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then
    if [ "${{5:-}}" = "devbox:latest" ]; then
        [ -n "${{MOCK_LATEST_IMAGE_ID:-}}" ] || exit 1
        echo "$MOCK_LATEST_IMAGE_ID"
    else
        echo "${{MOCK_CONTEXT_IMAGE_ID:-mock-context-image}}"
    fi
    exit 0
fi

if [ "$1" = "build" ]; then
    if [ -n "${{MOCK_BUILD_ACTIVE_DIR:-}}" ]; then
        if ! mkdir "${{MOCK_BUILD_ACTIVE_DIR}}" 2>/dev/null; then
            : > "${{MOCK_BUILD_OVERLAP_FILE}}"
        else
            sleep "${{MOCK_BUILD_SLEEP:-0.2}}"
            rmdir "${{MOCK_BUILD_ACTIVE_DIR}}"
        fi
    fi
    exit 0
fi

if [ "$1" = "tag" ]; then
    exit 0
fi

if [ "$1" = "run" ] && [ "$2" = "-d" ] \
    && [ "${{MOCK_REJECT_NESTED_SYSCTLS:-}}" = "1" ]; then
    case "$*" in
        *--sysctl*)
            echo "Error: OCI runtime error: crun: open" \
                "/proc/sys/net/ipv4/conf/default/route_localnet:" \
                "Read-only file system" >&2
            exit 126
            ;;
    esac
fi

if [ "$1" = "run" ] && [ "$2" = "-d" ] \
    && [ "${{MOCK_FAIL_CONTAINER_RUN:-}}" = "1" ]; then
    echo "Error: image create failed" >&2
    exit 125
fi

if [ "$1" = "run" ] && [ "$2" = "-d" ] \
    && [ "${{MOCK_FAIL_UNRELATED_SYSCTL:-}}" = "1" ]; then
    echo "Error: storage setup failed at /proc/sys/net/ipv4/ping_group_range" >&2
    exit 125
fi

if [ "$1" = "run" ] && [ "$2" = "-d" ] \
    && [ "${{MOCK_REJECT_IPV6_SYSCTL:-}}" = "1" ]; then
    case "$*" in
        *--sysctl*)
            echo "Error: crun: open /proc/sys/net/ipv6/conf/default/accept_ra" >&2
            exit 126
            ;;
    esac
fi

if [ "$1" = "run" ] && [ "$2" = "-d" ] \
    && [ "${{MOCK_REJECT_IPV6_FORWARDING:-}}" = "1" ]; then
    case "$*" in
        *--sysctl*)
            echo "Error: crun: open /proc/sys/net/ipv6/conf/all/forwarding" >&2
            exit 126
            ;;
    esac
fi

if [ "$1" = "container" ] && [ "$2" = "exists" ]; then
    # Default container does not exist
    [ "${{MOCK_CONTAINER_EXISTS:-}}" = "1" ] && exit 0
    if [ "${{MOCK_CONTAINER_EXISTS_ERROR:-}}" = "1" ]; then
        echo "Error: storage unavailable" >&2
        exit 125
    fi
    exit 1
fi

if [ "$1" = "rm" ] && [ "${{MOCK_RM_FAIL:-}}" = "1" ]; then
    echo "Error: removal failed" >&2
    exit 1
fi

if [ "$1" = "inspect" ]; then
    if [ "${{MOCK_VENV_SHADOW_MOUNT:-}}" = "1" ] && echo "$*" | grep -q "Mounts"; then
        echo "/sandbox/workdir/.venv /sandbox/kb /sandbox/.uv_cache "
        exit 0
    fi
    echo "true"
    exit 0
fi

if [ "$1" = "exec" ]; then
    # Mock /proc/self/uid_map and gid_map query
    if echo "$*" | grep -q "uid_map"; then
        [ "${{MOCK_UID_MAP_FAIL:-}}" = "1" ] && exit 1
        echo "65535"
        exit 0
    fi
    if echo "$*" | grep -q "gid_map"; then
        echo "65535"
        exit 0
    fi
    # Mock the container's own `git config --global <key> [value]`, so
    # tests can simulate an identity already configured inside the
    # (persistent) container via MOCK_CONTAINER_GIT_NAME/_EMAIL, without
    # a real container.
    if [ "$3" = "git" ] && [ "$4" = "config" ] && [ "$5" = "--global" ]; then
        if [ -n "${{7:-}}" ]; then
            # Simulated `git config --global <key> <value>` (set)
            if [ "${{MOCK_GIT_CONFIG_SET_FAILS:-}}" = "1" ]; then
                exit 1
            fi
            exit 0
        fi
        case "$6" in
            user.name)
                name="${{MOCK_CONTAINER_GIT_NAME:-}}"
                [ -n "$name" ] && echo "$name" && exit 0
                exit 1
                ;;
            user.email)
                email="${{MOCK_CONTAINER_GIT_EMAIL:-}}"
                [ -n "$email" ] && echo "$email" && exit 0
                exit 1
                ;;
        esac
    fi
    if [ "$3" = "gh" ] && [ "$4" = "auth" ] && [ "$5" = "setup-git" ]; then
        if [ "${{MOCK_GH_AUTH_SETUP_GIT_FAILS:-}}" = "1" ]; then
            exit 1
        fi
    fi
    if [ "$3" = "opencode" ] && [ "$4" = "models" ] && [ "$5" = "--refresh" ]; then
        if [ "${{MOCK_OPENCODE_REFRESH_FAILS:-}}" = "1" ]; then
            exit 1
        fi
    fi
    if [ "$3" = "podman" ] && [ "$4" = "create" ]; then
        echo "mock-preflight-container"
        exit 0
    fi
    if echo "$*" | grep -q "docker.sock"; then
        # Docker API readiness probe (curl --unix-socket .../_ping)
        [ "${{MOCK_DOCKER_API_NEVER_READY:-}}" = "1" ] && exit 1
        exit 0
    fi
    exit 0
fi

exit 0
""")
    mock_script.chmod(mock_script.stat().st_mode | stat.S_IEXEC)

    # Prevent the launcher from finding credentials in the host's gh config.
    fake_gh = bin_dir / "gh"
    fake_gh.write_text("#!/usr/bin/env bash\nexit 1\n")
    fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IEXEC)

    fake_curl = bin_dir / "curl"
    fake_curl.write_text(
        "#!/usr/bin/env bash\n"
        '[ -z "${MOCK_PRICETAG_CURL_LOG:-}" ] || '
        'printf "%s\\n" "$*" >> "$MOCK_PRICETAG_CURL_LOG"\n'
        "exit 97\n"
    )
    fake_curl.chmod(fake_curl.stat().st_mode | stat.S_IEXEC)

    env = isolated_env
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["MOCK_PRICETAG_CURL_LOG"] = str(tmp_path / "pricetag_curl_calls.log")
    return env, log_file


def parse_podman_calls(log_file: Path) -> list[list[str]]:
    if not log_file.exists():
        return []
    calls = []
    for line in log_file.read_text().splitlines():
        if line.strip():
            calls.append(json.loads(line))
    return calls


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
    )


def _init_repository(repo_dir: Path) -> None:
    _run_git(["init", "-b", "main", "."], repo_dir)
    _run_git(
        [
            "-c",
            "user.name=Devbox Test",
            "-c",
            "user.email=devbox-test@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "initial",
        ],
        repo_dir,
    )


def _copy_devbox_checkout(
    devbox_path: Path, checkout: Path, dockerfile_contents: str
) -> Path:
    (checkout / "container").mkdir(parents=True)
    launcher = checkout / "devbox"
    shutil.copyfile(devbox_path, launcher)
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    (checkout / "container" / "Dockerfile").write_text(dockerfile_contents)
    _init_repository(checkout)
    return launcher


def _assert_host_git_mount(volumes: list[str], path: Path) -> None:
    resolved = str(path.resolve())
    assert f"{resolved}:{resolved}" in volumes


def _assert_no_host_git_mount(volumes: list[str], path: Path) -> None:
    resolved = str(path.resolve())
    assert not any(volume.rsplit(":", 1)[-1] == resolved for volume in volumes)


def _write_dot_git_pointer(tmp_path: Path, gitdir: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").write_text(f"gitdir: {gitdir}\n")
    return project


def _create_container_volumes(log_file: Path) -> list[str]:
    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    return [
        run_call[index + 1]
        for index, arg in enumerate(run_call[:-1])
        if arg == "--volume"
    ]


@pytest.mark.unit
def test_devbox_gemini_env(devbox_path: Path, mock_podman_env, tmp_path: Path):
    env, log_file = mock_podman_env
    env["GEMINI_API_KEY"] = "mock-gemini-token"  # pragma: allowlist secret

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next(
        (c for c in calls if c and c[0] == "run" and "-d" in c),
        None,
    )
    assert run_call is not None
    assert "--env" in run_call
    assert "GEMINI_API_KEY=mock-gemini-token" in run_call
    assert "GOOGLE_GENERATIVE_AI_API_KEY=mock-gemini-token" in run_call


@pytest.mark.unit
def test_devbox_records_context_fingerprint_on_container(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None

    run_labels = [
        value
        for index, arg in enumerate(run_call[:-1])
        if arg == "--label"
        for value in [run_call[index + 1]]
    ]
    assert len(run_labels) == 1
    fingerprint_labels = [
        label
        for label in run_labels
        if label.startswith(
            "io.github.johnstrunk.my-sandbox.devbox-context-fingerprint="
        )
    ]
    assert len(fingerprint_labels) == 1
    assert any(
        call and call[0] == "exec" and call[2:] == ["opencode", "models", "--refresh"]
        for call in calls
    )


@pytest.mark.unit
def test_devbox_consumer_uses_shared_latest_tag(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    project = tmp_path / "consumer-project"
    project.mkdir()

    result = run_bash_script(devbox_path, ["true"], env=env, cwd=project)

    assert result.returncode == 0, result.stderr
    calls = parse_podman_calls(log_file)
    build_call = next(call for call in calls if call and call[0] == "build")
    context_tag = build_call[build_call.index("--tag") + 1]
    assert context_tag.startswith("localhost/devbox:context-")
    assert ["tag", context_tag, "devbox:latest"] in calls
    run_call = next(
        call for call in calls if call and call[0] == "run" and "-d" in call
    )
    assert run_call[-1] == "devbox:latest"


@pytest.mark.unit
def test_devbox_reuses_context_image(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env["MOCK_CONTEXT_IMAGE_EXISTS"] = "1"
    env["MOCK_LATEST_IMAGE_ID"] = "mock-context-image"
    project = tmp_path / "consumer-project"
    project.mkdir()

    result = run_bash_script(devbox_path, ["true"], env=env, cwd=project)

    assert result.returncode == 0, result.stderr
    calls = parse_podman_calls(log_file)
    assert not any(call and call[0] == "build" for call in calls)
    assert not any(call and call[0] == "tag" for call in calls)
    run_call = next(
        call for call in calls if call and call[0] == "run" and "-d" in call
    )
    assert run_call[-1] == "devbox:latest"


@pytest.mark.unit
def test_devbox_worktrees_use_distinct_images_and_share_the_build_lock(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env.pop("MY_SANDBOX_PODMAN_RUNTIME_LOCK_HELD", None)
    env["MY_SANDBOX_PODMAN_RUNTIME_LOCK_FILE"] = str(
        tmp_path / "runtime-locks" / "podman-runtime.lock"
    )
    active_build = tmp_path / "active-build"
    overlapping_build = tmp_path / "overlapping-build"
    env["MOCK_BUILD_ACTIVE_DIR"] = str(active_build)
    env["MOCK_BUILD_OVERLAP_FILE"] = str(overlapping_build)
    env["MOCK_BUILD_SLEEP"] = "0.2"

    checkouts = (
        tmp_path / "worktree-a",
        tmp_path / "worktree-b",
    )
    launchers = [
        _copy_devbox_checkout(
            devbox_path,
            checkout,
            f"FROM fedora:latest\nRUN echo {index}\n",
        )
        for index, checkout in enumerate(checkouts, start=1)
    ]

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                run_bash_script,
                launcher,
                ["true"],
                env=env,
                cwd=checkout,
                timeout=30,
            )
            for launcher, checkout in zip(launchers, checkouts, strict=True)
        ]
        results = [future.result(timeout=45) for future in futures]

    assert all(result.returncode == 0 for result in results), [
        result.stderr for result in results
    ]
    assert not overlapping_build.exists()

    calls = parse_podman_calls(log_file)
    build_calls = [call for call in calls if call and call[0] == "build"]
    build_tags = [call[call.index("--tag") + 1] for call in build_calls]
    assert len(build_tags) == 2
    assert len(set(build_tags)) == 2
    assert all(tag.startswith("localhost/devbox:context-") for tag in build_tags)
    assert not any(call and call[0] == "tag" for call in calls)
    runtime_images = {
        call[-1] for call in calls if call and call[0] == "run" and "-d" in call
    }
    assert runtime_images == set(build_tags)


@pytest.mark.unit
def test_devbox_lifecycle_lock_order_allows_test_child_to_remove_container(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, _ = mock_podman_env
    env.pop("MY_SANDBOX_PODMAN_RUNTIME_LOCK_HELD", None)
    env["MOCK_CONTAINER_EXISTS"] = "1"
    runtime_lock = tmp_path / "runtime-locks" / "podman-runtime.lock"
    runtime_lock.parent.mkdir()
    env["MY_SANDBOX_PODMAN_RUNTIME_LOCK_FILE"] = str(runtime_lock)
    project = tmp_path / "shared-project"
    project.mkdir()

    with runtime_lock.open("w") as parent_lock:
        fcntl.flock(parent_lock, fcntl.LOCK_EX)
        with ThreadPoolExecutor(max_workers=1) as executor:
            regular_launcher = executor.submit(
                run_bash_script,
                devbox_path,
                ["--remove"],
                env=env,
                cwd=project,
                timeout=10,
            )
            time.sleep(0.1)
            assert not regular_launcher.done(), (
                "a normal launcher should wait for the shared runtime lock"
            )

            test_child_env = env.copy()
            test_child_env["MY_SANDBOX_PODMAN_RUNTIME_LOCK_HELD"] = "1"
            test_child = run_bash_script(
                devbox_path,
                ["--remove"],
                env=test_child_env,
                cwd=project,
                timeout=5,
            )
            assert test_child.returncode == 0, test_child.stderr

            fcntl.flock(parent_lock, fcntl.LOCK_UN)
            regular_result = regular_launcher.result(timeout=15)

    assert regular_result.returncode == 0, regular_result.stderr


@pytest.mark.unit
def test_devbox_litemaas_env(devbox_path: Path, mock_podman_env, tmp_path: Path):
    env, log_file = mock_podman_env
    env["LITEMAAS_API_KEY"] = "mock-litemaas-token"  # pragma: allowlist secret

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    assert "LITEMAAS_API_KEY=mock-litemaas-token" in run_call


@pytest.mark.unit
def test_devbox_does_not_forward_host_credentials(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    logged = log_file.read_text()
    for name in CREDENTIAL_ENV_VARS:
        assert f"{name}=host-{name.lower()}" not in logged


@pytest.mark.unit
def test_devbox_launcher_uses_isolated_home(
    devbox_path: Path, mock_podman_env, tmp_path: Path, isolated_home: Path
):
    # The launcher must never discover the real host's CLI configuration or
    # credential files (e.g. gh, gcloud, OpenCode state) through $HOME.
    env, log_file = mock_podman_env
    assert env["HOME"] == str(isolated_home)
    assert env["HOME"] != os.environ.get("HOME")
    assert not any(isolated_home.iterdir())

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    volumes = [run_call[i + 1] for i, arg in enumerate(run_call) if arg == "--volume"]
    volume_destinations = {volume.rsplit(":", 1)[-1] for volume in volumes}
    # Project data and cache volumes are expected; no host config/credential
    # directories should be mounted.
    assert any(f"{run_dir}:/sandbox/" in v for v in volumes)
    assert {"/sandbox/.uv_cache", "/sandbox/.cache/pre-commit"} <= (volume_destinations)
    assert "/sandbox/.local/share/containers/storage" in volume_destinations
    assert not any(str(isolated_home) in volume for volume in volumes)


@pytest.mark.unit
@pytest.mark.parametrize("token_name", ["GH_TOKEN", "GITHUB_TOKEN"])
def test_devbox_github_mcp_config_from_token(
    devbox_path: Path, mock_podman_env, tmp_path: Path, token_name: str
):
    env, log_file = mock_podman_env
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    env.pop("CONTEXT7_API_KEY", None)
    env[token_name] = "mock-github-token"  # pragma: allowlist secret

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    assert "GH_TOKEN=mock-github-token" in run_call
    assert "GITHUB_TOKEN=mock-github-token" in run_call  # pragma: allowlist secret

    env_values = [
        run_call[index + 1] for index, arg in enumerate(run_call[:-1]) if arg == "--env"
    ]
    config_value = next(
        value for value in env_values if value.startswith("OPENCODE_CONFIG_CONTENT=")
    )
    config = json.loads(config_value.split("=", 1)[1])
    assert config == {
        "$schema": "https://opencode.ai/config.json",
        "disabled_providers": [
            "github-copilot",
            "gitlab",
            "google-vertex-anthropic",
        ],
        "permission": {
            "external_directory": {
                "/home/**": "allow",
                "/root/**": "deny",
                "/sandbox/**": "allow",
                "/tmp/**": "allow",
            },
        },
        "mcp": {
            "semble": SEMBLE_MCP,
            "github": GITHUB_MCP,
        },
    }
    assert "mock-github-token" not in config_value


@pytest.mark.unit
def test_devbox_context7_mcp_config_from_api_key(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    env.pop("CONTEXT7_API_KEY", None)
    env["CONTEXT7_API_KEY"] = "mock-context7-token"  # pragma: allowlist secret
    env["TAVILY_API_KEY"] = "mock-tavily-token"  # pragma: allowlist secret

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next(
        (c for c in calls if c and c[0] == "run" and "-d" in c),
        None,
    )
    assert run_call is not None
    assert (
        "CONTEXT7_API_KEY=mock-context7-token" in run_call
    )  # pragma: allowlist secret
    assert "TAVILY_API_KEY=mock-tavily-token" in run_call  # pragma: allowlist secret

    env_values = [
        run_call[index + 1] for index, arg in enumerate(run_call[:-1]) if arg == "--env"
    ]
    config_value = next(
        value for value in env_values if value.startswith("OPENCODE_CONFIG_CONTENT=")
    )
    config = json.loads(config_value.split("=", 1)[1])
    assert config == {
        "$schema": "https://opencode.ai/config.json",
        "disabled_providers": [
            "github-copilot",
            "gitlab",
            "google-vertex-anthropic",
        ],
        "permission": {
            "external_directory": {
                "/home/**": "allow",
                "/root/**": "deny",
                "/sandbox/**": "allow",
                "/tmp/**": "allow",
            },
        },
        "mcp": {
            "semble": SEMBLE_MCP,
            "context7": {
                "type": "remote",
                "url": "https://mcp.context7.com/mcp",
                "headers": {
                    "Authorization": "Bearer {env:CONTEXT7_API_KEY}",
                },
                "enabled": True,
            },
            "tavily": TAVILY_MCP,
        },
    }
    assert "mock-context7-token" not in config_value
    assert "mock-tavily-token" not in config_value  # pragma: allowlist secret


@pytest.mark.unit
def test_devbox_does_not_add_github_mcp_without_credentials(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    env.pop("CONTEXT7_API_KEY", None)
    for name in (
        "IGLOO_MCP_COMMUNITY",
        "IGLOO_MCP_COMMUNITY_KEY",
        "IGLOO_MCP_APP_PASS",
        "IGLOO_MCP_APP_ID",
        "IGLOO_MCP_USERNAME",
        "IGLOO_MCP_PASSWORD",
    ):
        env.pop(name, None)

    # Keep the test independent from any host gh login.
    fake_gh = Path(env["PATH"].split(":", 1)[0]) / "gh"
    fake_gh.write_text("#!/usr/bin/env bash\nexit 1\n")
    fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IEXEC)

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    env_values = [
        run_call[index + 1] for index, arg in enumerate(run_call[:-1]) if arg == "--env"
    ]
    config_value = next(
        value for value in env_values if value.startswith("OPENCODE_CONFIG_CONTENT=")
    )
    config = json.loads(config_value.split("=", 1)[1])
    assert config == {
        "$schema": "https://opencode.ai/config.json",
        "disabled_providers": [
            "github-copilot",
            "gitlab",
            "google-vertex-anthropic",
        ],
        "permission": {
            "external_directory": {
                "/home/**": "allow",
                "/root/**": "deny",
                "/sandbox/**": "allow",
                "/tmp/**": "allow",
            },
        },
        "mcp": {
            "semble": SEMBLE_MCP,
        },
    }


@pytest.mark.unit
def test_devbox_the_source_mcp_config(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    env.pop("CONTEXT7_API_KEY", None)
    source_credentials = {
        "IGLOO_MCP_COMMUNITY": "mock-community",
        "IGLOO_MCP_COMMUNITY_KEY": "mock-community-key",
        "IGLOO_MCP_APP_PASS": "mock-app-pass",
        "IGLOO_MCP_APP_ID": "mock-app-id",
        "IGLOO_MCP_USERNAME": "mock-username",
        "IGLOO_MCP_PASSWORD": "mock-password",  # pragma: allowlist secret
    }
    env.update(source_credentials)

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    for name, value in source_credentials.items():
        assert f"{name}={value}" in run_call  # pragma: allowlist secret

    env_values = [
        run_call[index + 1] for index, arg in enumerate(run_call[:-1]) if arg == "--env"
    ]
    config_value = next(
        value for value in env_values if value.startswith("OPENCODE_CONFIG_CONTENT=")
    )
    config = json.loads(config_value.split("=", 1)[1])
    assert config == {
        "$schema": "https://opencode.ai/config.json",
        "disabled_providers": [
            "github-copilot",
            "gitlab",
            "google-vertex-anthropic",
        ],
        "permission": {
            "external_directory": {
                "/home/**": "allow",
                "/root/**": "deny",
                "/sandbox/**": "allow",
                "/tmp/**": "allow",
            },
        },
        "mcp": {
            "semble": SEMBLE_MCP,
            "the-source": {
                "enabled": True,
                "type": "local",
                "command": [
                    "uvx",
                    "--from",
                    "git+https://github.com/johnstrunk/igloo-mcp",
                    "igloo-mcp",
                ],
                "environment": {
                    "IGLOO_MCP_COMMUNITY": "{env:IGLOO_MCP_COMMUNITY}",
                    "IGLOO_MCP_COMMUNITY_KEY": "{env:IGLOO_MCP_COMMUNITY_KEY}",
                    "IGLOO_MCP_APP_PASS": "{env:IGLOO_MCP_APP_PASS}",
                    "IGLOO_MCP_APP_ID": "{env:IGLOO_MCP_APP_ID}",
                    "IGLOO_MCP_USERNAME": "{env:IGLOO_MCP_USERNAME}",
                    "IGLOO_MCP_PASSWORD": "{env:IGLOO_MCP_PASSWORD}",
                    "IGLOO_MCP_SERVER_NAME": "The Source",
                    "IGLOO_MCP_SERVER_INSTRUCTIONS": (
                        "This server provides search and fetch capabilities for The "
                        "Source, Red Hat's intranet, containing articles with guides, "
                        "instructions, and useful information that helps team members "
                        "do their jobs and contribute to Red Hat."
                    ),
                },
            },
        },
    }
    for value in source_credentials.values():
        assert value not in config_value


@pytest.mark.unit
def test_devbox_merges_mcp_configurations(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env["GH_TOKEN"] = "mock-github-token"  # pragma: allowlist secret
    env.pop("CONTEXT7_API_KEY", None)
    env.update(
        {
            "IGLOO_MCP_COMMUNITY": "mock-community",
            "IGLOO_MCP_COMMUNITY_KEY": "mock-community-key",
            "IGLOO_MCP_APP_PASS": "mock-app-pass",
            "IGLOO_MCP_APP_ID": "mock-app-id",
            "IGLOO_MCP_USERNAME": "mock-username",
            "IGLOO_MCP_PASSWORD": "mock-password",  # pragma: allowlist secret
        }
    )

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    env_values = [
        run_call[index + 1] for index, arg in enumerate(run_call[:-1]) if arg == "--env"
    ]
    config_value = next(
        value for value in env_values if value.startswith("OPENCODE_CONFIG_CONTENT=")
    )
    config = json.loads(config_value.split("=", 1)[1])
    assert set(config["mcp"]) == {"semble", "github", "the-source"}
    assert config["mcp"]["github"] == GITHUB_MCP
    assert config["mcp"]["github"]["environment"] == {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "{env:GH_TOKEN}",
        "GITHUB_TOOLSETS": "context,repos,issues,pull_requests,users",
    }
    assert config["mcp"]["the-source"]["environment"]["IGLOO_MCP_APP_ID"] == (
        "{env:IGLOO_MCP_APP_ID}"
    )
    assert "mock-github-token" not in config_value


@pytest.mark.unit
def test_devbox_gitlab_env(devbox_path: Path, mock_podman_env, tmp_path: Path):
    env, log_file = mock_podman_env
    env["GITLAB_HOST"] = "gitlab.example.com"
    env["GITLAB_TOKEN"] = "mock-gitlab-token"  # pragma: allowlist secret

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    assert "GITLAB_HOST=gitlab.example.com" in run_call
    assert "GITLAB_TOKEN=mock-gitlab-token" in run_call  # pragma: allowlist secret


@pytest.mark.unit
@pytest.mark.parametrize(
    "anthropic_env",
    [
        {"ANTHROPIC_API_KEY": "mock-anthropic-token"},  # pragma: allowlist secret
        {"ANTHROPIC_BASE_URL": "https://anthropic.example/v1"},
        {
            "ANTHROPIC_API_KEY": "mock-anthropic-token",  # pragma: allowlist secret
            "ANTHROPIC_BASE_URL": "https://anthropic.example/v1",
        },
    ],
)
def test_devbox_anthropic_env(
    devbox_path: Path,
    mock_podman_env,
    tmp_path: Path,
    anthropic_env: dict[str, str],
):
    env, log_file = mock_podman_env
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_BASE_URL", None)
    env.update(anthropic_env)

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    for name, value in anthropic_env.items():
        assert f"{name}={value}" in run_call
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
        if name not in anthropic_env:
            assert not any(arg.startswith(f"{name}=") for arg in run_call)


@pytest.mark.unit
def test_devbox_pricetag_env_and_provider_config(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env["PRICETAG_ANTHROPIC_URL"] = "https://pricetag-anthropic.example/v1"
    env["PRICETAG_HOSTED_URL"] = "https://pricetag-hosted.example/v1"
    env["PRICETAG_OPENAI_URL"] = "https://pricetag-openai.example/v1"
    env["PRICETAG_API_KEY"] = "mock-pricetag-token"  # pragma: allowlist secret

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    assert "PRICETAG_ANTHROPIC_URL=https://pricetag-anthropic.example/v1" in run_call
    assert "PRICETAG_HOSTED_URL=https://pricetag-hosted.example/v1" in run_call
    assert "PRICETAG_OPENAI_URL=https://pricetag-openai.example/v1" in run_call
    pricetag_api_key_arg = (
        "PRICETAG_API_KEY=mock-pricetag-token"  # pragma: allowlist secret
    )
    assert pricetag_api_key_arg in run_call

    env_values = [
        run_call[index + 1] for index, arg in enumerate(run_call[:-1]) if arg == "--env"
    ]
    config_value = next(
        value for value in env_values if value.startswith("OPENCODE_CONFIG_CONTENT=")
    )
    config = json.loads(config_value.split("=", 1)[1])
    expected_variants = {
        "low": {"effort": "low"},
        "medium": {"effort": "medium"},
        "xhigh": {"effort": "xhigh"},
    }
    expected_models = {
        "Inferact/Qwen3.8-Flash-Next-NVFP4": {
            "name": "Qwen 3.8 Flash Next (hosted, $0.15/$0.47 per MTok)",
            "limit": {"context": 262144, "output": 128000},
            "reasoning": True,
            "modalities": {
                "input": ["text", "image"],
                "output": ["text"],
            },
            "variants": expected_variants,
        },
        "rits/zai-org/glm-5-3": {
            "name": "GLM 5.3 (hosted via curvebender)",
            "limit": {"context": 262144, "output": 128000},
            "reasoning": True,
            "modalities": {
                "input": ["text"],
                "output": ["text"],
            },
            "variants": expected_variants,
        },
    }
    assert config["provider"] == {
        "anthropic": {
            "options": {
                "baseURL": "{env:PRICETAG_ANTHROPIC_URL}",
                "apiKey": "{env:PRICETAG_API_KEY}",
            },
        },
        "pricetag-hosted": {
            "npm": "@ai-sdk/anthropic",
            "name": "PriceTag (Hosted)",
            "options": {
                "baseURL": "{env:PRICETAG_HOSTED_URL}",
                "apiKey": "{env:PRICETAG_API_KEY}",
            },
            "models": expected_models,
        },
        "openai": {
            "options": {
                "baseURL": "{env:PRICETAG_OPENAI_URL}",
                "apiKey": "{env:PRICETAG_API_KEY}",
            },
        },
    }
    assert not (tmp_path / "pricetag_curl_calls.log").exists()
    assert "mock-pricetag-token" not in config_value


@pytest.mark.unit
def test_devbox_octo_open_env_and_provider_config(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env["OCTO_OPEN_URL"] = "https://octo-gateway.example/v1"
    env["OCTO_OPEN_KEY"] = "mock-octo-token"  # pragma: allowlist secret

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    assert "OCTO_OPEN_URL=https://octo-gateway.example/v1" in run_call
    assert "OCTO_OPEN_KEY=mock-octo-token" in run_call  # pragma: allowlist secret

    env_values = [
        run_call[index + 1] for index, arg in enumerate(run_call[:-1]) if arg == "--env"
    ]
    config_value = next(
        value for value in env_values if value.startswith("OPENCODE_CONFIG_CONTENT=")
    )
    config = json.loads(config_value.split("=", 1)[1])
    assert config["provider"]["octo-open"] == {
        "npm": "@ai-sdk/openai-compatible",
        "name": "OCTO Open Models",
        "options": {
            "baseURL": "{env:OCTO_OPEN_URL}",
            "apiKey": "{env:OCTO_OPEN_KEY}",
        },
        "models": {
            "qwen38-27b-frontier": {
                "name": "Qwen 3.8 27B FP8 (Frontier)",
                "limit": {"context": 131072, "output": 8192},
                "tool_call": True,
                "reasoning": True,
                "temperature": True,
            },
            "qwen38-flash-next": {
                "name": "Qwen 3.8 Flash Next NVFP4 (Core)",
                "limit": {"context": 262144, "output": 8192},
                "tool_call": True,
                "reasoning": True,
                "temperature": True,
            },
            "qwen38-27b-fast": {
                "name": "Qwen 3.8 27B NVFP4 (Bulk)",
                "limit": {"context": 32768, "output": 8192},
                "tool_call": True,
                "reasoning": True,
                "temperature": True,
            },
        },
    }
    assert "mock-octo-token" not in config_value  # pragma: allowlist secret


@pytest.mark.unit
@pytest.mark.parametrize("missing", ["OCTO_OPEN_URL", "OCTO_OPEN_KEY"])
def test_devbox_does_not_add_octo_open_without_both_credentials(
    devbox_path: Path,
    mock_podman_env,
    tmp_path: Path,
    missing: str,
):
    env, log_file = mock_podman_env
    env["OCTO_OPEN_URL"] = "https://octo-gateway.example/v1"
    env["OCTO_OPEN_KEY"] = "mock-octo-token"  # pragma: allowlist secret
    env.pop(missing)

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    assert not any(arg.startswith("OCTO_OPEN_") for arg in run_call)

    env_values = [
        run_call[index + 1] for index, arg in enumerate(run_call[:-1]) if arg == "--env"
    ]
    config_value = next(
        value for value in env_values if value.startswith("OPENCODE_CONFIG_CONTENT=")
    )
    config = json.loads(config_value.split("=", 1)[1])
    assert "octo-open" not in config.get("provider", {})


@pytest.mark.unit
def test_devbox_pricetag_builtin_provider_override_does_not_discover_models(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env["PRICETAG_OPENAI_URL"] = "https://pricetag-openai.example/v1"
    env["PRICETAG_API_KEY"] = "mock-pricetag-token"  # pragma: allowlist secret

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0
    assert "could not discover PriceTag" not in res.stderr

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    env_values = [
        run_call[index + 1] for index, arg in enumerate(run_call[:-1]) if arg == "--env"
    ]
    config_value = next(
        value for value in env_values if value.startswith("OPENCODE_CONFIG_CONTENT=")
    )
    config = json.loads(config_value.split("=", 1)[1])
    assert config["provider"]["openai"] == {
        "options": {
            "baseURL": "{env:PRICETAG_OPENAI_URL}",
            "apiKey": "{env:PRICETAG_API_KEY}",
        }
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    ("url_name", "missing"),
    [
        ("PRICETAG_ANTHROPIC_URL", "PRICETAG_ANTHROPIC_URL"),
        ("PRICETAG_HOSTED_URL", "PRICETAG_HOSTED_URL"),
        ("PRICETAG_OPENAI_URL", "PRICETAG_OPENAI_URL"),
        ("PRICETAG_OPENAI_URL", "PRICETAG_API_KEY"),
    ],
)
def test_devbox_does_not_add_pricetag_without_both_credentials(
    devbox_path: Path,
    mock_podman_env,
    tmp_path: Path,
    url_name: str,
    missing: str,
):
    env, log_file = mock_podman_env
    env[url_name] = "https://pricetag.example/v1"
    env["PRICETAG_API_KEY"] = "mock-pricetag-token"  # pragma: allowlist secret
    env.pop(missing)

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    assert not any(arg.startswith("PRICETAG_") for arg in run_call)

    env_values = [
        run_call[index + 1] for index, arg in enumerate(run_call[:-1]) if arg == "--env"
    ]
    config_value = next(
        value for value in env_values if value.startswith("OPENCODE_CONFIG_CONTENT=")
    )
    config = json.loads(config_value.split("=", 1)[1])
    assert "provider" not in config


@pytest.mark.unit
def test_devbox_vertex_env(devbox_path: Path, mock_podman_env, tmp_path: Path):
    env, log_file = mock_podman_env
    env["GOOGLE_CLOUD_PROJECT"] = "my-gcp-project"
    env["VERTEX_LOCATION"] = "us-central1"

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    assert "GOOGLE_CLOUD_PROJECT=my-gcp-project" in run_call
    assert "VERTEX_LOCATION=us-central1" in run_call  # pragma: allowlist secret


@pytest.mark.unit
def test_devbox_config_volume_mounts(
    devbox_path: Path,
    mock_podman_env,
    tmp_path: Path,
):
    env, log_file = mock_podman_env
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    env["HOME"] = str(fake_home)
    env.pop("XDG_DATA_HOME", None)

    (fake_home / ".config" / "acli").mkdir(parents=True)
    (fake_home / ".config" / "gws").mkdir(parents=True)
    (fake_home / ".config" / "opencode").mkdir(parents=True)
    (fake_home / ".agents").mkdir()
    expected_data_dir = fake_home / ".local" / "share" / "opencode"
    expected_data_dir.mkdir(parents=True)

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    volumes = [run_call[i + 1] for i, arg in enumerate(run_call) if arg == "--volume"]
    assert any(":/sandbox/.config/acli" in v for v in volumes)
    assert any(":/sandbox/.config/gws" in v for v in volumes)
    assert any(":/sandbox/.config/opencode" in v for v in volumes)
    assert any(f"{fake_home / '.agents'}:/sandbox/.agents" in v for v in volumes)
    assert any(
        f"{expected_data_dir}:/sandbox/.local/share/opencode" in v for v in volumes
    )

    env_values = [
        run_call[index + 1] for index, arg in enumerate(run_call[:-1]) if arg == "--env"
    ]
    config_value = next(
        value for value in env_values if value.startswith("OPENCODE_CONFIG_CONTENT=")
    )
    config = json.loads(config_value.split("=", 1)[1])
    assert "plugin" not in config


@pytest.mark.unit
def test_devbox_does_not_mount_missing_global_agents_directory(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    env["HOME"] = str(fake_home)

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    volumes = [run_call[i + 1] for i, arg in enumerate(run_call) if arg == "--volume"]
    assert not any(":/sandbox/.agents" in v for v in volumes)
    assert not (fake_home / ".agents").exists()


@pytest.mark.unit
def test_devbox_opencode_data_volume_ignores_xdg_data_home(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    xdg_data_home = tmp_path / "xdg-data"
    env["HOME"] = str(fake_home)
    env["XDG_DATA_HOME"] = str(xdg_data_home)
    expected_data_dir = fake_home / ".local" / "share" / "opencode"
    expected_data_dir.mkdir(parents=True)

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    assert not xdg_data_home.exists()
    assert any(
        f"{expected_data_dir}:/sandbox/.local/share/opencode" in v for v in run_call
    )


@pytest.mark.unit
def test_devbox_persistent_cache_volumes(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    env["HOME"] = str(fake_home)
    env.pop("XDG_CACHE_HOME", None)

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    volumes = [run_call[i + 1] for i, arg in enumerate(run_call) if arg == "--volume"]

    # The knowledge base, Go, uv/pre-commit, and Semble caches are shared (not
    # per-directory) named volumes.
    assert "devbox-kb:/sandbox/kb" in volumes
    assert "devbox-go-cache:/sandbox/.cache/go" in volumes
    assert "devbox-uv-cache:/sandbox/.uv_cache" in volumes
    assert "devbox-precommit-cache:/sandbox/.cache/pre-commit" in volumes
    assert "devbox-semble-cache:/sandbox/.cache/semble" in volumes

    # Nested Podman/Buildah storage is host-backed so its size is easy to
    # manage, and the host directory is created ahead of time.
    expected_storage_dir = fake_home / ".cache" / "devbox" / "containers-storage"
    assert expected_storage_dir.is_dir()
    assert any(
        f"{expected_storage_dir}:/sandbox/.local/share/containers/storage" in v
        for v in volumes
    )


@pytest.mark.unit
def test_devbox_persistent_cache_volume_respects_xdg_cache_home(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    xdg_cache_home = tmp_path / "xdg-cache"
    env["HOME"] = str(fake_home)
    env["XDG_CACHE_HOME"] = str(xdg_cache_home)

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None

    expected_storage_dir = xdg_cache_home / "devbox" / "containers-storage"
    assert expected_storage_dir.is_dir()
    assert not (fake_home / ".cache").exists()
    assert any(
        f"{expected_storage_dir}:/sandbox/.local/share/containers/storage" in v
        for v in run_call
    )


def _git_config_set_calls(calls: list[list[str]], key: str) -> list[list[str]]:
    return [
        c
        for c in calls
        if c[:5] == ["exec", c[1], "git", "config", "--global"]
        and len(c) >= 6
        and c[5] == key
        and len(c) >= 7
    ]


def _github_https_rewrite_calls(calls: list[list[str]]) -> list[list[str]]:
    return [
        c
        for c in calls
        if len(c) == 8
        and c[0] == "exec"
        and c[2:7]
        == [
            "git",
            "config",
            "--global",
            "--add",
            "url.https://github.com/.insteadOf",
        ]
    ]


@pytest.mark.unit
def test_devbox_rewrites_github_ssh_remotes_to_https(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env["GH_TOKEN"] = "mock-github-token"  # pragma: allowlist secret
    host_gitconfig = tmp_path / "host.gitconfig"
    host_gitconfig.write_text("[user]\n\tname = Host User\n", encoding="utf-8")
    env["GIT_CONFIG_GLOBAL"] = str(host_gitconfig)
    host_config_before = host_gitconfig.read_bytes()

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0
    assert host_gitconfig.read_bytes() == host_config_before

    calls = parse_podman_calls(log_file)
    rewrite_calls = _github_https_rewrite_calls(calls)
    assert [call[7] for call in rewrite_calls] == [
        "git@github.com:",
        "ssh://git@github.com/",
    ]

    # Apply the actual container Git config commands to an isolated config and
    # verify Git resolves existing remotes to HTTPS without changing their
    # stored URLs or rewriting unrelated SSH hosts.
    container_gitconfig = tmp_path / "container.gitconfig"
    git_env = env.copy()
    git_env["GIT_CONFIG_GLOBAL"] = str(container_gitconfig)
    git_env["GIT_CONFIG_NOSYSTEM"] = "1"
    for call in rewrite_calls:
        subprocess.run(
            call[2:],
            env=git_env,
            check=True,
            capture_output=True,
            text=True,
        )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", str(repo_dir)],
        env=git_env,
        check=True,
        capture_output=True,
        text=True,
    )
    remotes = {
        "github-scp": (
            "git@github.com:owner/repo.git",
            "https://github.com/owner/repo.git",
        ),
        "github-ssh": (
            "ssh://git@github.com/owner/repo.git",
            "https://github.com/owner/repo.git",
        ),
        "gitlab": (
            "git@gitlab.com:owner/repo.git",
            "git@gitlab.com:owner/repo.git",
        ),
    }
    for name, (remote_url, expected_url) in remotes.items():
        subprocess.run(
            ["git", "-C", str(repo_dir), "remote", "add", name, remote_url],
            env=git_env,
            check=True,
            capture_output=True,
            text=True,
        )
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "remote", "get-url", name],
            env=git_env,
            check=True,
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == expected_url

    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_dir),
            "config",
            "--local",
            "--get",
            "remote.github-scp.url",
        ],
        env=git_env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "git@github.com:owner/repo.git"


@pytest.mark.unit
def test_devbox_configures_git_identity_from_host_config(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    host_gitconfig = tmp_path / "host_gitconfig"
    env["GIT_CONFIG_GLOBAL"] = str(host_gitconfig)
    env.pop("MOCK_CONTAINER_GIT_NAME", None)
    env.pop("MOCK_CONTAINER_GIT_EMAIL", None)
    subprocess.run(
        ["git", "config", "--global", "user.name", "Host User"],
        env=env,
        check=True,
    )
    subprocess.run(
        ["git", "config", "--global", "user.email", "host-user@example.com"],
        env=env,
        check=True,
    )

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0
    assert "not configured" not in res.stderr

    calls = parse_podman_calls(log_file)
    name_calls = _git_config_set_calls(calls, "user.name")
    email_calls = _git_config_set_calls(calls, "user.email")
    assert any(c[6] == "Host User" for c in name_calls)
    assert any(c[6] == "host-user@example.com" for c in email_calls)


@pytest.mark.unit
def test_devbox_warns_when_no_git_identity_available(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env["GIT_CONFIG_GLOBAL"] = str(tmp_path / "nonexistent_gitconfig")
    env.pop("MOCK_CONTAINER_GIT_NAME", None)
    env.pop("MOCK_CONTAINER_GIT_EMAIL", None)

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0
    assert "not configured" in res.stderr
    assert 'git config --global user.name "Your Name"' in res.stderr

    calls = parse_podman_calls(log_file)
    assert _git_config_set_calls(calls, "user.name") == []
    assert _git_config_set_calls(calls, "user.email") == []


@pytest.mark.unit
def test_devbox_preserves_existing_container_git_identity(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    host_gitconfig = tmp_path / "host_gitconfig"
    env["GIT_CONFIG_GLOBAL"] = str(host_gitconfig)
    subprocess.run(
        ["git", "config", "--global", "user.name", "Host User"],
        env=env,
        check=True,
    )
    subprocess.run(
        ["git", "config", "--global", "user.email", "host-user@example.com"],
        env=env,
        check=True,
    )
    env["MOCK_CONTAINER_GIT_NAME"] = "Container User"
    env["MOCK_CONTAINER_GIT_EMAIL"] = "container-user@example.com"

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0
    assert "not configured" not in res.stderr

    calls = parse_podman_calls(log_file)
    # The container's existing identity must never be overwritten with the
    # (different) host identity.
    assert _git_config_set_calls(calls, "user.name") == []
    assert _git_config_set_calls(calls, "user.email") == []


@pytest.mark.unit
def test_devbox_runs_gh_auth_setup_git_when_token_available(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env["GH_TOKEN"] = "mock-github-token"  # pragma: allowlist secret

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    setup_git_calls = [
        c for c in calls if c[:5] == ["exec", c[1], "gh", "auth", "setup-git"]
    ]
    assert len(setup_git_calls) == 1
    assert setup_git_calls[0][5:] == ["--hostname", "github.com", "--force"]


@pytest.mark.unit
def test_devbox_warns_when_no_github_token_for_setup_git(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0
    assert "authenticated Git operations against github.com" in res.stderr
    assert "gh auth login && gh auth setup-git" in res.stderr

    calls = parse_podman_calls(log_file)
    assert not any(c[:5] == ["exec", c[1], "gh", "auth", "setup-git"] for c in calls)
    assert [call[7] for call in _github_https_rewrite_calls(calls)] == [
        "git@github.com:",
        "ssh://git@github.com/",
    ]


@pytest.mark.unit
def test_devbox_warns_when_github_https_rewrite_fails(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, _ = mock_podman_env
    env["MOCK_GIT_CONFIG_SET_FAILS"] = "1"

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0
    assert res.stderr.count("Failed to configure GitHub HTTPS URL rewrite") == 2


@pytest.mark.unit
def test_devbox_warns_when_gh_auth_setup_git_fails(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env["GH_TOKEN"] = "mock-github-token"  # pragma: allowlist secret
    env["MOCK_GH_AUTH_SETUP_GIT_FAILS"] = "1"

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0
    assert "'gh auth setup-git' failed" in res.stderr


@pytest.mark.unit
def test_devbox_survives_git_config_set_failure(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    # A transient failure setting the Git identity inside the container
    # (e.g. a flaky `podman exec`) must not abort the whole launch under
    # `set -euo pipefail`; it should be reported as a warning and the
    # launcher should still finish entering the container.
    env, _ = mock_podman_env
    host_gitconfig = tmp_path / "host_gitconfig"
    env["GIT_CONFIG_GLOBAL"] = str(host_gitconfig)
    subprocess.run(
        ["git", "config", "--global", "user.name", "Host User"],
        env=env,
        check=True,
    )
    subprocess.run(
        ["git", "config", "--global", "user.email", "host-user@example.com"],
        env=env,
        check=True,
    )
    env.pop("MOCK_CONTAINER_GIT_NAME", None)
    env.pop("MOCK_CONTAINER_GIT_EMAIL", None)
    env["MOCK_GIT_CONFIG_SET_FAILS"] = "1"
    env["MOCK_OPENCODE_REFRESH_FAILS"] = "1"

    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0, res.stderr
    assert "Failed to set Git user.name" in res.stderr
    assert "Failed to set Git user.email" in res.stderr
    assert "OpenCode model catalog refresh command failed" in res.stderr
    assert "Entering container" in res.stdout


@pytest.mark.unit
def test_devbox_requests_nested_bridge_sysctls(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_calls = [c for c in calls if c and c[0] == "run" and "-d" in c]
    assert len(run_calls) == 1
    run_call = run_calls[0]
    for sysctl in (
        "net.ipv4.conf.default.route_localnet=1",
        "net.ipv4.conf.default.arp_notify=1",
        "net.ipv4.conf.default.rp_filter=2",
        "net.ipv4.ip_forward=1",
        "net.ipv6.conf.default.accept_dad=0",
        "net.ipv6.conf.default.accept_ra=0",
        "net.ipv6.conf.all.forwarding=1",
    ):
        assert sysctl in run_call
    assert any(
        "containers.conf" in arg and 'netns = "bridge"' in " ".join(call)
        for call in calls
        if call and call[0] == "exec"
        for arg in call
    )
    assert any(
        "podman" in call and "--rootless-netns" in call for call in calls if call
    )
    assert any(
        "podman" in call and "network" in call and "create" in call
        for call in calls
        if call
    )
    assert any(
        "podman" in call
        and "create" in call
        and "--network" in call
        and "--publish" in call
        for call in calls
        if call
    )


@pytest.mark.unit
def test_devbox_retries_without_nested_bridge_sysctls_on_rejection(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env["MOCK_REJECT_NESTED_SYSCTLS"] = "1"
    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0, res.stderr
    assert "retrying without them" in res.stderr
    assert "user-defined bridge networks" in res.stderr

    run_calls = [
        c for c in parse_podman_calls(log_file) if c and c[0] == "run" and "-d" in c
    ]
    assert len(run_calls) == 2
    assert any("--sysctl" in c for c in run_calls[:1])
    assert not any("--sysctl" in c for c in run_calls[1:])
    assert not any(
        "containers.conf" in arg and 'netns = "bridge"' in " ".join(call)
        for call in parse_podman_calls(log_file)
        if call and call[0] == "exec"
        for arg in call
    )


@pytest.mark.unit
def test_devbox_does_not_mask_unrelated_container_create_failure(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env["MOCK_FAIL_CONTAINER_RUN"] = "1"
    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode != 0
    assert "image create failed" in res.stderr

    run_calls = [
        c for c in parse_podman_calls(log_file) if c and c[0] == "run" and "-d" in c
    ]
    assert len(run_calls) == 1


@pytest.mark.unit
def test_devbox_does_not_treat_unrelated_sysctl_error_as_fallback(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env["MOCK_FAIL_UNRELATED_SYSCTL"] = "1"
    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode != 0
    assert "storage setup failed" in res.stderr
    assert "retrying without them" not in res.stderr

    run_calls = [
        c for c in parse_podman_calls(log_file) if c and c[0] == "run" and "-d" in c
    ]
    assert len(run_calls) == 1


@pytest.mark.unit
def test_devbox_retries_without_sysctls_on_ipv6_sysctl_rejection(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env["MOCK_REJECT_IPV6_SYSCTL"] = "1"
    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0
    assert "retrying without them" in res.stderr

    run_calls = [
        c for c in parse_podman_calls(log_file) if c and c[0] == "run" and "-d" in c
    ]
    assert len(run_calls) == 2
    assert not any("--sysctl" in c for c in run_calls[1:])


@pytest.mark.unit
def test_devbox_retries_without_sysctls_on_ipv6_forwarding_rejection(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env["MOCK_REJECT_IPV6_FORWARDING"] = "1"
    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0
    assert "retrying without them" in res.stderr

    run_calls = [
        c for c in parse_podman_calls(log_file) if c and c[0] == "run" and "-d" in c
    ]
    assert len(run_calls) == 2
    assert not any("--sysctl" in c for c in run_calls[1:])


@pytest.mark.unit
def test_devbox_removes_container_when_subid_setup_fails(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env["MOCK_UID_MAP_FAIL"] = "1"
    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode != 0

    calls = parse_podman_calls(log_file)
    assert any(c[:2] == ["rm", "-f"] for c in calls)


@pytest.mark.unit
def test_devbox_reports_container_removal_failure(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, _ = mock_podman_env
    env["MOCK_CONTAINER_EXISTS"] = "1"
    env["MOCK_RM_FAIL"] = "1"
    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["--recreate", "true"], env=env, cwd=run_dir)
    assert res.returncode != 0
    assert "failed to remove container" in res.stderr


@pytest.mark.unit
def test_devbox_reports_container_exists_probe_failure(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, _ = mock_podman_env
    env["MOCK_CONTAINER_EXISTS_ERROR"] = "1"
    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["--remove"], env=env, cwd=run_dir)
    assert res.returncode != 0
    assert "could not determine whether container" in res.stderr


@pytest.mark.unit
def test_devbox_sets_docker_host_ready_marker_env(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    assert "DEVBOX_SUBID_READY_FILE=/sandbox/.devbox-subids-ready" in run_call

    exec_calls = [c for c in calls if c and c[0] == "exec"]
    assert any(
        "docker.sock" in arg and "_ping" in " ".join(c) for c in exec_calls for arg in c
    )


@pytest.mark.unit
def test_devbox_warns_when_docker_api_never_ready(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    env["MOCK_DOCKER_API_NEVER_READY"] = "1"
    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir, timeout=60)
    assert res.returncode == 0, res.stderr
    assert "Docker API did not become ready" in res.stderr


@pytest.mark.unit
def test_devbox_mounts_git_directory_for_linked_worktree(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    # Issue #148: a linked worktree's `.git` pointer file references a
    # host-only gitdir path, so the launcher must additionally mount the
    # repository's git directory at the same path inside the container.
    env, log_file = mock_podman_env
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repository(repo)
    worktree = tmp_path / "linked-worktree"
    _run_git(["worktree", "add", "-b", "feature", str(worktree)], repo)

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=worktree)
    assert res.returncode == 0, res.stderr
    assert "Linked git worktree detected" in res.stdout

    volumes = _create_container_volumes(log_file)
    _assert_host_git_mount(volumes, repo / ".git")
    assert any(str(worktree.resolve()) in volume for volume in volumes)


@pytest.mark.unit
def test_devbox_mounts_separate_git_dir_without_commondir(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    # A `--separate-git-dir` style pointer (no `commondir` file) still names
    # the git directory that must be mounted for Git to work in the mount.
    env, log_file = mock_podman_env
    gitdir = tmp_path / "external-git-dir"
    gitdir.mkdir()
    (gitdir / "HEAD").write_text("ref: refs/heads/main\n")
    project = _write_dot_git_pointer(tmp_path, gitdir)

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=project)
    assert res.returncode == 0, res.stderr
    assert "Linked git worktree detected" in res.stdout

    volumes = _create_container_volumes(log_file)
    _assert_host_git_mount(volumes, gitdir)


@pytest.mark.unit
def test_devbox_no_extra_mount_for_normal_checkout(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repository(repo)

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=repo)
    assert res.returncode == 0, res.stderr
    assert "Linked git worktree detected" not in res.stdout

    volumes = _create_container_volumes(log_file)
    _assert_no_host_git_mount(volumes, repo / ".git")


@pytest.mark.unit
def test_devbox_no_extra_mount_for_git_pointer_inside_worktree(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    # Relative `.git` pointer files (e.g. submodule style) that resolve to a
    # path inside the bind mount stay resolvable in the container, so no
    # extra mount is needed.
    env, log_file = mock_podman_env
    inner = tmp_path / "project" / "inner"
    inner.mkdir(parents=True)
    _init_repository(inner)
    project = inner.parent
    (project / ".git").write_text("gitdir: inner/.git\n")

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=project)
    assert res.returncode == 0, res.stderr
    assert "Linked git worktree detected" not in res.stdout

    volumes = _create_container_volumes(log_file)
    _assert_no_host_git_mount(volumes, inner / ".git")


@pytest.mark.unit
def test_devbox_ignores_malformed_dot_git_pointer(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").write_text("not a gitdir pointer\n")

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=project)
    assert res.returncode == 0, res.stderr
    assert "Linked git worktree detected" not in res.stdout

    volumes = _create_container_volumes(log_file)
    _assert_no_host_git_mount(volumes, project / ".git")


@pytest.mark.unit
def test_devbox_refuses_to_mount_non_git_dir_from_dot_git_pointer(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    # The `.git` pointer is repository-controlled content, so a crafted
    # pointer must never turn an arbitrary host directory (e.g. ~/.ssh)
    # into a container mount.
    env, log_file = mock_podman_env
    outside = tmp_path / "not-a-git-dir"
    outside.mkdir()
    (outside / "id_ed25519").write_text("secret key material")
    project = _write_dot_git_pointer(tmp_path, outside)

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=project)
    assert res.returncode == 0, res.stderr
    assert "Linked git worktree detected" not in res.stdout

    volumes = _create_container_volumes(log_file)
    _assert_no_host_git_mount(volumes, outside)
    assert not any(str(outside.resolve()) in volume for volume in volumes)


def test_devbox_shadows_host_venv_with_container_volume(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    # Issue #154: a host-created .venv contains host-only interpreter paths,
    # so the launcher must shadow it with a container-local named volume
    # instead of letting uv run its broken shebangs.
    env, log_file = mock_podman_env
    run_dir = tmp_path / "workdir"
    (run_dir / ".venv" / "bin").mkdir(parents=True)
    (run_dir / ".venv" / "bin" / "pytest").write_text(
        "#!/nonexistent/host/python\nraise SystemExit\n"
    )

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0, res.stderr
    assert "Shadowing host .venv" in res.stdout

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    volumes = [run_call[i + 1] for i, arg in enumerate(run_call) if arg == "--volume"]
    assert f"devbox-venv-{run_dir.name}:/sandbox/{run_dir.name}/.venv" in volumes


@pytest.mark.unit
def test_devbox_no_venv_volume_when_host_venv_absent(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    # Projects without a .venv must be untouched: no shadow volume, and no
    # mount-point directory is ever created in the host project.
    env, log_file = mock_podman_env
    run_dir = tmp_path / "workdir"
    run_dir.mkdir()

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0, res.stderr
    assert not (run_dir / ".venv").exists()

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    volumes = [run_call[i + 1] for i, arg in enumerate(run_call) if arg == "--volume"]
    assert not any("/.venv" in volume for volume in volumes)


@pytest.mark.unit
def test_devbox_warns_on_symlinked_host_venv(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    # A symlinked .venv cannot be shadowed safely (the mount target would not
    # resolve inside the container), so the launcher warns and skips it.
    env, log_file = mock_podman_env
    run_dir = tmp_path / "workdir"
    run_dir.mkdir()
    outside = tmp_path / "shared-env"
    outside.mkdir()
    run_dir.joinpath(".venv").symlink_to(outside)

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0, res.stderr
    assert "cannot be shadowed" in res.stderr

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    volumes = [run_call[i + 1] for i, arg in enumerate(run_call) if arg == "--volume"]
    assert not any("/.venv" in volume for volume in volumes)


@pytest.mark.unit
def test_devbox_warns_on_non_directory_host_venv(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, log_file = mock_podman_env
    run_dir = tmp_path / "workdir"
    run_dir.mkdir()
    (run_dir / ".venv").write_text("not a virtualenv\n")

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0, res.stderr
    assert "not a directory" in res.stderr

    calls = parse_podman_calls(log_file)
    run_call = next((c for c in calls if c and c[0] == "run" and "-d" in c), None)
    assert run_call is not None
    volumes = [run_call[i + 1] for i, arg in enumerate(run_call) if arg == "--volume"]
    assert not any("/.venv" in volume for volume in volumes)


@pytest.mark.unit
def test_devbox_warns_when_existing_container_lacks_venv_shadow(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    # A container created before the project grew a host .venv (or before
    # this feature existed) has no shadow mount; entering it must warn
    # instead of silently running uv against host-only interpreter paths.
    env, _ = mock_podman_env
    env["MOCK_CONTAINER_EXISTS"] = "1"
    run_dir = tmp_path / "workdir"
    (run_dir / ".venv").mkdir(parents=True)

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0, res.stderr
    assert "does not shadow the host .venv" in res.stderr


@pytest.mark.unit
def test_devbox_no_shadow_warning_when_container_has_venv_mount(
    devbox_path: Path, mock_podman_env, tmp_path: Path
):
    env, _ = mock_podman_env
    env["MOCK_CONTAINER_EXISTS"] = "1"
    env["MOCK_VENV_SHADOW_MOUNT"] = "1"
    run_dir = tmp_path / "workdir"
    (run_dir / ".venv").mkdir(parents=True)

    res = run_bash_script(devbox_path, ["true"], env=env, cwd=run_dir)
    assert res.returncode == 0, res.stderr
    assert "does not shadow the host .venv" not in res.stderr
