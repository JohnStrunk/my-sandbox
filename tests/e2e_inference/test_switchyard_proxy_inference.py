import os
from pathlib import Path

import pytest
import requests


def get_switchyard_proxy_base_url() -> str:
    host = os.getenv("SWITCHYARD_HOST", "localhost")
    port = os.getenv("SWITCHYARD_PORT", "4000")
    return f"http://{host}:{port}"


def is_switchyard_proxy_running(base_url: str) -> bool:
    try:
        res = requests.get(f"{base_url}/health", timeout=2)
        return res.status_code == 200
    except requests.RequestException:
        return False


@pytest.fixture(scope="module")
def switchyard_proxy_url(switchyard_proxy_dir: Path):
    base_url = get_switchyard_proxy_base_url()
    if is_switchyard_proxy_running(base_url):
        yield base_url
        return

    # Check container gateway
    fallback_url = "http://10.0.2.2:4000"
    if is_switchyard_proxy_running(fallback_url):
        yield fallback_url
        return

    # Check if credentials are present to start an in-process test server
    has_creds = bool(os.getenv("GEMINI_API_KEY") or os.getenv("LITEMAAS_API_KEY"))
    if not has_creds:
        pytest.skip(
            "Switchyard proxy is not running and no LLM API keys found to start one."
        )

    try:
        from switchyard_rust.server import Server
    except ImportError:
        pytest.skip(
            f"Switchyard proxy not running at {base_url} "
            "and nemo-switchyard not installed."
        )

    config_path = switchyard_proxy_dir / "routes.toml"
    server = Server(config_path, port=0)
    server_url = server.base_url

    yield server_url

    server.close()


@pytest.mark.e2e_inference
def test_switchyard_proxy_health(switchyard_proxy_url: str):
    res = requests.get(f"{switchyard_proxy_url}/health", timeout=5)
    assert res.status_code == 200
    data = res.json()
    assert data.get("status") == "ok"


@pytest.mark.e2e_inference
def test_switchyard_proxy_list_models(switchyard_proxy_url: str):
    res = requests.get(f"{switchyard_proxy_url}/v1/models", timeout=5)
    assert res.status_code == 200
    data = res.json()
    assert "data" in data
    model_ids = [m["id"] for m in data["data"]]
    assert "switchyard-auto" in model_ids
    assert "switchyard-stage" in model_ids
    assert "switchyard-random" in model_ids


@pytest.mark.e2e_inference
def test_switchyard_proxy_auto_routing(switchyard_proxy_url: str):
    payload = {
        "model": "switchyard-auto",
        "messages": [
            {
                "role": "system",
                "content": "You are a concise assistant.",
            },
            {"role": "user", "content": "Respond with only the single word: PONG"},
        ],
        "max_tokens": 150,
        "temperature": 0.0,
    }

    res = requests.post(
        f"{switchyard_proxy_url}/v1/chat/completions",
        json=payload,
        timeout=60,
    )
    assert res.status_code == 200, (
        f"Auto-routing request failed with code {res.status_code}: {res.text}"
    )

    data = res.json()
    assert "choices" in data
    assert len(data["choices"]) > 0
    message = data["choices"][0].get("message", {})
    content = message.get("content") or message.get("reasoning_content") or ""
    assert len(content.strip()) > 0
    assert "PONG" in content.upper()


@pytest.mark.e2e_inference
def test_switchyard_proxy_stage_routing(switchyard_proxy_url: str):
    payload = {
        "model": "switchyard-stage",
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
        f"{switchyard_proxy_url}/v1/chat/completions",
        json=payload,
        timeout=60,
    )
    assert res.status_code == 200, (
        f"Stage routing request failed with code {res.status_code}: {res.text}"
    )

    data = res.json()
    assert "choices" in data
    assert len(data["choices"]) > 0
    message = data["choices"][0].get("message", {})
    content = message.get("content") or message.get("reasoning_content") or ""
    assert "PONG" in content.upper()


@pytest.mark.e2e_inference
def test_switchyard_proxy_random_routing(switchyard_proxy_url: str):
    payload = {
        "model": "switchyard-random",
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
        f"{switchyard_proxy_url}/v1/chat/completions",
        json=payload,
        timeout=60,
    )
    assert res.status_code == 200, (
        f"Random routing request failed with code {res.status_code}: {res.text}"
    )

    data = res.json()
    assert "choices" in data
    assert len(data["choices"]) > 0
    message = data["choices"][0].get("message", {})
    content = message.get("content") or message.get("reasoning_content") or ""
    assert "PONG" in content.upper()
