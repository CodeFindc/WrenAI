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

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    try:
        from fastmcp import FastMCP
    except ImportError:
        sys.exit("mcp package with FastMCP support (mcp>=1.2.0) is required. Please run: pip install \"mcp>=1.2.0\"")

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from wren_langchain import WrenToolkit

# Environment variables
HOST = os.environ.get("MCP_HOST", os.environ.get("HOST", "0.0.0.0"))
PORT = int(os.environ.get("MCP_PORT", os.environ.get("PORT", "8202")))
# FastMCP defaults: sse_path="/sse", message_path="/messages/", streamable_http_path="/mcp"
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
# Cache of name → LangChain BaseTool from the last successful get_tools() call.
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
        _tools_by_name = None  # force refresh on next lookup
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
    except TypeError:
        return str(result)


def _friendly_error(exc: BaseException) -> str:
    err_msg = str(exc)
    lower = err_msg.lower()
    if any(k in lower for k in ("connection", "refused", "timeout", "unreachable")):
        return (
            "Database Connection Error: Failed to execute tool. "
            f"Target database appears unreachable or unconfigured. Detail: {err_msg}"
        )
    return f"Error executing tool: {err_msg}"


async def _ainvoke_tool(name: str, payload: dict[str, Any]) -> str:
    """Dispatch to a WrenToolkit LangChain tool by name; return JSON envelope text.

    Memory tools are always registered on the MCP surface, but only appear in
    ``toolkit.get_tools()`` when ``.wren/memory/`` exists. Calling them while
    memory is disabled returns a clear error instead of crashing.
    """
    try:
        tools = _get_tools_by_name()
    except Exception as e:
        return _friendly_error(e)

    tool = tools.get(name)
    if tool is None:
        available = ", ".join(sorted(tools)) or "(none)"
        return (
            f"Error: tool '{name}' is not available on this project. "
            f"Runtime tools always include wren_query / wren_dry_plan / "
            f"wren_list_models; memory tools (wren_fetch_context / "
            f"wren_recall_queries / wren_store_query) require a "
            f"`.wren/memory/` directory (run `wren memory index`). "
            f"Currently available: {available}"
        )

    # Drop None-valued optional kwargs so LangChain args_schema validation
    # does not reject omitted optional fields that were passed as null.
    clean_payload = {k: v for k, v in payload.items() if v is not None}

    try:
        if hasattr(tool, "ainvoke"):
            result = tool.ainvoke(clean_payload)
            if hasattr(result, "__await__"):
                result = await result
            return _format_result(result)
        if hasattr(tool, "invoke"):
            result = await asyncio.to_thread(tool.invoke, clean_payload)
            return _format_result(result)
        return f"Error: tool '{name}' has no invoke/ainvoke."
    except Exception as e:
        return _friendly_error(e)


# ── Runtime tools (always present when toolkit loads) ─────────────────────


@mcp.tool(
    name="wren_query",
    description=(
        "Execute SQL through the Wren semantic layer and return rows as a JSON "
        "envelope ({ok, content, data, warnings} or {ok:false, error}). "
        "SQL must target Wren models (from wren_list_models / wren_fetch_context), "
        "not raw physical tables. Default limit is 100 rows; hard cap is 1000 — "
        "aggregate in SQL if you need more. Prefer wren_dry_plan first on complex SQL."
    ),
)
async def wren_query(sql: str, limit: int = 100) -> str:
    """Execute semantic-layer SQL and return an envelope JSON string."""
    return await _ainvoke_tool("wren_query", {"sql": sql, "limit": limit})


@mcp.tool(
    name="wren_dry_plan",
    description=(
        "Plan SQL through the Wren MDL and return the expanded target-dialect SQL "
        "as a JSON envelope. Cheap (no DB round-trip). Use to verify model/column "
        "references before wren_query."
    ),
)
async def wren_dry_plan(sql: str) -> str:
    """Dry-plan semantic SQL; return envelope JSON string."""
    return await _ainvoke_tool("wren_dry_plan", {"sql": sql})


@mcp.tool(
    name="wren_list_models",
    description=(
        "List all models defined in this Wren project with column counts and "
        "descriptions. Returns a JSON envelope. Call this before writing SQL "
        "if you do not yet know the model names."
    ),
)
async def wren_list_models() -> str:
    """List Wren models; return envelope JSON string."""
    return await _ainvoke_tool("wren_list_models", {})


# ── Memory tools (registered always; error clearly when memory is off) ────


