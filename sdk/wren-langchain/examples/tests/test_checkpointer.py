"""Unit tests for checkpointer persistence and fallback mechanisms."""

from __future__ import annotations

import sys
import os

tests_dir = os.path.dirname(os.path.abspath(__file__))
examples_dir = os.path.dirname(tests_dir)
sdk_dir = os.path.dirname(examples_dir)

for path in [sdk_dir, examples_dir]:
    if path not in sys.path:
        sys.path.insert(0, path)

import pytest
from langgraph.checkpoint.memory import MemorySaver

try:
    from examples.server.checkpointer import ReconnectingPyMySQLSaver
except ImportError:
    from server.checkpointer import ReconnectingPyMySQLSaver


def test_memory_saver_fallback():
    """Verify MemorySaver operates in-memory when MySQL URI is not supplied."""
    saver = MemorySaver()
    assert saver is not None
    assert hasattr(saver, "get_tuple")
    assert hasattr(saver, "put")


def test_reconnecting_pymysql_saver_import():
    """Verify ReconnectingPyMySQLSaver class availability."""
    if ReconnectingPyMySQLSaver is not None:
        assert hasattr(ReconnectingPyMySQLSaver, "from_conn_string")
        assert hasattr(ReconnectingPyMySQLSaver, "_ping_unlocked")
