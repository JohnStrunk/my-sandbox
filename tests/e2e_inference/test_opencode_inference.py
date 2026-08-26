import json
import os
import subprocess
from pathlib import Path

import pytest
import requests

from tests.conftest import run_in_devbox


@pytest.mark.e2e_inference
def test_litemaas_inference_e2e(opencode_json_path: Path):
    api_key = os.getenv("LITEMAAS_API_KEY")
    if not api_key:
        pytest.skip(
            "LITEMAAS_API_KEY not set. Set it to run LiteMaaS E2E inference test."
        )

    data = json.loads(opencode_json_path.read_text())
    litemaas_config = data.get("provider", {}).get("litemaas", {})
    base_url = litemaas_config.get("options", {}).get("baseURL")
    assert base_url, "LiteMaaS baseURL missing in opencode.json"

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
def test_switchyard_inference_e2e(opencode_json_path: Path):
    data = json.loads(opencode_json_path.read_text())
    sy_config = data.get("provider", {}).get("switchyard-proxy", {})
    base_url = sy_config.get("options", {}).get("baseURL")
    assert base_url, "switchyard-proxy baseURL missing in opencode.json"

    # Try local proxy first, or fallback to configured baseURL
    target_url = "http://localhost:4000/v1"
    try:
        r = requests.get("http://localhost:4000/health", timeout=2)
        if r.status_code != 200:
            target_url = base_url
    except requests.RequestException:
        target_url = base_url

    try:
        res = requests.get(f"{target_url}/models", timeout=2)
        if res.status_code != 200:
            pytest.skip("Switchyard proxy not reachable")
    except requests.RequestException:
        pytest.skip("Switchyard proxy not reachable")

    payload = {
        "model": "switchyard-auto",
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
        f"{target_url}/chat/completions",
        json=payload,
        timeout=30,
    )
    assert res.status_code == 200, (
        f"Switchyard request failed with code {res.status_code}: {res.text}"
    )
    res_data = res.json()
    assert "choices" in res_data
    message = res_data["choices"][0]["message"]
    content = message.get("content") or message.get("reasoning_content") or ""
    assert "PONG" in content.upper()


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


@pytest.mark.e2e_inference
def test_opencode_switchyard_in_devbox(devbox_path: Path, repo_root: Path):
    # Verify switchyard proxy is reachable
    try:
        r = requests.get("http://localhost:4000/health", timeout=2)
        if r.status_code != 200:
            pytest.skip("Switchyard proxy is not running locally on port 4000")
    except requests.RequestException:
        pytest.skip("Switchyard proxy is not running locally on port 4000")

    cmd = [
        str(devbox_path),
        "opencode",
        "run",
        "-m",
        "switchyard-proxy/switchyard-auto",
        "Respond with only the single word: PONG",
    ]
    res = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        timeout=60,
        check=False,
    )
    assert res.returncode == 0, (
        f"OpenCode run in devbox failed: {res.stderr}\nStdout: {res.stdout}"
    )
    assert "PONG" in res.stdout.upper()
