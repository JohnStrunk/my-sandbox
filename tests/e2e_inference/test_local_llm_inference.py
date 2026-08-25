import os

import pytest
import requests


def get_local_llm_base_url() -> str:
    host = os.getenv("LLM_HOST", "localhost")
    port = os.getenv("LLM_PORT", "11434")
    return f"http://{host}:{port}"


def is_local_llm_running(base_url: str) -> bool:
    try:
        res = requests.get(f"{base_url}/v1/models", timeout=2)
        return res.status_code == 200
    except requests.RequestException:
        return False


@pytest.fixture(scope="module")
def local_llm_url():
    base_url = get_local_llm_base_url()
    if not is_local_llm_running(base_url):
        # Also try 10.0.2.2 if running inside container
        fallback_url = "http://10.0.2.2:11434"
        if is_local_llm_running(fallback_url):
            return fallback_url
        pytest.skip(
            f"Local LLM inference server not running at {base_url}. "
            "Start it with ./local-llm/start-llm-server.sh for local tests."
        )
    return base_url


@pytest.mark.e2e_inference
def test_local_llm_list_models(local_llm_url: str):
    res = requests.get(f"{local_llm_url}/v1/models", timeout=5)
    assert res.status_code == 200
    data = res.json()
    assert "data" in data
    assert isinstance(data["data"], list)
    assert len(data["data"]) > 0, "Expected at least one loaded model in local Ollama"


@pytest.mark.e2e_inference
def test_local_llm_chat_completion(local_llm_url: str):
    # Fetch available models
    res_models = requests.get(f"{local_llm_url}/v1/models", timeout=5)
    assert res_models.status_code == 200
    models = [m["id"] for m in res_models.json().get("data", []) if "id" in m]
    assert models, "No models available for inference"

    selected_model = models[0]
    payload = {
        "model": selected_model,
        "messages": [
            {
                "role": "system",
                "content": "You are a concise assistant. Respond with the token.",
            },
            {"role": "user", "content": "Respond with the single word: PONG"},
        ],
        "max_tokens": 10,
        "temperature": 0.0,
    }

    res = requests.post(
        f"{local_llm_url}/v1/chat/completions",
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
    content = message.get("content", "").strip()
    assert len(content) > 0, "Inference response was empty"
    assert "PONG" in content.upper()
