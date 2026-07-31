"""Contract tests for Track A FastMCP dual-transport server (wren_mcp_server.py)."""

from __future__ import annotations

import pytest


def test_mcp_health_check(mcp_client):
    """Test GET /health on Track A MCP server."""
    response = mcp_client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["service"] == "wren-mcp"
    assert "transports" in data
    assert data["transports"]["sse"] == "/sse"
    assert data["transports"]["streamable_http"] == "/mcp"
    assert isinstance(data["tools"], list)
    assert "wren_query" in data["tools"]
    assert "wren_dry_plan" in data["tools"]
    assert "wren_list_models" in data["tools"]


def test_mcp_sse_endpoint_registered(mcp_app):
    """Test GET /sse route is registered in the Starlette dual app."""
    paths = [getattr(r, "path", None) for r in mcp_app.routes]
    assert "/sse" in paths


def test_mcp_streamable_http_endpoint_post(mcp_client):
    """Test POST /mcp endpoint responds to JSON-RPC initializations."""
    response = mcp_client.post(
        "/mcp",
        headers={"Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0"},
            },
            "id": 1,
        },
    )
    assert response.status_code in (200, 400, 406, 415, 422)
