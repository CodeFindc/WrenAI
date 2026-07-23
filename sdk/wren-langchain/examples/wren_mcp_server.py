"""FastMCP SSE Server for WrenAI Semantic Layer.

Exposes WrenAI Semantic Layer capabilities (Text-to-SQL, data modeling, semantic queries)
over Model Context Protocol (MCP) using SSE transport (default port 8202).

Usage:
    python examples/wren_mcp_server.py
    or:
    uvicorn examples.wren_mcp_server:app --host 0.0.0.0 --port 8202
"""

from __future__ import annotations

import os
import sys
import asyncio
from typing import Any

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    sys.exit("mcp package with FastMCP is required. Run: pip install mcp")

from wren_langchain import WrenToolkit

# Environment variables
HOST = os.environ.get("MCP_HOST", os.environ.get("HOST", "0.0.0.0"))
PORT = int(os.environ.get("MCP_PORT", os.environ.get("PORT", "8202")))

mcp = FastMCP("WrenAI-Semantic-Layer", host=HOST, port=PORT)

_toolkit: WrenToolkit | None = None

def get_toolkit() -> WrenToolkit:
    global _toolkit
    if _toolkit is None:
        proj_path = os.environ.get("PROJECT_PATH")
        if not proj_path:
            raise RuntimeError("PROJECT_PATH environment variable is not set. Please point it to your Wren project directory.")
        _toolkit = WrenToolkit.from_project(proj_path)
    return _toolkit


@mcp.tool(
    name="wren_semantic_query",
    description="Query the WrenAI semantic data layer using natural language. Automatically converts question into semantic context, generates valid SQL, executes it against the target database, and returns analytical results."
)
async def wren_semantic_query(question: str) -> str:
    """Execute a natural language query against the WrenAI semantic layer."""
    try:
        toolkit = get_toolkit()
        tools = toolkit.get_tools()
        if not tools:
            return "Error: No semantic tools available. Please ensure models are defined and connection profiles are configured in profiles.yml."
            
        query_tool = tools[0]
        if hasattr(query_tool, "ainvoke"):
            res = await query_tool.ainvoke({"query": question})
            return str(res)
        elif hasattr(query_tool, "invoke"):
            res = query_tool.invoke({"query": question})
            return str(res)
    
        if hasattr(toolkit, "query"):
            res = toolkit.query(question)
            return str(res)
        return "No suitable query execution mechanism found in WrenToolkit."
    except Exception as e:
        err_msg = str(e)
        if "connection" in err_msg.lower() or "refused" in err_msg.lower() or "timeout" in err_msg.lower():
            return f"Database Connection Error: Failed to execute query. Target database appears unreachable or unconfigured. Detail: {err_msg}"
        return f"Error executing semantic query: {err_msg}"


@mcp.tool(
    name="wren_get_system_prompt",
    description="Get the system prompt and instructions for the WrenAI semantic project, detailing schema knowledge, model guidelines, and querying instructions."
)
async def wren_get_system_prompt() -> str:
    """Retrieve system prompt instructions for the current Wren project."""
    try:
        toolkit = get_toolkit()
        return toolkit.system_prompt()
    except Exception as e:
        return f"Error fetching system prompt: {e}"


@mcp.tool(
    name="wren_list_tools",
    description="List all available sub-tools and internal schemas defined inside the WrenAI toolkit."
)
async def wren_list_tools() -> str:
    """List internal sub-tools provided by WrenToolkit."""
    try:
        toolkit = get_toolkit()
        tools = toolkit.get_tools()
        info = []
        for t in tools:
            name = getattr(t, "name", str(t))
            desc = getattr(t, "description", "")
            info.append(f"- {name}: {desc}")
        return "\n".join(info) if info else "No sub-tools available."
    except Exception as e:
        return f"Error listing toolkit tools: {e}"


# Expose Starlette SSE app for mounting under Uvicorn if needed
app = mcp.sse_app()

if __name__ == "__main__":
    print(f"Starting FastMCP SSE Server for WrenAI on http://{HOST}:{PORT}/sse ...")
    mcp.run(transport="sse")
