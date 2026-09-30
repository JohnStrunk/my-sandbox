#!/usr/bin/env python3
"""Build the VM's credential-gated OpenCode runtime overlay.

Only environment-variable references are written to the generated config;
credential values are used for gate checks and never serialized.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def present(name: str) -> bool:
    return bool(os.environ.get(name))


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

    if present("OCTO_OPEN_URL") and present("OCTO_OPEN_KEY"):
        config["providers"] = {
            "octo-open": {
                "package": "aisdk:@ai-sdk/openai-compatible",
                "name": "OCTO Open Models",
                "settings": {
                    "baseURL": "{env:OCTO_OPEN_URL}",
                    "apiKey": "{env:OCTO_OPEN_KEY}",
                },
                "models": {
                    "qwen38-27b-frontier": {
                        "name": "Qwen 3.8 27B FP8 (Frontier)",
                        "limit": {"context": 131072, "output": 8192},
                        "capabilities": {
                            "tools": True,
                            "input": ["text"],
                            "output": ["text"],
                        },
                    },
                    "qwen38-flash-next": {
                        "name": "Qwen 3.8 Flash Next NVFP4 (Core)",
                        "limit": {"context": 262144, "output": 8192},
                        "capabilities": {
                            "tools": True,
                            "input": ["text"],
                            "output": ["text"],
                        },
                    },
                    "qwen38-27b-fast": {
                        "name": "Qwen 3.8 27B NVFP4 (Bulk)",
                        "limit": {"context": 32768, "output": 8192},
                        "capabilities": {
                            "tools": True,
                            "input": ["text"],
                            "output": ["text"],
                        },
                    },
                },
            }
        }

    if present("PRICETAG_API_KEY"):
        providers = config.setdefault("providers", {})
        if present("PRICETAG_ANTHROPIC_URL"):
            providers["anthropic"] = {
                "env": [],
                "settings": {
                    "baseURL": "{env:PRICETAG_ANTHROPIC_URL}",
                    "apiKey": "{env:PRICETAG_API_KEY}",
                },
            }
        if present("PRICETAG_HOSTED_URL"):
            providers["pricetag-hosted"] = {
                "package": "aisdk:@ai-sdk/anthropic",
                "name": "PriceTag (Hosted)",
                "settings": {
                    "baseURL": "{env:PRICETAG_HOSTED_URL}",
                    "apiKey": "{env:PRICETAG_API_KEY}",
                },
                "models": {
                    "Inferact/Qwen3.8-Flash-Next-NVFP4": {
                        "name": "Qwen 3.8 Flash Next (hosted, $0.15/$0.47 per MTok)",
                        "limit": {"context": 262144, "output": 128000},
                        "capabilities": {
                            "tools": True,
                            "input": ["text", "image"],
                            "output": ["text"],
                        },
                        "variants": [
                            {"id": "low", "settings": {"effort": "low"}},
                            {"id": "medium", "settings": {"effort": "medium"}},
                            {"id": "xhigh", "settings": {"effort": "xhigh"}},
                        ],
                    },
                    "rits/zai-org/glm-5-3": {
                        "name": "GLM 5.3 (hosted via curvebender)",
                        "limit": {"context": 262144, "output": 128000},
                        "capabilities": {
                            "tools": True,
                            "input": ["text"],
                            "output": ["text"],
                        },
                        "variants": [
                            {"id": "low", "settings": {"effort": "low"}},
                            {"id": "high", "settings": {"effort": "high"}},
                            {"id": "max", "settings": {"effort": "max"}},
                        ],
                    },
                },
            }
        if present("PRICETAG_OPENAI_URL"):
            providers["openai"] = {
                "env": [],
                "settings": {
                    "baseURL": "{env:PRICETAG_OPENAI_URL}",
                    "apiKey": "{env:PRICETAG_API_KEY}",
                },
            }

    return config


if __name__ == "__main__":
    print(json.dumps(build_config(), separators=(",", ":")))
