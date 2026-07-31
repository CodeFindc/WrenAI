"""Pytest fixtures and sys.path configuration for Wren-LangChain example servers."""

from __future__ import annotations

import os
import sys

tests_dir = os.path.dirname(os.path.abspath(__file__))
examples_dir = os.path.dirname(tests_dir)
sdk_dir = os.path.dirname(examples_dir)

for path in [sdk_dir, examples_dir]:
    if path not in sys.path:
        sys.path.insert(0, path)

import pytest
from fastapi.testclient import TestClient

wren_project_dir = os.path.join(examples_dir, "wren_project")


@pytest.fixture(scope="session", autouse=True)
def setup_test_env():
    """Ensure PROJECT_PATH and OPENAI_API_KEY are set for unit testing."""
    old_project_path = os.environ.get("PROJECT_PATH")
    old_mcp_config = os.environ.get("MCP_CONFIG_DIR")
    old_openai_key = os.environ.get("OPENAI_API_KEY")

    os.environ["PROJECT_PATH"] = wren_project_dir
    os.environ["MCP_CONFIG_DIR"] = ""
    if not os.environ.get("OPENAI_API_KEY"):
        os.environ["OPENAI_API_KEY"] = "sk-test-mock-key-for-examples-pytest"

    yield

    if old_project_path is not None:
        os.environ["PROJECT_PATH"] = old_project_path
    else:
        os.environ.pop("PROJECT_PATH", None)

    if old_mcp_config is not None:
        os.environ["MCP_CONFIG_DIR"] = old_mcp_config
    else:
        os.environ.pop("MCP_CONFIG_DIR", None)

    if old_openai_key is not None:
        os.environ["OPENAI_API_KEY"] = old_openai_key
    else:
        os.environ.pop("OPENAI_API_KEY", None)


@pytest.fixture(scope="module")
def fastapi_client():
    """TestClient for Track B FastAPI server (langgraph_fastapi_multi), session-scoped for fast test execution."""
    from examples.langgraph_fastapi_multi import app

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture(scope="module")
def mcp_app():
    """Starlette app for Track A FastMCP server (wren_mcp_server)."""
    from examples.wren_mcp_server import app

    return app


@pytest.fixture(scope="module")
def mcp_client(mcp_app):
    """TestClient for Track A FastMCP server."""
    with TestClient(mcp_app, raise_server_exceptions=False) as client:
        yield client
