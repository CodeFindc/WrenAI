"""FastMCP dual-transport server for WrenAI Semantic Layer (Track A).

Exposes the real WrenToolkit LangChain tools over Model Context Protocol (MCP)
on a single port (default 8202) with **both** transports coexisting:

    GET  /sse          classic MCP SSE (long-lived event stream + /messages/)
    *    /mcp          Streamable HTTP JSON-RPC (DEEIX-Chat and modern clients)

Upstream MCP clients drive the ReAct loop themselves:

    wren_list_models / wren_fetch_context / wren_recall_queries
      → compose SQL against Wren models
      → wren_dry_plan (optional verify)
      → wren_query
      → wren_store_query (optional)

This is NOT an NL2SQL black box. There is no wren_semantic_query entrypoint;
natural-language planning belongs to the calling LLM. For a full agent that
does NL→SQL internally, use Track B (langgraph_fastapi_multi.py) on port 8201.

Usage:
    PROJECT_PATH=/path/to/wren-project python wren_mcp_server.py
    # or:
    uvicorn wren_mcp_server:app --host 0.0.0.0 --port 8202
"""

from __future__ import annotations

import os
import sys
import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

# Ensure both the `examples` directory and its parent directory are in sys.path
_current_dir = os.path.dirname(os.path.abspath(__file__))
_parent_dir = os.path.dirname(_current_dir)
for _p in [_current_dir, _parent_dir]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    try:
        from fastmcp import FastMCP
    except ImportError:
        sys.exit("[ERROR] 'mcp' package with FastMCP support (mcp>=1.2.0) is required for Track A. Please run: pip install \"mcp>=1.2.0\"")

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from wren_langchain import WrenToolkit

# Environment variables
HOST = os.environ.get("MCP_HOST", os.environ.get("HOST", "0.0.0.0"))
PORT = int(os.environ.get("MCP_PORT", os.environ.get("PORT", "8202")))
SSE_PATH = os.environ.get("MCP_SSE_PATH", "/sse")
STREAMABLE_HTTP_PATH = os.environ.get("MCP_STREAMABLE_HTTP_PATH", "/mcp")

mcp = FastMCP(
    "WrenAI-Semantic-Layer",
    host=HOST,
    port=PORT,
    sse_path=SSE_PATH,
    streamable_http_path=STREAMABLE_HTTP_PATH,
)

_toolkit: WrenToolkit | None = None
_tools_by_name: dict[str, Any] | None = None


def get_toolkit() -> WrenToolkit:
    """Lazy-load WrenToolkit from PROJECT_PATH (once per process)."""
    global _toolkit, _tools_by_name
    if _toolkit is None:
        proj_path = os.environ.get("PROJECT_PATH")
        if not proj_path:
            raise RuntimeError(
                "PROJECT_PATH environment variable is not set. "
                "Please point it to your Wren project directory."
            )
        _toolkit = WrenToolkit.from_project(proj_path)
        _tools_by_name = None
    return _toolkit


def _get_tools_by_name() -> dict[str, Any]:
    """Return a name→tool map, refreshing when the toolkit is first built."""
    global _tools_by_name
    toolkit = get_toolkit()
    if _tools_by_name is None:
        _tools_by_name = {t.name: t for t in toolkit.get_tools()}
    return _tools_by_name


def _format_result(result: Any) -> str:
    """Serialize a LangChain tool result (usually an envelope dict) to JSON text."""
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception:
        return str(result)


@mcp.tool(
    name="wren_query",
    description="Execute a SELECT SQL query against the target database via Wren Engine.",
)
def wren_query(sql: str, limit: int = 100) -> str:
    tools = _get_tools_by_name()
    if "wren_query" not in tools:
        return json.dumps({"ok": False, "error": {"message": "wren_query tool is unavailable"}})
    res = tools["wren_query"].invoke({"sql": sql, "limit": limit})
    return _format_result(res)


@mcp.tool(
    name="wren_dry_plan",
    description="Dry-run/plan a SQL query against the Wren MDL schema without executing physical DB read.",
)
def wren_dry_plan(sql: str) -> str:
    tools = _get_tools_by_name()
    if "wren_dry_plan" not in tools:
        return json.dumps({"ok": False, "error": {"message": "wren_dry_plan tool is unavailable"}})
    res = tools["wren_dry_plan"].invoke({"sql": sql})
    return _format_result(res)