@mcp.tool(
    name="wren_fetch_context",
    description=(
        "Fetch relevant schema and business context for an analytical question "
        "via embedding search over the Wren memory index. Call BEFORE writing SQL. "
        "Requires `.wren/memory/` (run `wren memory index`). Optional item_type: "
        "model|column|relationship|view; optional model name to narrow scope."
    ),
)
async def wren_fetch_context(
    question: str,
    limit: int = 5,
    item_type: str | None = None,
    model: str | None = None,
) -> str:
    """Fetch schema/business context; return envelope JSON string."""
    return await _ainvoke_tool(
        "wren_fetch_context",
        {
            "question": question,
            "limit": limit,
            "item_type": item_type,
            "model": model,
        },
    )


@mcp.tool(
    name="wren_recall_queries",
    description=(
        "Recall past natural-language → SQL pairs similar to the given question "
        "(few-shot examples). Requires `.wren/memory/`. Use before writing new SQL."
    ),
)
async def wren_recall_queries(question: str, limit: int = 3) -> str:
    """Recall similar NL→SQL pairs; return envelope JSON string."""
    return await _ainvoke_tool(
        "wren_recall_queries",
        {"question": question, "limit": limit},
    )


@mcp.tool(
    name="wren_store_query",
    description=(
        "Persist a confirmed natural-language → SQL pair for future recall. "
        "Call AFTER wren_query succeeds and the result was useful. "
        "Requires `.wren/memory/`. tags is an optional list of strings."
    ),
)
async def wren_store_query(
    nl: str,
    sql: str,
    tags: list[str] | None = None,
) -> str:
    """Store a confirmed NL→SQL pair; return envelope JSON string."""
    return await _ainvoke_tool(
        "wren_store_query",
        {"nl": nl, "sql": sql, "tags": tags},
    )


# ── Meta helper (not a WrenToolkit tool; always available) ────────────────


@mcp.tool(
    name="wren_get_system_prompt",
    description=(
        "Return the Wren-aware system prompt for this project (workflow rules, "
        "available tools, and project instructions.md). Upstream agents can "
        "inject this as their system message so they follow the same "
        "recall→fetch→compose→dry_plan→query→store workflow as Track B."
    ),
)
async def wren_get_system_prompt() -> str:
    """Return toolkit.system_prompt() text for the calling agent."""
    try:
        toolkit = get_toolkit()
        # Pass the actually available tool list so the prompt stays in sync
        # (memory tools omitted when disabled).
        tools = list(_get_tools_by_name().values())
        return toolkit.system_prompt(tools=tools)
    except Exception as e:
        return _friendly_error(e)


# ── Dual-transport ASGI app (SSE + Streamable HTTP on one port) ───────────
#
# FastMCP's built-in run(transport=...) only starts ONE transport. DEEIX-Chat
# speaks Streamable HTTP JSON-RPC (POST /mcp); classic clients still use GET
# /sse. We merge both route tables from the same FastMCP instance so one
# Uvicorn process serves both without a second port or process.
#
# streamable_http_app() must be built first: it lazily creates the
# StreamableHTTPSessionManager whose .run() lifespan is required for /mcp.


def _build_dual_app() -> Starlette:
    """Combine SSE (/sse, /messages/) and Streamable HTTP (/mcp) routes."""
    http_app = mcp.streamable_http_app()  # initializes session_manager
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

    # SSE routes first (GET /sse + Mount /messages/), then /mcp, then health.
    # Paths do not overlap, so order is only for readability.
    routes = list(sse_app.routes) + list(http_app.routes) + [
        Route("/health", endpoint=health, methods=["GET"]),
    ]

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        # session_manager.run() owns the task group for streamable-http sessions.
        # Must only be entered once per process (FastMCP enforces this).
        async with mcp.session_manager.run():
            yield

    return Starlette(debug=False, routes=routes, lifespan=lifespan)


# Expose dual-transport app for Uvicorn (`uvicorn wren_mcp_server:app ...`)
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
    print(
        "MCP tools: wren_query, wren_dry_plan, wren_list_models, "
        "wren_fetch_context, wren_recall_queries, wren_store_query, "
        "wren_get_system_prompt"
    )
    print(
        "Note: this server exposes SQL/semantic tools for an upstream LLM ReAct "
        "loop. It does NOT accept natural-language questions as SQL. "
        "For a full NL agent use Track B :8201 /v1/chat/completions."
    )
    print(
        "DEEIX-Chat MCP config example:\n"
        f'  {{"name":"wren-semantic","baseURL":"http://<host>:{PORT}{STREAMABLE_HTTP_PATH}"}}'
    )
    # Use the merged app (not mcp.run) so both transports stay live.
    try:
        uvicorn.run(app, host=HOST, port=PORT, log_level="info")
    except Exception as err:
        import traceback
        print(f"[ERROR] FastMCP server failed to start: {err}", file=sys.stderr, flush=True)
        traceback.print_exc()
        sys.exit(1)
