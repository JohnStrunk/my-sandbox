"""Offline coverage for optional OpenCode inference configuration."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.e2e_inference import test_opencode_inference as inference_tests


@pytest.mark.unit
def test_enmaas_credential_precedence_uses_openai_config_and_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    enmaas_url = "https://enmaas.example/v1/"
    enmaas_api_key = "enmaas-inference-test-secret"  # pragma: allowlist secret
    monkeypatch.setenv("ENMAAS_URL", enmaas_url)
    monkeypatch.setenv("ENMAAS_API_KEY", enmaas_api_key)
    request_calls: list[dict] = []
    run_calls: list[dict] = []

    def fake_post(url: str, **kwargs: object) -> SimpleNamespace:
        request_calls.append({"url": url, **kwargs})
        return SimpleNamespace(status_code=200, text="accepted")

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        run_calls.append({"command": command, **kwargs})
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(inference_tests.requests, "post", fake_post)
    monkeypatch.setattr(inference_tests.subprocess, "run", fake_run)

    inference_tests.test_enmaas_openai_credential_precedence_e2e(tmp_path)

    assert len(request_calls) == 1
    request = request_calls[0]
    assert request["url"] == "https://enmaas.example/v1/chat/completions"
    assert request["headers"] == {
        "Authorization": f"Bearer {enmaas_api_key}",
        "Content-Type": "application/json",
    }
    assert request["json"]["model"] == inference_tests.ENMAAS_TEST_MODEL
    assert request["allow_redirects"] is False

    assert len(run_calls) == 1
    run = run_calls[0]
    assert run["command"][run["command"].index("--model") + 1] == (
        f"openai/{inference_tests.ENMAAS_TEST_MODEL}"
    )
    env = run["env"]
    assert env["ENMAAS_URL"] == enmaas_url
    assert env["ENMAAS_API_KEY"] == enmaas_api_key
    assert env["OPENAI_API_KEY"] == inference_tests.INVALID_OPENAI_API_KEY
    config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    provider = config["providers"]["openai"]
    assert provider["env"] == []
    assert provider["settings"] == {
        "baseURL": "{env:ENMAAS_URL}",
        "apiKey": "{env:ENMAAS_API_KEY}",
    }
    assert enmaas_api_key not in env["OPENCODE_CONFIG_CONTENT"]


@pytest.mark.unit
def test_enmaas_credential_precedence_rejects_http_before_sending_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("ENMAAS_URL", "http://enmaas.example/v1")
    monkeypatch.setenv(
        "ENMAAS_API_KEY",
        "enmaas-inference-test-secret",  # pragma: allowlist secret
    )

    def unexpected_post(*args: object, **kwargs: object) -> None:
        raise AssertionError("credentials were sent before HTTPS validation")

    monkeypatch.setattr(inference_tests.requests, "post", unexpected_post)

    with pytest.raises(pytest.fail.Exception, match="must use HTTPS"):
        inference_tests.test_enmaas_openai_credential_precedence_e2e(tmp_path)
