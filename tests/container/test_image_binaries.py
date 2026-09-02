import pytest

from tests.conftest import run_in_devbox

BINARIES = [
    ("go", ["go", "version"]),
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
    ("tokenjuice", ["tokenjuice", "--version"]),
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
