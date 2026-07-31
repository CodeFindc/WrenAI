"""LangGraph ReAct agent graph state machine builder."""

from __future__ import annotations

import os
import time
import datetime
from typing import Annotated, TypedDict, Any
from langchain_core.messages import BaseMessage, SystemMessage, AIMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from wren_langchain import WrenToolkit

try:
    from server.logging_config import get_logger
    from server.mcp_client import global_mcp_tools
except ImportError:
    from .logging_config import get_logger
    from .mcp_client import global_mcp_tools

logger_llm = get_logger("llm")


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


@tool("get_current_time")
def get_current_time() -> str:
    """Get the current system date and time of the host machine."""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_wren_system_prompt(messages: list[BaseMessage], system_prompt: str) -> list[BaseMessage]:
    """Ensure the Wren system prompt is present in the message list without duplicating or overriding existing system slots."""
    if not system_prompt:
        return list(messages)

    for msg in messages:
        if isinstance(msg, SystemMessage) and msg.content == system_prompt:
            return list(messages)

    insert_idx = 0
    while insert_idx < len(messages) and isinstance(messages[insert_idx], SystemMessage):
        insert_idx += 1

    new_messages = list(messages)
    new_messages.insert(insert_idx, SystemMessage(content=system_prompt))
    return new_messages


def build_app(toolkit: WrenToolkit, checkpointer: Any = None, model_name: str = "gpt-4o") -> Any:
    """Compile a ReAct graph that uses Wren tools and binds the provided checkpointer (or compiles statelessly if checkpointer=None)."""
    tools = toolkit.get_tools()
    tools.append(get_current_time)

    if global_mcp_tools:
        print(f"Binding {len(global_mcp_tools)} MCP tools to the agent graph...")
        tools.extend(global_mcp_tools)

    system_prompt = toolkit.system_prompt()

    api_base = os.getenv("LLM_API_BASE") or os.getenv("OPENAI_API_BASE")
    api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")

    # Determine target LLM model name
    env_model = os.getenv("LLM_MODEL_NAME")
    if env_model:
        model_to_use = env_model
    elif model_name and model_name not in ("wren-agent", "wren-semantic-analyst"):
        model_to_use = model_name
    else:
        model_to_use = "gpt-4o"

    try:
        llm_request_timeout = float(os.getenv("LLM_REQUEST_TIMEOUT", "60"))
    except ValueError:
        print(f"[WARN] Invalid LLM_REQUEST_TIMEOUT={os.getenv('LLM_REQUEST_TIMEOUT')!r}, using 60s")
        llm_request_timeout = 60.0

    print(f"--- Configured LLM: {model_to_use} | API Base: {api_base or 'default'} | request_timeout={llm_request_timeout}s ---")

    model_with_tools = None

    def get_model() -> Any:
        nonlocal model_with_tools
        if model_with_tools is None:
            # Pass extra_body directly as top-level parameter to avoid UserWarning
            model_with_tools = ChatOpenAI(
                model=model_to_use,
                base_url=api_base,
                api_key=api_key,
                temperature=0,
                request_timeout=llm_request_timeout,
                extra_body={"option": {"num_ctx": 1048576}},
            ).bind_tools(tools)
        return model_with_tools

    def agent_node(state: AgentState) -> dict:
        nonlocal model_with_tools
        messages = state["messages"]
        messages = ensure_wren_system_prompt(messages, system_prompt)

        logger_llm.info(f"invoke_start model={model_to_use} base={api_base or 'default'} messages={len(messages)}")
        start_time = time.perf_counter()

        try:
            model = get_model()
            response = model.invoke(messages)
        except Exception as first_err:
            err_str = str(first_err)
            if "404" in err_str or "NotFound" in err_str or "Model not found" in err_str:
                msg = (
                    f"LLM model '{model_to_use}' not found at API base '{api_base or 'default'}' (Error 404). "
                    f"Please check LLM_MODEL_NAME environment variable or verify the model is deployed."
                )
                logger_llm.error(f"invoke_failed {msg}")
                raise RuntimeError(msg) from first_err

            logger_llm.warning(f"invoke_retry LLM call failed: {first_err}. Resetting client connection pool and retrying...")
            model_with_tools = None
            try:
                model = get_model()
                response = model.invoke(messages)
            except Exception as second_err:
                msg = f"LLM call failed: {second_err}"
                logger_llm.error(f"invoke_failed {msg}")
                raise RuntimeError(msg) from second_err


        duration_ms = int((time.perf_counter() - start_time) * 1000)
        has_tool_calls = bool(getattr(response, "tool_calls", None))
        content_len = len(str(getattr(response, "content", "") or ""))

        logger_llm.info(f"invoke_end duration_ms={duration_ms} has_tool_calls={has_tool_calls} content_chars={content_len}")
        logger_llm.debug(f"invoke_metadata response_metadata={getattr(response, 'response_metadata', {})}")

        return {"messages": [response]}

    def should_continue(state: AgentState) -> str:
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            return "tools"
        return END

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", ToolNode(tools))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")

    if checkpointer is not None:
        return graph.compile(checkpointer=checkpointer)
    return graph.compile()
