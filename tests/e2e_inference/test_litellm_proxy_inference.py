import os

import pytest
import requests


def get_proxy_base_url() -> str:
    host = os.getenv("PROXY_HOST", "localhost")
    port = os.getenv("PROXY_PORT", "4000")
    return f"http://{host}:{port}"


def is_proxy_running(base_url: str) -> bool:
    try:
        res = requests.get(f"{base_url}/health/readiness", timeout=2)
        return res.status_code == 200
    except requests.RequestException:
        try:
            res = requests.get(f"{base_url}/v1/models", timeout=2)
            return res.status_code == 200
        except requests.RequestException:
            return False


@pytest.fixture(scope="module")
def proxy_url():
    base_url = get_proxy_base_url()
    if not is_proxy_running(base_url):
        fallback_url = "http://10.0.2.2:4000"
        if is_proxy_running(fallback_url):
            return fallback_url
        pytest.skip(
            f"LiteLLM proxy server not running at {base_url}. "
            "Start it with ./litellm-proxy/start-proxy-server.sh for e2e tests."
        )
    return base_url


@pytest.mark.e2e_inference
def test_litellm_proxy_list_models(proxy_url: str):
    headers = {}
    master_key = os.getenv("LITELLM_MASTER_KEY")
    if master_key:
        headers["Authorization"] = f"Bearer {master_key}"

    res = requests.get(f"{proxy_url}/v1/models", headers=headers, timeout=5)
    assert res.status_code == 200
    data = res.json()
    assert "data" in data
    assert isinstance(data["data"], list)
    model_ids = [m.get("id") for m in data["data"]]
    assert "smart-router" in model_ids


@pytest.mark.e2e_inference
def test_litellm_proxy_chat_completion_smart_router(proxy_url: str):
    headers = {"Content-Type": "application/json"}
    master_key = os.getenv("LITELLM_MASTER_KEY")
    if master_key:
        headers["Authorization"] = f"Bearer {master_key}"

    payload = {
        "model": "smart-router",
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
        f"{proxy_url}/v1/chat/completions",
        headers=headers,
        json=payload,
        timeout=60,
    )
    assert res.status_code == 200, (
        f"Inference request failed with code {res.status_code}: {res.text}"
    )

    data = res.json()
    assert "choices" in data
    assert len(data["choices"]) > 0
    message = data["choices"][0].get("message", {})
    content = (message.get("content") or message.get("reasoning_content") or "").strip()
    assert len(content) > 0, "Inference response was empty"
    assert "PONG" in content.upper()
    assert "router_model_name" in data


@pytest.mark.e2e_inference
def test_litellm_proxy_complexity_routing_e2e(proxy_url: str):
    headers = {"Content-Type": "application/json"}
    master_key = os.getenv("LITELLM_MASTER_KEY")
    if master_key:
        headers["Authorization"] = f"Bearer {master_key}"

    # 1. Simple request -> should route to simple tier (gemini-flash)
    simple_payload = {
        "model": "smart-router",
        "messages": [
            {
                "role": "user",
                "content": "Hi",
            }
        ],
        "max_tokens": 150,
        "temperature": 0.0,
    }
    res_simple = requests.post(
        f"{proxy_url}/v1/chat/completions",
        headers=headers,
        json=simple_payload,
        timeout=60,
    )
    assert res_simple.status_code == 200
    data_simple = res_simple.json()
    assert data_simple.get("router_model_name") == "gemini-flash"

    # 2. Complex reasoning request -> routes to complex tier (litemaas-qwen)
    complex_payload = {
        "model": "smart-router",
        "messages": [
            {
                "role": "user",
                "content": (
                    "Think step by step and analyze the architecture for a "
                    "distributed database consensus protocol. Respond with PONG."
                ),
            }
        ],
        "max_tokens": 150,
        "temperature": 0.0,
    }
    res_complex = requests.post(
        f"{proxy_url}/v1/chat/completions",
        headers=headers,
        json=complex_payload,
        timeout=60,
    )
    assert res_complex.status_code == 200
    data_complex = res_complex.json()
    assert data_complex.get("router_model_name") == "litemaas-qwen"
