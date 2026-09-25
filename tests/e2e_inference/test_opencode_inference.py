import json
import os
import subprocess

import pytest
import requests

from tests.conftest import run_in_devbox


@pytest.mark.e2e_inference
def test_litemaas_inference_e2e():
    api_key = os.getenv("LITEMAAS_API_KEY")
    if not api_key:
        pytest.skip(
            "LITEMAAS_API_KEY not set. Set it to run LiteMaaS E2E inference test."
        )

    base_url = "https://litemaas.rhoai.rh-aiservices-bu.com/v1"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "Qwen3.6-35B-A3B",
        "messages": [
            {
                "role": "user",
                "content": "Respond with only the single word: PONG",
            }
        ],
        "max_tokens": 150,
        "temperature": 0.0,
    }

    res = requests.post(
        f"{base_url}/chat/completions",
        headers=headers,
        json=payload,
        timeout=30,
    )
    assert res.status_code == 200, (
        f"LiteMaaS request failed with code {res.status_code}: {res.text}"
    )
    res_data = res.json()
    assert "choices" in res_data
    message = res_data["choices"][0]["message"]
    content = message.get("content") or message.get("reasoning_content") or ""
    assert "PONG" in content.upper()


@pytest.mark.e2e_inference
def test_gemini_inference_e2e():
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_GENERATIVE_AI_API_KEY")
    if not api_key:
        pytest.skip("GEMINI_API_KEY not set. Set it to run Gemini E2E test.")

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:generateContent?key={api_key}"
    )
    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": "Respond with only the single word: PONG",
                    }
                ]
            }
        ]
    }

    res = requests.post(url, json=payload, timeout=30)
    assert res.status_code == 200, (
        f"Gemini API request failed with code {res.status_code}: {res.text}"
    )
    data = res.json()
    assert "candidates" in data
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    assert "PONG" in text.upper()


@pytest.mark.e2e_inference
def test_pricetag_openai_gateway_credential_precedence_e2e(tmp_path):
    """OpenCode must send the PriceTag gateway key, not OPENAI_API_KEY.

    OpenCode v2 resolves a built-in provider's credential from its
    environment connection (OPENAI_API_KEY) in preference to a provider
    override's settings.apiKey. The devbox launcher therefore clears the
    provider's environment credential list ("env": []) in the override it
    generates for the PriceTag OpenAI gateway. This test runs OpenCode with
    that exact override shape plus a deliberately invalid OPENAI_API_KEY
    and asserts inference still succeeds; without the cleared list every
    request fails with HTTP 401 because the direct key is sent to the
    gateway.
    """
    api_key = os.getenv("PRICETAG_API_KEY")
    base_url = os.getenv("PRICETAG_OPENAI_URL")
    if not (api_key and base_url):
        pytest.skip(
            "PRICETAG_OPENAI_URL and PRICETAG_API_KEY not set. Set both to "
            "run the PriceTag OpenAI gateway E2E inference test."
        )

    # Sanity-check the gateway itself so a gateway outage is not reported as
    # a credential-precedence regression.
    res = requests.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": "gpt-5.6-luna",
            "messages": [{"role": "user", "content": "Respond with only: PONG"}],
            "max_completion_tokens": 16,
        },
        timeout=60,
    )
    assert res.status_code == 200, (
        f"PriceTag OpenAI gateway rejected the gateway key with code "
        f"{res.status_code}: {res.text}"
    )

    override = json.dumps(
        {
            "$schema": "https://opencode.ai/config.json",
            "providers": {
                "openai": {
                    "env": [],
                    "settings": {
                        "baseURL": "{env:PRICETAG_OPENAI_URL}",
                        "apiKey": "{env:PRICETAG_API_KEY}",
                    },
                },
            },
        }
    )

    env = dict(os.environ)
    env["OPENAI_API_KEY"] = (
        "sk-invalid-key-for-precedence-test"  # pragma: allowlist secret
    )
    env["OPENCODE_CONFIG_CONTENT"] = override
    # Redirect OpenCode's state so the run does not touch real session data
    # or pick up the user's global configuration and plugins.
    for name in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
        directory = tmp_path / name.removeprefix("XDG_").lower()
        directory.mkdir()
        env[name] = str(directory)

    run = subprocess.run(
        [
            "opencode",
            "run",
            "--standalone",
            "--model",
            "openai/gpt-5.6-luna",
            "Reply with just: ok",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    output = f"{run.stdout}\n{run.stderr}"
    assert run.returncode == 0, (
        "OpenCode inference through the PriceTag OpenAI gateway failed "
        "while a conflicting OPENAI_API_KEY was set; the provider override "
        f"may no longer suppress environment credentials.\n{output}"
    )
    assert "HTTP 401" not in output
    assert "ok" in run.stdout.lower()


@pytest.mark.e2e_inference
def test_opencode_cli_in_devbox(devbox_image: str):
    # If any inference credential is set, verify OpenCode runs a basic model check
    has_creds = bool(
        os.getenv("GEMINI_API_KEY")
        or os.getenv("LITEMAAS_API_KEY")
        or os.getenv("GOOGLE_CLOUD_PROJECT")
    )
    if not has_creds:
        pytest.skip(
            "No cloud/LLM credentials found in environment for OpenCode CLI run"
        )

    res = run_in_devbox(devbox_image, ["opencode", "--version"], user="sandbox")
    assert res.returncode == 0
