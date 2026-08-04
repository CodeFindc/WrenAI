"""OpenAI API protocol adapter and SSE stream generation logic."""

from __future__ import annotations

import os
import json
import uuid
import datetime
from typing import Any
from pydantic import BaseModel
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage, ToolMessage

try:
    from server.logging_config import get_logger
except ImportError:
    from .logging_config import get_logger

logger_sse = get_logger("sse")

VIRTUAL_MODEL_ALIASES = {"wren-agent", "wrenai", "wren-semantic-analyst", "wren", "default"}


class OpenAIMessage(BaseModel):
    role: str
    content: Any = ""
    name: str | None = None
    tool_call_id: str | None = None


class OpenAIChatCompletionRequest(BaseModel):
    model: str = "wren-agent"
    messages: list[OpenAIMessage]
    stream: bool = False
    temperature: float | None = 0.0
    top_p: float | None = 1.0
    n: int | None = 1
    max_tokens: int | None = None
    user: str | None = None



def get_exposed_openai_models() -> list[str]:
    """Parse OPENAI_EXPOSED_MODELS environment variable (comma-separated). Fallback to defaults if empty."""
    raw = os.getenv("OPENAI_EXPOSED_MODELS", "").strip()
    if not raw:
        models = ["wren-agent", "wrenai", "wren-semantic-analyst"]
    else:
        models = [m.strip() for m in raw.split(",") if m.strip()]
        if not models:
            models = ["wren-agent", "wrenai", "wren-semantic-analyst"]

    env_model = os.getenv("LLM_MODEL_NAME", "").strip()
    if env_model and env_model not in models:
        models.insert(0, env_model)

    return models


def convert_openai_messages(messages: list[OpenAIMessage]) -> list[BaseMessage]:
    """Convert a list of OpenAI API format messages into LangChain BaseMessage objects.

    Merges all SystemMessages into a single SystemMessage at index 0 to comply with strict
    vLLM / Qwen Jinja2 chat templates ('System message must be at the beginning.').
    """
    sys_contents = []
    other_messages = []

    for msg in messages:
        content = msg.content or ""
        if isinstance(content, list):
            parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
            content = " ".join(parts)

        role = msg.role.lower()
        if role == "system":
            if content.strip() and content.strip() not in sys_contents:
                sys_contents.append(content.strip())
        elif role == "user":
            other_messages.append(HumanMessage(content=content))
        elif role == "assistant":
            other_messages.append(AIMessage(content=content))
        elif role == "tool":
            other_messages.append(ToolMessage(content=content, tool_call_id=msg.tool_call_id or "tool_call"))

    if sys_contents:
        merged_sys_message = SystemMessage(content="\n\n---\n\n".join(sys_contents))
        return [merged_sys_message] + other_messages

    return other_messages




def get_openai_process_stream_mode() -> str:
    """Get the OpenAI process streaming mode: 'off' | 'text' | 'reasoning' | 'both'. Defaults to 'reasoning'."""
    val = os.getenv("OPENAI_PROCESS_STREAM_MODE", "reasoning").strip().lower()
    if val in ("off", "text", "reasoning", "both"):
        return val
    return "reasoning"


def get_openai_process_max_tool_chars() -> int:
    """Get the maximum character limit for tool execution summary in process stream."""
    try:
        return int(os.getenv("OPENAI_PROCESS_MAX_TOOL_CHARS", "400"))
    except ValueError:
        return 400


def get_openai_sse_keepalive_seconds() -> float:
    """Get SSE keepalive interval in seconds. Defaults to 15.0. Set <= 0 to disable."""
    try:
        return float(os.getenv("OPENAI_SSE_KEEPALIVE_SECONDS", "15.0"))
    except ValueError:
        return 15.0


def get_openai_sse_keepalive_style() -> str:
    """Get SSE keepalive style: 'comment' (default, ': keepalive\n\n') or 'empty_delta'."""
    val = os.getenv("OPENAI_SSE_KEEPALIVE_STYLE", "comment").strip().lower()
    if val in ("comment", "empty_delta"):
        return val
    return "comment"


def sanitize_and_truncate_text(text: Any, max_chars: int = 400) -> str:
    """Sanitize sensitive keywords and truncate text to max_chars."""
    if not text:
        return ""
    sanitized = str(text)
    for kw in ["password", "secret", "api_key", "token", "access_key"]:
        if kw in sanitized.lower():
            import re

            sanitized = re.sub(
                rf"('{kw}'|\"{kw}\"|{kw})\s*[:=]\s*['\"]?[^'\";\s]+['\"]?", r"\1: ***", sanitized, flags=re.IGNORECASE
            )
    if len(sanitized) > max_chars:
        return sanitized[:max_chars] + f"... [truncated {len(sanitized)} chars]"
    return sanitized


def format_process_tool_start(tool_calls: list) -> str:
    """Format tool call intentions into human-readable thinking trace."""
    traces = []
    for tc in tool_calls:
        name = tc.get("name", "unknown_tool") if isinstance(tc, dict) else getattr(tc, "name", "unknown_tool")
        args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
        args_str = json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args)
        args_str = sanitize_and_truncate_text(args_str, 150)
        traces.append(f"🔧 [Wren Agent] 准备执行工具 `{name}` (参数: {args_str})")
    return "\n".join(traces)


def format_process_tool_result(tool_msg: ToolMessage, max_chars: int = 400) -> str:
    """Format tool execution result into thinking trace summary."""
    name = getattr(tool_msg, "name", "tool")
    content = str(tool_msg.content or "")
    summary = sanitize_and_truncate_text(content, max_chars)
    return f"⚡ [Wren Agent] 工具 `{name}` 执行完成，结果摘要:\n{summary}"


def format_process_progress(event: dict) -> str:
    """Format MCP or internal progress event into thinking trace."""
    msg = event.get("message", "")
    detail = event.get("detail", "")
    text = f"{msg}: {detail}" if detail else msg
    return f"⏳ [Wren Progress] {sanitize_and_truncate_text(text, 200)}"


def make_chat_chunk(completion_id: str, model: str, created_ts: int, delta: dict, finish_reason: str | None = None) -> str:
    """Helper to generate standard OpenAI SSE chat completion chunk string."""
    chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created_ts,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


def make_sse_keepalive_chunk(completion_id: str, model: str, created_ts: int, style: str) -> str:
    """Generate an SSE keepalive byte stream (either a comment line or an empty delta chunk)."""
    if style == "empty_delta":
        return make_chat_chunk(completion_id, model, created_ts, {})
    return ": keepalive\n\n"
