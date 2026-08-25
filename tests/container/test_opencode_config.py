import json
from pathlib import Path

import pytest


@pytest.mark.container
def test_opencode_json_valid_syntax(opencode_json_path: Path):
    assert opencode_json_path.exists(), "opencode.json should exist in the repo root"
    data = json.loads(opencode_json_path.read_text())
    assert isinstance(data, dict)


@pytest.mark.container
def test_opencode_providers_configuration(opencode_json_path: Path):
    data = json.loads(opencode_json_path.read_text())
    providers = data.get("provider", {})

    # Check LiteMaaS provider configuration
    assert "litemaas" in providers
    litemaas = providers["litemaas"]
    assert "baseURL" in litemaas.get("options", {})
    assert litemaas["options"]["apiKey"] == "{env:LITEMAAS_API_KEY}"
    assert "Qwen3.6-35B-A3B" in litemaas.get("models", {})

    # Check local LLM provider configuration
    assert "local-llm" in providers
    local_llm = providers["local-llm"]
    assert local_llm.get("options", {}).get("baseURL") == "http://10.0.2.2:11434/v1"
    models = local_llm.get("models", {})
    assert "gpt-oss:20b" in models
    assert "qwen3.8:27b" in models


@pytest.mark.container
def test_opencode_permissions(opencode_json_path: Path):
    data = json.loads(opencode_json_path.read_text())
    permissions = data.get("permission", {})
    external_dir = permissions.get("external_directory", {})
    assert external_dir.get("/tmp/**") == "allow"