@mcp.tool(
    name="wren_list_models",
    description="List all available data models and view definitions defined in the active Wren project.",
)
def wren_list_models() -> str:
    tools = _get_tools_by_name()
    if "wren_list_models" not in tools:
        return json.dumps({"ok": False, "error": {"message": "wren_list_models tool is unavailable"}})
    res = tools["wren_list_models"].invoke({})
    return _format_result(res)


@mcp.tool(
    name="wren_fetch_context",
    description="Fetch schema and business context relevant to the user question using embedding vector search.",
)
def wren_fetch_context(question: str, limit: int = 5) -> str:
    tools = _get_tools_by_name()
    if "wren_fetch_context" not in tools:
        return json.dumps({"ok": False, "error": {"message": "Memory features not enabled for this project."}})
    res = tools["wren_fetch_context"].invoke({"question": question, "limit": limit})
    return _format_result(res)


@mcp.tool(
    name="wren_recall_queries",
    description="Recall past confirmed natural-language to SQL query pairs relevant to the user question.",
)
def wren_recall_queries(question: str, limit: int = 3) -> str:
    tools = _get_tools_by_name()
    if "wren_recall_queries" not in tools:
        return json.dumps({"ok": False, "error": {"message": "Memory features not enabled for this project."}})
    res = tools["wren_recall_queries"].invoke({"question": question, "limit": limit})
    return _format_result(res)


@mcp.tool(
    name="wren_store_query",
    description="Store a verified natural-language to SQL query mapping into the persistent memory store.",
)
def wren_store_query(question: str, sql: str, tags: list[str] | None = None) -> str:
    tools = _get_tools_by_name()
    if "wren_store_query" not in tools:
        return json.dumps({"ok": False, "error": {"message": "Memory features not enabled for this project."}})
    res = tools["wren_store_query"].invoke({"question": question, "sql": sql, "tags": tags or []})
    return _format_result(res)


@mcp.tool(
    name="wren_get_system_prompt",
    description="Retrieve the recommended Wren AI system prompt for orchestrating tool calls.",
)
def wren_get_system_prompt() -> str:
    toolkit = get_toolkit()
    return toolkit.system_prompt()


def _build_dual_app() -> Starlette:
    """Combine SSE (/sse, /messages/) and Streamable HTTP (/mcp) routes."""
    http_app = mcp.streamable_http_app()
    sse_app = mcp.sse_app()

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "service": "wren-mcp",
                "transports": {
                    "sse": SSE_PATH,
                    "streamable_http": STREAMABLE_HTTP_PATH,
                    "sse_messages": "/messages/",
                },
                "tools": [
                    "wren_query",
                    "wren_dry_plan",
                    "wren_list_models",
                    "wren_fetch_context",
                    "wren_recall_queries",
                    "wren_store_query",
                    "wren_get_system_prompt",
                ],
            }
        )

    routes = list(sse_app.routes) + list(http_app.routes) + [
        Route("/health", endpoint=health, methods=["GET"]),
    ]

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with mcp.session_manager.run():
            yield

    return Starlette(debug=False, routes=routes, lifespan=lifespan)


app = _build_dual_app()


if __name__ == "__main__":
    import uvicorn

    print(
        f"Starting FastMCP dual-transport server for WrenAI on "
        f"http://{HOST}:{PORT} ..."
    )
    print(f"  SSE (classic):           http://{HOST}:{PORT}{SSE_PATH}")
    print(f"  Streamable HTTP (DEEIX): http://{HOST}:{PORT}{STREAMABLE_HTTP_PATH}")
    print(f"  Health:                  http://{HOST}:{PORT}/health")

    try:
        uvicorn.run(app, host=HOST, port=PORT, log_level="info")
    except Exception as err:
        import traceback

        print(f"[ERROR] FastMCP server failed to start: {err}", file=sys.stderr, flush=True)
        traceback.print_exc()
        sys.exit(1)
