import json
import subprocess
import uuid
from pathlib import Path

import pytest

from tests.conftest import run_in_devbox

BINARIES = [
    ("go", ["go", "version"]),
    ("kind", ["kind", "version"]),
    ("kubectl", ["kubectl", "version", "--client=true"]),
    ("devbox-kind", ["devbox-kind", "--help"]),
    ("devbox-go", ["devbox-go", "--help"]),
    ("rustc", ["rustc", "--version"]),
    ("cargo", ["cargo", "--version"]),
    ("rustup", ["rustup", "--version"]),
    ("uv", ["uv", "--version"]),
    ("uvx", ["uvx", "--version"]),
    ("pre-commit", ["pre-commit", "--version"]),
    ("node", ["node", "--version"]),
    ("npm", ["npm", "--version"]),
    ("npx", ["npx", "--version"]),
    ("playwright-cli", ["playwright-cli", "--version"]),
    ("gh", ["gh", "--version"]),
    ("glab", ["glab", "--version"]),
    ("gcloud", ["gcloud", "--version"]),
    ("gws", ["gws", "--help"]),
    ("acli", ["acli", "--version"]),
    ("agy", ["agy", "--help"]),
    ("opencode", ["opencode", "--version"]),
    ("github-mcp-server", ["github-mcp-server", "--help"]),
    ("github-mcp-server-proxy", ["github-mcp-server-proxy", "--help"]),
    ("repomix", ["repomix", "--version"]),
    ("ripwire", ["ripwire", "--version"]),
    ("ast-grep", ["ast-grep", "--version"]),
    ("semble", ["semble", "--version"]),
    ("tokei", ["tokei", "--version"]),
    ("just", ["just", "--version"]),
    ("difft", ["difft", "--version"]),
    ("hyperfine", ["hyperfine", "--version"]),
    ("fd", ["fd", "--version"]),
    ("which", ["which", "ripwire"]),
    ("rg", ["rg", "--version"]),
    ("jq", ["jq", "--version"]),
    ("shellcheck", ["shellcheck", "--version"]),
    ("hadolint", ["hadolint", "--version"]),
    ("markdownlint-cli2", ["markdownlint-cli2", "--help"]),
    ("ffmpeg", ["ffmpeg", "-version"]),
    ("ps", ["ps", "--version"]),
    ("pgrep", ["pgrep", "--version"]),
    ("podman", ["podman", "--version"]),
    ("pasta", ["pasta", "--version"]),
    ("devbox-docker-api-check", ["devbox-docker-api-check", "--help"]),
]


@pytest.mark.container
@pytest.mark.parametrize("binary_name,cmd", BINARIES)
def test_container_binary_presence_and_execution(
    devbox_image: str, binary_name: str, cmd: list[str]
):
    res = run_in_devbox(devbox_image, cmd, user="sandbox")
    # markdownlint-cli2 exits with code 2 on --help while outputting syntax
    valid_returncodes = (0, 2) if binary_name == "markdownlint-cli2" else (0,)
    assert res.returncode in valid_returncodes, (
        f"Command '{' '.join(cmd)}' failed with code {res.returncode}.\n"
        f"Stdout: {res.stdout}\nStderr: {res.stderr}"
    )


@pytest.mark.container
def test_pre_commit_hooks_bootstrap_from_empty_cache(
    devbox_image: str, repo_root: Path
):
    command = r"""
set -euo pipefail
fixture="$(mktemp -d)"
trap 'rm -rf -- "$fixture"' EXIT
tar \
  --exclude='./.git' \
  --exclude='./.venv' \
  --exclude='./.pytest_cache' \
  --exclude='./.ruff_cache' \
  --exclude='./.ripwire_quality_baseline' \
  -C /workspace -cf - . | tar -xf - -C "$fixture"
git -C "$fixture" init -q
git -C "$fixture" add --all
cd "$fixture"
PRE_COMMIT_HOME="$fixture/pre-commit-home" pre-commit run --all-files
"""
    res = run_in_devbox(
        devbox_image,
        ["bash", "-ceu", command],
        user="sandbox",
        volumes=[f"{repo_root}:/workspace:ro"],
        timeout=300,
    )
    assert res.returncode == 0, (
        "Pre-commit hooks could not initialize from an empty cache in the devbox.\n"
        f"Stdout: {res.stdout}\nStderr: {res.stderr}"
    )


@pytest.mark.container
def test_repomix_version_matches_manifest(devbox_image: str, repo_root: Path):
    manifest = json.loads((repo_root / "container" / "tool-versions.json").read_text())
    expected_version = manifest["tools"]["repomix"]["version"]

    res = run_in_devbox(devbox_image, ["repomix", "--version"], user="sandbox")

    assert res.returncode == 0, (
        f"Repomix version check failed.\nStdout: {res.stdout}\nStderr: {res.stderr}"
    )
    assert res.stdout.strip() == expected_version


