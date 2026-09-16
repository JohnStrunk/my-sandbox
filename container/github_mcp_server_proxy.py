#!/usr/bin/env python3
"""Enforce bounded, field-filtered GitHub MCP search calls."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from typing import Any, TextIO

MAX_PER_PAGE = 20
MAX_RESPONSE_BYTES = 64 * 1024

FIELD_DEFAULTS: dict[str, list[str]] = {
    "search_issues": ["number", "title", "state", "html_url"],
    "search_pull_requests": ["number", "title", "state", "html_url"],
    "search_code": ["name", "path", "sha", "repository"],
    "list_issues": ["number", "title", "state", "created_at"],
    "list_pull_requests": ["number", "title", "state", "html_url"],
}
PAGINATED_TOOLS = frozenset(
    {
        *FIELD_DEFAULTS,
        "search_commits",
        "search_orgs",
        "search_repositories",
        "search_users",
    }
)


def _id_key(identifier: Any) -> str:
    return json.dumps(identifier, sort_keys=True, separators=(",", ":"))


def _cap_page(arguments: dict[str, Any]) -> None:
    value = arguments.get("perPage")
    if not isinstance(value, int) or isinstance(value, bool):
        arguments["perPage"] = MAX_PER_PAGE
        return
    arguments["perPage"] = min(max(value, 1), MAX_PER_PAGE)


def _rewrite_request(message: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Apply safe defaults and return the called tool name for response tracking."""

    if message.get("method") != "tools/call":
        return message, None

    params = message.get("params")
    if not isinstance(params, dict):
        return message, None
    tool_name = params.get("name")
    if not isinstance(tool_name, str):
        return message, None

    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}
    else:
        arguments = dict(arguments)

    if tool_name in FIELD_DEFAULTS:
        fields = arguments.get("fields")
        if not isinstance(fields, list) or not fields:
            arguments["fields"] = FIELD_DEFAULTS[tool_name].copy()
    elif tool_name == "search_repositories":
        arguments["minimal_output"] = True

    if tool_name in PAGINATED_TOOLS:
        _cap_page(arguments)

    rewritten = dict(message)
    rewritten_params = dict(params)
    rewritten_params["arguments"] = arguments
    rewritten["params"] = rewritten_params
    return rewritten, tool_name if "id" in message else None


def _harden_tool_schema(tool: dict[str, Any]) -> None:
    name = tool.get("name")
    if not isinstance(name, str):
        return
    schema = tool.get("inputSchema")
    if not isinstance(schema, dict):
        return
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return

    if name in FIELD_DEFAULTS:
        fields = properties.get("fields")
        if isinstance(fields, dict):
            fields["minItems"] = 1
        required = schema.setdefault("required", [])
        if isinstance(required, list) and "fields" not in required:
            required.append("fields")

    if name in PAGINATED_TOOLS:
        per_page = properties.get("perPage")
        if isinstance(per_page, dict):
            per_page["default"] = MAX_PER_PAGE
            per_page["maximum"] = MAX_PER_PAGE


def _rewrite_tools_list(response: dict[str, Any]) -> dict[str, Any]:
    result = response.get("result")
    if not isinstance(result, dict):
        return response
    tools = result.get("tools")
    if not isinstance(tools, list):
        return response
    for tool in tools:
        if isinstance(tool, dict):
            _harden_tool_schema(tool)
    return response


def _bound_tool_response(response: dict[str, Any], tool_name: str) -> dict[str, Any]:
    result = response.get("result")
    if not isinstance(result, dict) or result.get("isError"):
        return response
    try:
        response_bytes = len(
            json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
        )
    except (TypeError, ValueError):
        return response
    if response_bytes <= MAX_RESPONSE_BYTES:
        return response

    bounded = dict(response)
    bounded["result"] = {
        "isError": True,
        "content": [
            {
                "type": "text",
                "text": (
                    f"{tool_name} returned more than {MAX_RESPONSE_BYTES // 1024} KiB. "
                    "Request fewer fields or a smaller perPage value, then paginate."
                ),
            }
        ],
    }
    return bounded


def _rewrite_response(
    response: dict[str, Any], pending_tools: dict[str, str]
) -> dict[str, Any]:
    if response.get("method") == "notifications/tools/list_changed":
        return response
    if "id" not in response:
        return response

    tool_name = pending_tools.pop(_id_key(response["id"]), None)
    result = response.get("result")
    if isinstance(result, dict) and result.get("tools") is not None:
        return _rewrite_tools_list(response)
    if tool_name is None:
        return response
    if tool_name not in PAGINATED_TOOLS:
        return response
    return _bound_tool_response(response, tool_name)


def _write_message(
    stream: TextIO, message: dict[str, Any], lock: threading.Lock
) -> None:
    with lock:
        stream.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")))
        stream.write("\n")
        stream.flush()


def _run_proxy() -> int:
    child = subprocess.Popen(
        ["github-mcp-server", "stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert child.stdin is not None
    assert child.stdout is not None
    child_stdout = child.stdout

    pending_tools: dict[str, str] = {}
    pending_lock = threading.Lock()
    output_lock = threading.Lock()

    def forward_responses() -> None:
        for line in child_stdout:
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                print(line, file=sys.stderr, end="")
                continue
            if not isinstance(response, dict):
                continue
            with pending_lock:
                rewritten = _rewrite_response(response, pending_tools)
            _write_message(sys.stdout, rewritten, output_lock)

    response_thread = threading.Thread(target=forward_responses, daemon=True)
    response_thread.start()

    try:
        for line in sys.stdin:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                print(line, file=sys.stderr, end="")
                continue
            if not isinstance(message, dict):
                continue
            with pending_lock:
                rewritten, tool_name = _rewrite_request(message)
                if tool_name is not None:
                    pending_tools[_id_key(message["id"])] = tool_name
            child.stdin.write(
                json.dumps(rewritten, ensure_ascii=False, separators=(",", ":"))
            )
            child.stdin.write("\n")
            child.stdin.flush()
    except BrokenPipeError:
        print("github-mcp-server-proxy: client closed the MCP stream", file=sys.stderr)
    finally:
        child.stdin.close()

    response_thread.join()
    return child.wait()


def main() -> int:
    if len(sys.argv) > 1:
        if sys.argv[1] in {"-h", "--help"}:
            print("Usage: github-mcp-server-proxy")
            return 0
        print("github-mcp-server-proxy does not accept arguments", file=sys.stderr)
        return 2
    return _run_proxy()


if __name__ == "__main__":
    raise SystemExit(main())
