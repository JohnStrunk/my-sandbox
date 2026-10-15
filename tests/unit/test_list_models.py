"""Tests for the EnMaaS model-listing utility."""

import json
from pathlib import Path

import pytest

from tests.conftest import run_bash_script

TEST_ENMAAS_API_KEY = "enmaas-model-list-test-secret"  # pragma: allowlist secret


def _install_model_listing_mocks(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "mock-bin"
    bin_dir.mkdir()
    calls_file = tmp_path / "curl-calls.jsonl"

    curl = bin_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        "capture = os.environ['MOCK_CURL_CALLS']\n"
        "with open(capture, 'a') as output:\n"
        "    output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "format_name = (\n"
        "    'anthropic'\n"
        "    if 'anthropic-version: 2023-06-01' in sys.argv\n"
        "    else 'openai'\n"
        ")\n"
        "response = {'format': format_name, 'model': format_name + '-model'}\n"
        "if os.environ.get('MOCK_CURL_FAIL_FORMAT') == format_name:\n"
        "    print(json.dumps(response))\n"
        "    print('mock curl failure', file=sys.stderr)\n"
        "    sys.exit(22)\n"
        "if os.environ.get('MOCK_JQ_FAIL_FORMAT') == format_name:\n"
        "    print('{ invalid JSON')\n"
        "else:\n"
        "    print(json.dumps(response))\n"
    )
    curl.chmod(0o755)

    jq = bin_dir / "jq"
    jq.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import sys\n"
        "if sys.argv[1:] != ['.']:\n"
        "    sys.exit(2)\n"
        "try:\n"
        "    response = json.load(sys.stdin)\n"
        "except json.JSONDecodeError as error:\n"
        "    print(f'parse error: {error}', file=sys.stderr)\n"
        "    sys.exit(4)\n"
        "print(json.dumps(response))\n"
    )
    jq.chmod(0o755)
    return bin_dir, calls_file


@pytest.mark.unit
def test_list_models_requests_both_enmaas_formats_without_printing_key(
    repo_root: Path,
    isolated_env: dict[str, str],
    tmp_path: Path,
):
    bin_dir, calls_file = _install_model_listing_mocks(tmp_path)
    api_key = TEST_ENMAAS_API_KEY
    env = isolated_env | {
        "PATH": f"{bin_dir}:{isolated_env['PATH']}",
        "MOCK_CURL_CALLS": str(calls_file),
        "ENMAAS_URL": "https://enmaas.example/v1///",
        "ENMAAS_API_KEY": api_key,
    }

    result = run_bash_script(repo_root / "list-models.sh", env=env, cwd=repo_root)

    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
    assert len(calls) == 2
    assert all("-L" not in call and "--location" not in call for call in calls)
    assert all(call[:2] == ["--proto", "=https"] for call in calls)
    assert [call[-1] for call in calls] == [
        "https://enmaas.example/v1/models",
        "https://enmaas.example/v1/models",
    ]

    def headers(args: list[str]) -> list[str]:
        return [args[index + 1] for index, arg in enumerate(args[:-1]) if arg == "-H"]

    assert headers(calls[0]) == [
        f"x-api-key: {api_key}",
        "anthropic-version: 2023-06-01",
    ]
    assert headers(calls[1]) == [f"Authorization: Bearer {api_key}"]
    assert "anthropic-version" not in " ".join(calls[1])
    anthropic_heading = result.stdout.index("Anthropic-compatible endpoint")
    openai_heading = result.stdout.index("OpenAI-compatible endpoint")
    assert anthropic_heading < openai_heading
    assert (
        '"model": "anthropic-model"' in result.stdout[anthropic_heading:openai_heading]
    )
    assert '"model": "openai-model"' in result.stdout[openai_heading:]
    assert api_key not in result.stdout
    assert api_key not in result.stderr


@pytest.mark.unit
@pytest.mark.parametrize("missing_name", ["ENMAAS_URL", "ENMAAS_API_KEY"])
def test_list_models_requires_both_enmaas_values_before_calling_curl(
    missing_name: str,
    repo_root: Path,
    isolated_env: dict[str, str],
    tmp_path: Path,
):
    bin_dir, calls_file = _install_model_listing_mocks(tmp_path)
    env = isolated_env | {
        "PATH": f"{bin_dir}:{isolated_env['PATH']}",
        "MOCK_CURL_CALLS": str(calls_file),
    }
    if missing_name == "ENMAAS_URL":
        env["ENMAAS_API_KEY"] = TEST_ENMAAS_API_KEY
    else:
        env["ENMAAS_URL"] = "https://enmaas.example/v1"

    result = run_bash_script(repo_root / "list-models.sh", env=env, cwd=repo_root)

    assert result.returncode != 0
    assert not calls_file.exists()
    assert TEST_ENMAAS_API_KEY not in result.stdout
    assert TEST_ENMAAS_API_KEY not in result.stderr


@pytest.mark.unit
@pytest.mark.parametrize("url", ["http://enmaas.example/v1", "ftp://enmaas.example/v1"])
def test_list_models_rejects_non_https_before_sending_credentials(
    url: str,
    repo_root: Path,
    isolated_env: dict[str, str],
    tmp_path: Path,
):
    bin_dir, calls_file = _install_model_listing_mocks(tmp_path)
    api_key = TEST_ENMAAS_API_KEY
    env = isolated_env | {
        "PATH": f"{bin_dir}:{isolated_env['PATH']}",
        "MOCK_CURL_CALLS": str(calls_file),
        "ENMAAS_URL": url,
        "ENMAAS_API_KEY": api_key,
    }

    result = run_bash_script(repo_root / "list-models.sh", env=env, cwd=repo_root)

    assert result.returncode != 0
    assert not calls_file.exists()
    assert "ENMAAS_URL must use HTTPS" in result.stderr
    assert api_key not in result.stdout
    assert api_key not in result.stderr


@pytest.mark.unit
@pytest.mark.parametrize("failure_format", ["anthropic", "openai"])
@pytest.mark.parametrize("failure_kind", ["curl", "jq"])
def test_list_models_runs_other_request_after_format_failure(
    failure_kind: str,
    failure_format: str,
    repo_root: Path,
    isolated_env: dict[str, str],
    tmp_path: Path,
):
    bin_dir, calls_file = _install_model_listing_mocks(tmp_path)
    api_key = TEST_ENMAAS_API_KEY
    env = isolated_env | {
        "PATH": f"{bin_dir}:{isolated_env['PATH']}",
        "MOCK_CURL_CALLS": str(calls_file),
        "ENMAAS_URL": "https://enmaas.example/v1",
        "ENMAAS_API_KEY": api_key,
    }
    env[f"MOCK_{failure_kind.upper()}_FAIL_FORMAT"] = failure_format

    result = run_bash_script(repo_root / "list-models.sh", env=env, cwd=repo_root)

    assert result.returncode != 0
    calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
    assert len(calls) == 2
    successful_format = "openai" if failure_format == "anthropic" else "anthropic"
    if successful_format == "anthropic":
        section_start = result.stdout.index("Anthropic-compatible endpoint")
        section_end = result.stdout.index("OpenAI-compatible endpoint")
    else:
        section_start = result.stdout.index("OpenAI-compatible endpoint")
        section_end = len(result.stdout)
    assert (
        f'"model": "{successful_format}-model"'
        in result.stdout[section_start:section_end]
    )
    assert api_key not in result.stdout
    assert api_key not in result.stderr