@pytest.mark.container
def test_project_go_toolchain_selector(devbox_image: str):
    res = run_in_devbox(
        devbox_image,
        [
            "bash",
            "-ceu",
            r"""
fixture="$(mktemp -d)"
trap 'rm -rf "$fixture"' EXIT
printf '%s\n' 'module example.test/project' '' 'go 1.26.0' > "$fixture/go.mod"
output="$(cd "$fixture" && devbox-go --doctor)"
grep -F 'Selected toolchain: go1.26.0' <<<"$output"
grep -F 'GOTOOLCHAIN: go1.26.0+auto' <<<"$output"
""",
        ],
        user="sandbox",
    )
    assert res.returncode == 0, (
        "devbox-go did not select the project toolchain.\n"
        f"Stdout: {res.stdout}\nStderr: {res.stderr}"
    )

    cache_volume = f"devbox-go-test-{uuid.uuid4().hex}"
    subprocess.run(
        ["podman", "volume", "create", cache_volume],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        first = run_in_devbox(
            devbox_image,
            [
                "bash",
                "-ceu",
                r"""
test "$GOPATH" = /sandbox/.cache/go
test "$GOCACHE" = /sandbox/.cache/go/build-cache
case ":$PATH:" in *:/sandbox/.cache/go/bin:*) ;; *) exit 1 ;; esac
marker="$GOPATH/pkg/mod/cache/download/golang.org/toolchain/marker"
mkdir -p "$(dirname "$marker")"
printf '%s\n' cached > "$marker"
""",
            ],
            user="sandbox",
            volumes=[f"{cache_volume}:/sandbox/.cache/go"],
        )
        assert first.returncode == 0, (
            "The Go cache path was not writable in the image.\n"
            f"Stdout: {first.stdout}\nStderr: {first.stderr}"
        )

        second = run_in_devbox(
            devbox_image,
            [
                "bash",
                "-ceu",
                r"""
marker=/sandbox/.cache/go/pkg/mod/cache/download/golang.org/toolchain/marker
test "$(cat "$marker")" = cached
""",
            ],
            user="sandbox",
            volumes=[f"{cache_volume}:/sandbox/.cache/go"],
        )
        assert second.returncode == 0, (
            "The Go cache did not survive a second container.\n"
            f"Stdout: {second.stdout}\nStderr: {second.stderr}"
        )
    finally:
        subprocess.run(
            ["podman", "volume", "rm", cache_volume],
            check=False,
            capture_output=True,
            text=True,
        )


@pytest.mark.container
def test_repomix_token_budget_overflow_fails(devbox_image: str):
    res = run_in_devbox(
        devbox_image,
        [
            "bash",
            "-ceu",
            r"""
set -eu
fixture="$(mktemp -d)"
trap 'rm -rf "$fixture"' EXIT
printf '%s\n' 'This fixture must exceed one token.' > "$fixture/source.txt"
set +e
repomix "$fixture" --output "$fixture/packed.xml" --token-budget 1
status=$?
set -e
test "$status" -ne 0
""",
        ],
        user="sandbox",
        timeout=60,
    )
    assert res.returncode == 0, (
        "Repomix accepted output over the token budget.\n"
        f"Stdout: {res.stdout}\nStderr: {res.stderr}"
    )


@pytest.mark.container
def test_container_user_identity(devbox_image: str):
    res = run_in_devbox(devbox_image, ["id", "-u", "-n"], user="sandbox")
    assert res.returncode == 0
    assert res.stdout.strip() == "sandbox"


@pytest.mark.container
def test_playwright_browser_launches(devbox_image: str):
    res = run_in_devbox(
        devbox_image,
        ["playwright-cli", "open", "about:blank"],
        user="sandbox",
        timeout=60,
    )
    assert res.returncode == 0, (
        "Playwright failed to launch the bundled Chromium browser.\n"
        f"Stdout: {res.stdout}\nStderr: {res.stderr}"
    )
    assert "Page URL: about:blank" in res.stdout


@pytest.mark.container
def test_ast_grep_structural_rewrite(devbox_image: str):
    res = run_in_devbox(
        devbox_image,
        [
            "bash",
            "-c",
            (
                "set -eu; "
                'fixture="$(mktemp --suffix=.py)"; '
                "printf '%s\\n' "
                "'print(\"hello\")' "
                "'# print(\"comment\")' "
                '\'print("hello", "world")\' > "$fixture"; '
                "ast-grep --lang python -p 'print($ARG)' "
                "-r 'logger.info($ARG)' -U \"$fixture\"; "
                'grep -Fx \'logger.info("hello")\' "$fixture"; '
                'grep -Fx \'# print("comment")\' "$fixture"; '
                'grep -Fx \'print("hello", "world")\' "$fixture"'
            ),
        ],
        user="sandbox",
    )
    assert res.returncode == 0, (
        "ast-grep failed to apply a structural rewrite.\n"
        f"Stdout: {res.stdout}\nStderr: {res.stderr}"
    )


@pytest.mark.container
def test_semble_search_returns_json_without_network(devbox_image: str):
    res = subprocess.run(
        [
            "podman",
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            "sandbox",
            devbox_image,
            "bash",
            "-c",
            (
                "set -eu; "
                'fixture="$(mktemp -d)"; '
                "trap 'rm -rf \"$fixture\"' EXIT; "
                "printf '%s\\n' "
                "'def retry_failed_request(request):' "
                "'    for attempt in range(3):' "
                "'        try:' "
                "'            return request()' "
                "'        except TimeoutError:' "
                "'            continue' "
                '> "$fixture/retry.py"; '
                "semble search 'where are failed requests retried' \"$fixture\" "
                "--top-k 1 --max-snippet-lines 0 --json"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert res.returncode == 0, (
        "Semble failed to search without network access.\n"
        f"Stdout: {res.stdout}\nStderr: {res.stderr}"
    )
    payload = json.loads(res.stdout)
    assert payload["results"]
    assert payload["results"][0]["file_path"].endswith("retry.py")
