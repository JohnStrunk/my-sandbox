#!/usr/bin/env python3
"""Build the VM's credential-gated OpenCode runtime overlay.

Only environment-variable references are written to the generated config;
credential values are used for gate checks and never serialized.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit


def present(name: str) -> bool:
    return bool(os.environ.get(name))


def enmaas_credentials_ready() -> bool:
    if not present("ENMAAS_URL") or not present("ENMAAS_API_KEY"):
        return False
    enmaas_url = os.environ["ENMAAS_URL"]
    if (
        any(
            not char.isascii() or ord(char) <= 0x20 or ord(char) == 0x7F
            for char in enmaas_url
        )
        or "\\" in enmaas_url
        or re.search(r"%(?![0-9a-fA-F]{2})", enmaas_url)
    ):
        raise ValueError("ENMAAS_URL must be a valid HTTPS URL")
    try:
        endpoint = urlsplit(enmaas_url)
    except ValueError:
        raise ValueError("ENMAAS_URL must be a valid HTTPS URL") from None

    try:
        port = endpoint.port  # Validate a supplied port before using the URL.
        hostname = endpoint.hostname
    except ValueError:
        raise ValueError("ENMAAS_URL must be a valid HTTPS URL") from None

    if (
        endpoint.scheme.lower() != "https"
        or not hostname
        or endpoint.username is not None
        or endpoint.password is not None
        or not valid_enmaas_hostname(hostname, endpoint.netloc.startswith("["))
    ):
        raise ValueError("ENMAAS_URL must be a valid HTTPS URL")
    if port == 0:
        raise ValueError("ENMAAS_URL must be a valid HTTPS URL")
    return True


def valid_enmaas_hostname(hostname: str, bracketed: bool) -> bool:
    if bracketed:
        if "%" in hostname:
            return False
        try:
            return ipaddress.ip_address(hostname).version == 6
        except ValueError:
            return False

    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        pass

    dns_name = hostname[:-1] if hostname.endswith(".") else hostname
    labels = dns_name.split(".")
    # WHATWG URL parsers treat a numeric final DNS label as an IPv4 literal.
    if re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]*)", labels[-1], re.IGNORECASE):
        return False
    return len(dns_name) <= 253 and all(
        0 < len(label) <= 63
        and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label, re.IGNORECASE)
        is not None
        for label in labels
    )


def build_config() -> dict[str, object]:
    config: dict[str, object] = {
        "$schema": "https://opencode.ai/config.json",
        "experimental": {
            "policies": [
                {
                    "action": "provider.use",
                    "resource": "github-copilot",
                    "effect": "deny",
                },
                {"action": "provider.use", "resource": "gitlab", "effect": "deny"},
            ]
        },
        "permissions": [
            {
                "action": "external_directory",
                "resource": "/home/*",
                "effect": "allow",
            },
            {
                "action": "external_directory",
                "resource": "/root/*",
                "effect": "deny",
            },
            {
                "action": "external_directory",
                "resource": "/tmp/*",
                "effect": "allow",
            },
            {"action": "websearch", "resource": "*", "effect": "allow"},
        ],
        "mcp": {
            "servers": {
                "semble": {
                    "type": "local",
                    "command": ["semble"],
                    "disabled": False,
                }
            }
        },
    }
    servers = config["mcp"]["servers"]  # type: ignore[index]

    if present("CONTEXT7_API_KEY"):
        servers["context7"] = {
            "type": "remote",
            "url": "https://mcp.context7.com/mcp",
            "headers": {"Authorization": "Bearer {env:CONTEXT7_API_KEY}"},
            "disabled": False,
        }

    if present("TAVILY_API_KEY"):
        config["websearch"] = {"provider": "tavily"}

    igloo_names = (
        "IGLOO_MCP_COMMUNITY",
        "IGLOO_MCP_COMMUNITY_KEY",
        "IGLOO_MCP_APP_PASS",
        "IGLOO_MCP_APP_ID",
        "IGLOO_MCP_USERNAME",
        "IGLOO_MCP_PASSWORD",
    )
    if all(present(name) for name in igloo_names):
        servers["the-source"] = {
            "disabled": False,
            "type": "local",
            "command": [
                "bash",
                str(Path(__file__).resolve().with_name("run-the-source-mcp.sh")),
            ],
            "environment": {
                **{name: f"{{env:{name}}}" for name in igloo_names},
                "IGLOO_MCP_SERVER_NAME": "The Source",
                "IGLOO_MCP_SERVER_INSTRUCTIONS": (
                    "This server provides search and fetch capabilities for The "
                    "Source, Red Hat's intranet, containing articles with guides, "
                    "instructions, and useful information that helps team members "
                    "do their jobs and contribute to Red Hat."
                ),
            },
        }

    if enmaas_credentials_ready():
        enmaas_settings = {
            "baseURL": "{env:ENMAAS_URL}",
            "apiKey": "{env:ENMAAS_API_KEY}",
        }
        config["providers"] = {
            "anthropic": {
                # An empty env list prevents OpenCode from preferring the
                # provider's direct API-key environment variable.
                "env": [],
                "settings": enmaas_settings,
            },
            "openai": {
                "env": [],
                "settings": enmaas_settings,
                "models": {"rits/zai-org/glm-5-3": {"name": "GLM 5.3 (curvebender)"}},
            },
        }

    return config


if __name__ == "__main__":
    try:
        generated_config = build_config()
    except ValueError as error:
        raise SystemExit(str(error)) from None
    print(json.dumps(generated_config, separators=(",", ":")))
