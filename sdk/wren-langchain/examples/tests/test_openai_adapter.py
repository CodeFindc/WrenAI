"""Unit tests for OpenAI protocol message adapter and formatting utilities."""

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
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage

try:
    from examples.server.openai_adapter import (
        OpenAIMessage,
        convert_openai_messages,
        sanitize_and_truncate_text,
        get_exposed_openai_models,
        make_chat_chunk,
        make_sse_keepalive_chunk,
    )
except ImportError:
    from server.openai_adapter import (
        OpenAIMessage,
        convert_openai_messages,
        sanitize_and_truncate_text,
        get_exposed_openai_models,
        make_chat_chunk,
        make_sse_keepalive_chunk,
    )


def test_convert_openai_messages():
    """Test OpenAI message conversion to LangChain message types."""
    raw_msgs = [
        OpenAIMessage(role="system", content="You are a helpful assistant."),
        OpenAIMessage(role="user", content="Hello!"),
        OpenAIMessage(role="assistant", content="Hi there!"),
        OpenAIMessage(role="tool", content="{'ok': true}", tool_call_id="tc_1"),
    ]
    lc_msgs = convert_openai_messages(raw_msgs)
    assert len(lc_msgs) == 4
    assert isinstance(lc_msgs[0], SystemMessage)
    assert isinstance(lc_msgs[1], HumanMessage)
    assert isinstance(lc_msgs[2], AIMessage)
    assert isinstance(lc_msgs[3], ToolMessage)
    assert lc_msgs[3].tool_call_id == "tc_1"


def test_convert_openai_messages_out_of_order_system():
    """Test that out-of-order system messages (e.g. from file upload) are grouped at the head."""
    raw_msgs = [
        OpenAIMessage(role="user", content="Analyze file"),
        OpenAIMessage(role="system", content="[File Attachment Context] ID, Name, Value"),
    ]
    lc_msgs = convert_openai_messages(raw_msgs)
    assert len(lc_msgs) == 2
    assert isinstance(lc_msgs[0], SystemMessage)
    assert isinstance(lc_msgs[1], HumanMessage)
    assert lc_msgs[0].content == "[File Attachment Context] ID, Name, Value"
    assert lc_msgs[1].content == "Analyze file"



def test_sanitize_and_truncate_text():
    """Test secret redaction and character truncation."""
    secret_text = "Database connection: mysql://root:password=my_secret_pwd@localhost:3306/db"
    sanitized = sanitize_and_truncate_text(secret_text, max_chars=200)
    assert "my_secret_pwd" not in sanitized
    assert "***" in sanitized

    long_text = "A" * 500
    truncated = sanitize_and_truncate_text(long_text, max_chars=50)
    assert len(truncated) < 100
    assert "[truncated 500 chars]" in truncated


def test_get_exposed_openai_models(monkeypatch):
    """Test environment variable parsing for exposed OpenAI models."""
    monkeypatch.delenv("OPENAI_EXPOSED_MODELS", raising=False)
    monkeypatch.delenv("LLM_MODEL_NAME", raising=False)
    assert get_exposed_openai_models() == ["wren-agent", "wrenai", "wren-semantic-analyst"]

    monkeypatch.setenv("LLM_MODEL_NAME", "Qwen3.6-27B")
    assert get_exposed_openai_models() == ["Qwen3.6-27B", "wren-agent", "wrenai", "wren-semantic-analyst"]

    monkeypatch.setenv("OPENAI_EXPOSED_MODELS", "model-a, model-b, model-c")
    assert get_exposed_openai_models() == ["Qwen3.6-27B", "model-a", "model-b", "model-c"]




def test_make_sse_keepalive_chunk():
    """Test SSE keepalive chunk generation formats."""
    comment_chunk = make_sse_keepalive_chunk("cid-1", "m", 12345, style="comment")
    assert comment_chunk == ": keepalive\n\n"

    empty_delta = make_sse_keepalive_chunk("cid-1", "m", 12345, style="empty_delta")
    assert "data: {" in empty_delta
    assert "chat.completion.chunk" in empty_delta
