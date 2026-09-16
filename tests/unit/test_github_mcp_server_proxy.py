import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from container.github_mcp_server_proxy import (
    MAX_PER_PAGE,
    MAX_RESPONSE_BYTES,
    _rewrite_request,
    _rewrite_response,
)


@pytest.mark.unit
def test_search_request_gets_safe_fields_and_page_cap():
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "search_issues",
            "arguments": {"query": "repo:owner/repo", "perPage": 100},
        },
    }

    rewritten, tool_name = _rewrite_request(request)

    assert tool_name == "search_issues"
    assert rewritten["params"]["arguments"] == {
        "query": "repo:owner/repo",
        "fields": ["number", "title", "state", "html_url"],
        "perPage": MAX_PER_PAGE,
    }


@pytest.mark.unit
def test_explicit_search_fields_are_preserved():
    request = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "search_pull_requests",
            "arguments": {"fields": ["body"], "perPage": 0},
        },
    }

    rewritten, _ = _rewrite_request(request)

    assert rewritten["params"]["arguments"] == {"fields": ["body"], "perPage": 1}


@pytest.mark.unit
def test_tools_list_advertises_search_contract():
    response = {
        "jsonrpc": "2.0",
        "id": 3,
        "result": {
            "tools": [
                {
                    "name": "search_issues",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "fields": {"type": "array"},
                            "perPage": {"type": "integer"},
                        },
                    },
                }
            ]
        },
    }

    rewritten = _rewrite_response(response, {})
    schema = rewritten["result"]["tools"][0]["inputSchema"]

    assert "fields" in schema["required"]
    assert schema["properties"]["fields"]["minItems"] == 1
    assert schema["properties"]["perPage"]["maximum"] == MAX_PER_PAGE


@pytest.mark.unit
def test_oversized_search_result_becomes_bounded_error():
    response = {
        "jsonrpc": "2.0",
        "id": 4,
        "result": {"content": [{"type": "text", "text": "x" * MAX_RESPONSE_BYTES}]},
    }

    rewritten = _rewrite_response(response, {"4": "search_issues"})

    assert rewritten["result"]["isError"] is True
    assert "64 KiB" in rewritten["result"]["content"][0]["text"]


@pytest.mark.unit
def test_proxy_calls_rewrite_search_bound_results_and_pass_through_reads(
    tmp_path: Path,
):
    fake_server = tmp_path / "github-mcp-server"
    fake_server.write_text(
        """#!/usr/bin/env python3
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    params = request.get("params", {})
    arguments = params.get("arguments", {})
    if request.get("method") == "tools/call":
        if arguments.get("query") == "oversized":
            text = "x" * 65536
        elif params.get("name") == "get_me":
            text = "x" * 65536
        else:
            text = json.dumps(arguments, sort_keys=True)
        result = {"content": [{"type": "text", "text": text}]}
    else:
        result = {}
    print(
        json.dumps({"jsonrpc": "2.0", "id": request.get("id"), "result": result}),
        flush=True,
    )
"""
    )
    fake_server.chmod(0o755)

    environment = os.environ.copy()
    environment["PATH"] = f"{tmp_path}:{environment['PATH']}"
    proxy = Path(__file__).parents[2] / "container" / "github_mcp_server_proxy.py"
    process = subprocess.Popen(
        [sys.executable, str(proxy)],
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None
    assert process.stdout is not None

    process.stdin.write(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "search_issues", "arguments": {"query": "small"}},
            }
        )
        + "\n"
    )
    process.stdin.flush()
    search_response = json.loads(process.stdout.readline())
    search_arguments = json.loads(search_response["result"]["content"][0]["text"])
    assert search_arguments["fields"] == ["number", "title", "state", "html_url"]
    assert search_arguments["perPage"] == MAX_PER_PAGE

    code_request = {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {"name": "search_code", "arguments": {"query": "needle"}},
    }
    rewritten_code, _ = _rewrite_request(code_request)
    assert rewritten_code["params"]["arguments"]["fields"] == [
        "name",
        "path",
        "sha",
        "repository",
    ]

    process.stdin.write(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "get_me", "arguments": {}},
            }
        )
        + "\n"
    )
    process.stdin.flush()
    read_response = json.loads(process.stdout.readline())
    assert read_response["result"].get("isError") is not True
    assert len(read_response["result"]["content"][0]["text"]) == MAX_RESPONSE_BYTES

    process.stdin.write(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "search_issues",
                    "arguments": {"query": "oversized"},
                },
            }
        )
        + "\n"
    )
    process.stdin.flush()
    oversized_response = json.loads(process.stdout.readline())
    assert oversized_response["result"]["isError"] is True

    process.stdin.close()
    assert process.wait(timeout=5) == 0
