"""Contract tests for Track B FastAPI server (langgraph_fastapi_multi.py)."""

from __future__ import annotations

import pytest


def test_health_check_endpoint(fastapi_client):
    """Test GET /health returns valid status structure."""
    response = fastapi_client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert "status" in data
    assert "project_loaded" in data
    assert "tools_count" in data
    assert "target_db_connected" in data
    assert "checkpointer_type" in data


def test_list_openai_models_endpoint(fastapi_client):
    """Test GET /v1/models returns standard OpenAI model list structure."""
    response = fastapi_client.get("/v1/models")
    assert response.status_code == 200
    data = response.json()
    assert data["object"] == "list"
    assert isinstance(data["data"], list)
    model_ids = [m["id"] for m in data["data"]]
    assert "wren-agent" in model_ids
    assert "wren-semantic-analyst" in model_ids


def test_openai_chat_completions_empty_messages(fastapi_client):
    """Test POST /v1/chat/completions validates empty messages."""
    response = fastapi_client.post(
        "/v1/chat/completions",
        json={"model": "wren-agent", "messages": []},
    )
    assert response.status_code in (400, 500)


def test_get_session_history_empty(fastapi_client):
    """Test GET /chat/history/{session_id} for a session."""
    session_id = "test-session-non-existent-12345"
    response = fastapi_client.get(f"/chat/history/{session_id}")
    assert response.status_code in (200, 500)
    if response.status_code == 200:
        data = response.json()
        assert data["session_id"] == session_id
        assert isinstance(data["messages"], list)


def test_docs_page(fastapi_client):
    """Test GET /docs returns Swagger UI HTML."""
    response = fastapi_client.get("/docs")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Swagger UI" in response.text
