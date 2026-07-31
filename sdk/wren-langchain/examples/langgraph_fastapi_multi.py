"""FastAPI server wrapper with PERSISTENT MULTI-TURN SESSIONS and OFFLINE Swagger UI support.

This server integrates the Wren AI semantic layer into a dual-track agent architecture,
offering OpenAI-compatible endpoints (`/v1/*`) and native stateful session chat (`/chat*`).
"""

from __future__ import annotations

import os
import sys
import uuid
import time
import json
import asyncio
import datetime
from contextlib import asynccontextmanager, AsyncExitStack
from typing import Any

# Ensure both the `examples` directory and its parent directory are in sys.path
_current_dir = os.path.dirname(os.path.abspath(__file__))
_parent_dir = os.path.dirname(_current_dir)
for _p in [_current_dir, _parent_dir]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse, Response, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, ToolMessage, SystemMessage

from wren_langchain import WrenToolkit

# Import server sub-modules with fallback support for all execution modes
try:
    from server.logging_config import setup_logging, get_logger, request_id_var
    from server.checkpointer import ReconnectingPyMySQLSaver
    from server.mcp_client import (
        mcp_sessions,
        mcp_configs_registry,
        mcp_exit_stacks,
        global_mcp_tools,
        initialized_mcp_servers,
        mcp_manager_queue,
        load_mcp_configs,
        get_or_create_mcp_session,
        convert_mcp_to_langchain,
        mcp_manager_worker,
        retry_failed_mcp_connections_loop,
    )
    from server.agent_graph import build_app
    from server.openai_adapter import (
        OpenAIMessage,
        OpenAIChatCompletionRequest,
        get_exposed_openai_models,
        convert_openai_messages,
        get_openai_process_stream_mode,
        get_openai_process_max_tool_chars,
        get_openai_sse_keepalive_seconds,
        get_openai_sse_keepalive_style,
        sanitize_and_truncate_text,
        format_process_tool_start,
        format_process_tool_result,
        format_process_progress,
        make_chat_chunk,
        make_sse_keepalive_chunk,
    )
except ImportError:
    from examples.server.logging_config import setup_logging, get_logger, request_id_var
    from examples.server.checkpointer import ReconnectingPyMySQLSaver
    from examples.server.mcp_client import (
        mcp_sessions,
        mcp_configs_registry,
        mcp_exit_stacks,
        global_mcp_tools,
        initialized_mcp_servers,
        mcp_manager_queue,
        load_mcp_configs,
        get_or_create_mcp_session,
        convert_mcp_to_langchain,
        mcp_manager_worker,
        retry_failed_mcp_connections_loop,
    )
    from examples.server.agent_graph import build_app
    from examples.server.openai_adapter import (
        OpenAIMessage,
        OpenAIChatCompletionRequest,
        get_exposed_openai_models,
        convert_openai_messages,
        get_openai_process_stream_mode,
        get_openai_process_max_tool_chars,
        get_openai_sse_keepalive_seconds,
        get_openai_sse_keepalive_style,
        sanitize_and_truncate_text,
        format_process_tool_start,
        format_process_tool_result,
        format_process_progress,
        make_chat_chunk,
        make_sse_keepalive_chunk,
    )

# ── Structured Logging ────────────────────────────────────────────────
logger = setup_logging()
logger_lifespan = get_logger("lifespan")
logger_api = get_logger("api")
logger_sse = get_logger("sse")
logger_tool = get_logger("tool")

# ── Global State ──────────────────────────────────────────────────────
toolkit: WrenToolkit | None = None
langgraph_app = None
langgraph_app_stateless = None
toolkit_init_error: str | None = None
lazy_init_lock = asyncio.Lock()

mcp_retry_task: asyncio.Task | None = None
mcp_manager_task: asyncio.Task | None = None
mysql_exit_stack: AsyncExitStack = AsyncExitStack()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle context manager to initialize the Wren Toolkit, MCP sessions, and checkpointer at startup."""
    global toolkit, langgraph_app, langgraph_app_stateless, mcp_retry_task, mcp_manager_task, mysql_exit_stack, toolkit_init_error

    print("[LIFESPAN] Starting dual-track server lifespan initialization...", flush=True)

    mcp_manager_task = asyncio.create_task(mcp_manager_worker())

    # 1. Initialize Wren Toolkit
    project_path = os.environ.get("PROJECT_PATH")
    if project_path:
        project_path = project_path.strip().strip('"').strip("'")
        try:
            print(f"[LIFESPAN] Initializing WrenToolkit from project: {project_path}...", flush=True)
            toolkit = await asyncio.to_thread(WrenToolkit.from_project, project_path)
            print("[LIFESPAN] Successfully initialized WrenToolkit!", flush=True)
            toolkit_init_error = None
        except Exception as e:
            toolkit_init_error = str(e)
            print(
                f"[LIFESPAN Warning] Error initializing WrenToolkit during lifespan startup: {e}. Will lazy-initialize on first request.",
                flush=True,
            )

    if not os.environ.get("OPENAI_API_KEY"):
        print(
            "[LIFESPAN Warning] OPENAI_API_KEY is not set. Custom endpoint configuration or key will be required.",
            flush=True,
        )

    # 2. Initialize MCP Sessions
    mcp_config_dir = os.environ.get("MCP_CONFIG_DIR")
    if mcp_config_dir:
        mcp_config_dir = mcp_config_dir.strip().strip('"').strip("'")
        mcp_configs = load_mcp_configs(mcp_config_dir)
        if mcp_configs:
            print(f"Loaded {len(mcp_configs)} MCP server configs: {list(mcp_configs.keys())}")
            mcp_configs_registry.update(mcp_configs)
            try:
                for server_name in mcp_configs.keys():
                    try:

                        async def _connect_server():
                            session = await get_or_create_mcp_session(server_name)
                            tools_result = await session.list_tools()
                            print(
                                f"Discovered {len(tools_result.tools)} tools from MCP server '{server_name}': {[t.name for t in tools_result.tools]}"
                            )

                            for mcp_tool in tools_result.tools:
                                lc_tool = convert_mcp_to_langchain(server_name, mcp_tool)
                                global_mcp_tools.append(lc_tool)
                            initialized_mcp_servers.add(server_name)

                        await asyncio.wait_for(_connect_server(), timeout=15.0)
                    except asyncio.TimeoutError:
                        print(
                            f"Failed to connect to MCP server '{server_name}': Connection or initialization timed out after 15.0 seconds."
                        )
                    except Exception as e:
                        print(f"Failed to connect to MCP server '{server_name}': {e}")

                failed_servers = set(mcp_configs.keys()) - initialized_mcp_servers
                if failed_servers:

                    def _on_reconnect_success(reconnected_name: str) -> None:
                        global langgraph_app, langgraph_app_stateless
                        langgraph_app = None
                        langgraph_app_stateless = None

                    print(f"Starting background reconnect task for failed MCP servers: {list(failed_servers)}")
                    mcp_retry_task = asyncio.create_task(
                        retry_failed_mcp_connections_loop(on_reconnect_success_cb=_on_reconnect_success)
                    )
            except ImportError:
                print("Warning: 'mcp' package is not installed. Skipping MCP initialization.")

    # 3. Determine Checkpointer
    db_uri = os.environ.get("CHAT_HISTORY_DB_URI")
    if db_uri:
        db_uri = db_uri.strip().strip('"').strip("'")

    if db_uri and sys.platform.startswith("win") and "localhost" in db_uri:
        try:
            from urllib.parse import urlparse, urlunparse

            parsed = urlparse(db_uri)
            if parsed.hostname == "localhost":
                netloc = parsed.netloc
                if "@" in netloc:
                    user_pass, host_port = netloc.rsplit("@", 1)
                    if host_port.startswith("localhost:"):
                        host_port = "127.0.0.1" + host_port[len("localhost") :]
                    elif host_port == "localhost":
                        host_port = "127.0.0.1"
                    new_netloc = f"{user_pass}@{host_port}"
                else:
                    if netloc.startswith("localhost:"):
                        new_netloc = "127.0.0.1" + netloc[len("localhost") :]
                    elif netloc == "localhost":
                        new_netloc = "127.0.0.1"
                parsed = parsed._replace(netloc=new_netloc)
                db_uri = urlunparse(parsed)
                print("Auto-resolved 'localhost' to '127.0.0.1' in database URI for Windows compatibility.")
        except Exception as e:
            print(f"Warning: Failed to auto-resolve localhost in CHAT_HISTORY_URI: {e}")

    checkpointer = None

    if db_uri and toolkit and ReconnectingPyMySQLSaver:
        print("CHAT_HISTORY_DB_URI detected. Attempting to initialize MySQL checkpointer...")
        try:
            if "autocommit" not in db_uri.lower():
                separator = "&" if "?" in db_uri else "?"
                db_uri = f"{db_uri}{separator}autocommit=true"

            checkpointer_candidate = await asyncio.to_thread(
                lambda: mysql_exit_stack.enter_context(ReconnectingPyMySQLSaver.from_conn_string(db_uri))
            )
            print("[MySQL] Setting up checkpointer database tables (timeout: 10s)...", flush=True)
            try:
                await asyncio.wait_for(asyncio.to_thread(checkpointer_candidate.setup), timeout=10.0)
                checkpointer = checkpointer_candidate
                print("[MySQL] Successfully initialized persistent MySQL checkpointer with auto-reconnection!", flush=True)
            except (asyncio.TimeoutError, Exception) as setup_err:
                print(
                    f"[MySQL Warning] Checkpointer table setup failed/timed out: {setup_err}. Cleaning up connection leakage and falling back to in-memory MemorySaver.",
                    flush=True,
                )
                try:
                    mysql_exit_stack.pop_all().close()
                except Exception:
                    pass
                checkpointer = None
        except Exception as e:
            print(f"\n[MySQL Error] Failed to initialize MySQL checkpointer: {e}", flush=True)
            print("Falling back to in-memory MemorySaver...", flush=True)

    if not checkpointer:
        print("Using in-memory MemorySaver (data will clear on server reload).", flush=True)
        from langgraph.checkpoint.memory import MemorySaver

        checkpointer = MemorySaver()

    if toolkit:
        print("[LIFESPAN] Compiling stateful and stateless LangGraph apps...", flush=True)
        langgraph_app = build_app(toolkit, checkpointer=checkpointer)
        langgraph_app_stateless = build_app(toolkit, checkpointer=None)

    try:
        yield
    finally:
        print("Cleaning up lifespan resources...")
        if mcp_retry_task:
            print("Cancelling MCP retry background task...")
            mcp_retry_task.cancel()
            try:
                await mcp_retry_task
            except asyncio.CancelledError:
                pass

        if mcp_manager_task:
            print("Shutting down MCP sessions via manager task...")
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            try:
                await mcp_manager_queue.put(("shutdown", (), future))
                await asyncio.wait_for(future, timeout=10.0)
            except Exception as e:
                print(f"Error during graceful MCP session manager shutdown: {e}")

            mcp_manager_task.cancel()
            try:
                await mcp_manager_task
            except asyncio.CancelledError:
                pass

        await mysql_exit_stack.aclose()
        print("Lifespan cleanup complete.")


app = FastAPI(
    title="Wren LangGraph Stateful API Server",
    description="Expose a Wren-aware LangGraph ReAct agent via stateful, persistent session-based endpoints.",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequestMulti(BaseModel):
    question: str
    session_id: str | None = None
    model_name: str = "gpt-4o"


def serialize_message(msg: BaseMessage) -> dict:
    """Serialize a LangChain BaseMessage into a JSON-compatible dictionary."""
    base_dict = {
        "role": msg.type if msg.type != "human" else "user",
        "content": msg.content,
    }
    if isinstance(msg, AIMessage) and msg.tool_calls:
        base_dict["tool_calls"] = msg.tool_calls
    if isinstance(msg, ToolMessage):
        base_dict["tool_call_id"] = msg.tool_call_id
        if hasattr(msg, "name") and msg.name:
            base_dict["name"] = msg.name
    return base_dict


def get_local_or_cdn(filename: str, cdn_url: str) -> str:
    """Check for local static file asset or fallback to CDN URL."""
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    local_path = os.path.join(static_dir, filename)
    if os.path.exists(local_path):
        return f"/static/{filename}"
    return cdn_url


@app.get("/docs", include_in_schema=False)
async def custom_swagger_ui_html():
    """Serve Swagger UI HTML page with local/CDN fallback."""
    js_url = get_local_or_cdn("swagger-ui-bundle.js", "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js")
    css_url = get_local_or_cdn("swagger-ui.css", "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css")

    html = f"""<!DOCTYPE html>
<html>
<head>
  <link type="text/css" rel="stylesheet" href="{css_url}">
  <title>Wren LangGraph Stateful API Server - Swagger UI</title>
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="{js_url}"></script>
  <script>
    const ui = SwaggerUIBundle({{
      url: '/openapi.json',
      dom_id: '#swagger-ui',
      presets: [
        SwaggerUIBundle.presets.apis,
        SwaggerUIBundle.SwaggerUIStandalonePreset
      ],
      layout: "BaseLayout"
    }})
  </script>
</body>
</html>"""
    return HTMLResponse(html)


@app.get("/health")
async def health_check():
    """Health check endpoint reflecting target database and checkpointer status."""
    global toolkit, toolkit_init_error
    target_db_status = False
    target_db_error = None
    tools_count = 0

    if toolkit:
        try:
            tools = toolkit.get_tools()
            tools_count = len(tools)
            target_db_status = True
        except Exception as e:
            target_db_error = str(e)

    checkpointer_type = "None"
    checkpointer_status = False
    if langgraph_app and hasattr(langgraph_app, "checkpointer"):
        cp = langgraph_app.checkpointer
        checkpointer_type = type(cp).__name__
        if "MySQL" in checkpointer_type or hasattr(cp, "conn"):
            try:
                if hasattr(cp, "conn") and cp.conn is not None:
                    cp.conn.ping()
                    checkpointer_status = True
                else:
                    checkpointer_status = False
            except Exception:
                checkpointer_status = False
        else:
            checkpointer_status = True

    degraded = toolkit is None or not target_db_status
    if toolkit_init_error:
        target_db_error = target_db_error or toolkit_init_error

    return {
        "status": "healthy" if not degraded else "degraded",
        "project_loaded": toolkit is not None,
        "tools_count": tools_count,
        "target_db_connected": target_db_status,
        "target_db_error": target_db_error,
        "checkpointer_type": checkpointer_type,
        "checkpointer_connected": checkpointer_status,
        "stateless_graph_ready": langgraph_app_stateless is not None,
        "toolkit_init_error": toolkit_init_error,
        "memory_enabled": toolkit._memory.enabled if toolkit and hasattr(toolkit, "_memory") else False,
    }


@app.get("/v1/models")
async def list_openai_models():
    """OpenAI API compatible model discovery endpoint for DEEIX-Chat integration."""
    models = get_exposed_openai_models()
    return {
        "object": "list",
        "data": [
            {
                "id": model_id,
                "object": "model",
                "created": 1700000000,
                "owned_by": "wrenai",
                "permission": [],
                "root": model_id,
                "parent": None,
            }
            for model_id in models
        ],
    }


async def lazy_init_app(model_name: str = "gpt-4o") -> None:
    """Thread-safe and exception-safe lazy initialization for stateful and stateless LangGraph applications."""
    global langgraph_app, langgraph_app_stateless, toolkit, toolkit_init_error
    if langgraph_app and langgraph_app_stateless:
        return

    async with lazy_init_lock:
        if langgraph_app and langgraph_app_stateless:
            return

        project_path = os.environ.get("PROJECT_PATH")
        if not project_path:
            raise HTTPException(
                status_code=500, detail="WrenToolkit not initialized. Please set PROJECT_PATH environment variable."
            )
        try:
            if not toolkit:
                toolkit = WrenToolkit.from_project(project_path)
            toolkit_init_error = None

            if not langgraph_app:
                db_uri = os.environ.get("CHAT_HISTORY_DB_URI")
                checkpointer = None
                if db_uri and ReconnectingPyMySQLSaver:
                    db_uri = db_uri.strip().strip('"').strip("'")
                    if "autocommit" not in db_uri.lower():
                        separator = "&" if "?" in db_uri else "?"
                        db_uri = f"{db_uri}{separator}autocommit=true"
                    try:
                        import concurrent.futures

                        cand = mysql_exit_stack.enter_context(ReconnectingPyMySQLSaver.from_conn_string(db_uri))
                        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                            fut = executor.submit(cand.setup)
                            try:
                                fut.result(timeout=10.0)
                                checkpointer = cand
                                print("Lazy-initialized persistent MySQL checkpointer!")
                            except (concurrent.futures.TimeoutError, Exception) as setup_err:
                                print(
                                    f"Warning during lazy MySQL setup: {setup_err}. Cleaning up connection leak & falling back to MemorySaver."
                                )
                                try:
                                    mysql_exit_stack.pop_all().close()
                                except Exception:
                                    pass
                                checkpointer = None
                    except Exception as db_err:
                        print(f"Failed to lazy-initialize MySQL checkpointer: {db_err}. Falling back to MemorySaver...")
                        checkpointer = None

                if not checkpointer:
                    print("Using in-memory MemorySaver (data will clear on server reload) for lazy init.")
                    from langgraph.checkpoint.memory import MemorySaver

                    checkpointer = MemorySaver()

                langgraph_app = build_app(toolkit, checkpointer, model_name=model_name)

            if not langgraph_app_stateless:
                langgraph_app_stateless = build_app(toolkit, checkpointer=None, model_name=model_name)
        except Exception as e:
            toolkit_init_error = str(e)
            raise HTTPException(status_code=500, detail=f"Failed to lazy initialize app: {e}")


@app.post("/v1/chat/completions")
async def openai_chat_completions(request: OpenAIChatCompletionRequest):
    """OpenAI API compatible chat completion endpoint. Stateless: relies strictly on request messages[]."""
    await lazy_init_app(model_name=request.model)
    if not langgraph_app_stateless:
        raise HTTPException(
            status_code=500,
            detail=f"Stateless LangGraph app is not initialized. Toolkit status: {'Error: ' + toolkit_init_error if toolkit_init_error else 'Pending initialization'}",
        )

    if not request.messages:
        raise HTTPException(status_code=400, detail="messages field cannot be empty.")

    input_messages = convert_openai_messages(request.messages)
    if not input_messages:
        raise HTTPException(status_code=400, detail="No valid messages parsed from request.")

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:16]}"
    request_id_var.set(completion_id)
    created_ts = int(datetime.datetime.now().timestamp())
    graph_input = {"messages": input_messages}

    stream_mode = get_openai_process_stream_mode()
    max_tool_chars = get_openai_process_max_tool_chars()

    if not request.stream:
        sync_start_time = time.perf_counter()
        logger_api.info(
            f"request_start route=/v1/chat/completions model={request.model} stream=false messages={len(input_messages)}"
        )
        try:
            final_state = await langgraph_app_stateless.ainvoke(graph_input)
            final_content = ""
            reasoning_traces = []

            if "messages" in final_state and final_state["messages"]:
                for m in final_state["messages"]:
                    if isinstance(m, AIMessage):
                        tool_calls = getattr(m, "tool_calls", None)
                        if tool_calls:
                            reasoning_traces.append(format_process_tool_start(tool_calls))
                        content = getattr(m, "content", "")
                        if content and isinstance(content, str) and content.strip():
                            final_content = content.strip()
                    elif isinstance(m, ToolMessage):
                        reasoning_traces.append(format_process_tool_result(m, max_tool_chars))

                if not final_content:
                    for m in reversed(final_state["messages"]):
                        content = getattr(m, "content", "")
                        if isinstance(m, ToolMessage) and content and isinstance(content, str) and content.strip():
                            final_content = content.strip()
                            break

            if not final_content:
                final_content = "服务已收到您的消息。目前数据库或工具链尚无返回结果，请检查目标数据库连接及配置文件。"

            msg_payload = {"role": "assistant", "content": final_content}
            if stream_mode in ("reasoning", "both") and reasoning_traces:
                msg_payload["reasoning_content"] = "\n\n".join(reasoning_traces)

            duration_ms = int((time.perf_counter() - sync_start_time) * 1000)
            logger_api.info(
                f"request_end route=/v1/chat/completions status=200 duration_ms={duration_ms} content_chars={len(final_content)}"
            )

            return {
                "id": completion_id,
                "object": "chat.completion",
                "created": created_ts,
                "model": request.model,
                "choices": [{"index": 0, "message": msg_payload, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": sum(len(str(m.content)) for m in input_messages) // 4,
                    "completion_tokens": len(final_content) // 4,
                    "total_tokens": (sum(len(str(m.content)) for m in input_messages) + len(final_content)) // 4,
                },
            }
        except Exception as e:
            duration_ms = int((time.perf_counter() - sync_start_time) * 1000)
            err_msg = str(e)
            if "404" in err_msg or "NotFound" in err_msg or "not found" in err_msg.lower():
                logger_api.error(f"request_fail route=/v1/chat/completions duration_ms={duration_ms} error={err_msg}")
            else:
                logger_api.error(f"request_fail route=/v1/chat/completions duration_ms={duration_ms} error={err_msg}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"LLM execution error: {err_msg}")


    async def event_stream_generator():
        request_id_var.set(completion_id)
        stream_start_time = time.perf_counter()
        stream_outcome = "ok"
        keepalive_s = get_openai_sse_keepalive_seconds()
        keepalive_style = get_openai_sse_keepalive_style()

        logger_sse.info(
            f"stream_start route=/v1/chat/completions model={request.model} process_mode={stream_mode} keepalive={keepalive_s}s style={keepalive_style}"
        )

        yield make_chat_chunk(completion_id, request.model, created_ts, {"role": "assistant"})

        stream_queue = asyncio.Queue()
        config = {"configurable": {"stream_queue": stream_queue}}

        async def run_astream():
            try:
                async for event in langgraph_app_stateless.astream(graph_input, config=config, stream_mode="updates"):
                    await stream_queue.put(("graph", event))
            except Exception as err:
                await stream_queue.put(("error", err))
            finally:
                await stream_queue.put(("end", None))

        astream_task = asyncio.create_task(run_astream())
        yielded_any_content = False
        fallback_tool_content = ""
        keepalive_count = 0

        def emit_process_trace(trace_text: str):
            if not trace_text or stream_mode == "off":
                return []
            chunks = []
            if stream_mode in ("reasoning", "both"):
                chunks.append(
                    make_chat_chunk(completion_id, request.model, created_ts, {"reasoning_content": trace_text + "\n\n"})
                )
            if stream_mode in ("text", "both"):
                chunks.append(
                    make_chat_chunk(completion_id, request.model, created_ts, {"content": f"_{trace_text}_\n\n"})
                )
            return chunks

        try:
            while True:
                try:
                    if keepalive_s > 0:
                        item_type, item_data = await asyncio.wait_for(stream_queue.get(), timeout=keepalive_s)
                    else:
                        item_type, item_data = await stream_queue.get()
                except asyncio.TimeoutError:
                    keepalive_count += 1
                    logger_sse.debug(f"keepalive_ping count={keepalive_count} style={keepalive_style}")
                    if keepalive_count == 1 or keepalive_count % 4 == 0:
                        logger_sse.info(f"keepalive_ping count={keepalive_count} style={keepalive_style}")
                    yield make_sse_keepalive_chunk(completion_id, request.model, created_ts, keepalive_style)
                    continue

                if item_type == "end":
                    break
                elif item_type == "error":
                    stream_outcome = "error"
                    logger_sse.error(f"stream_error Exception during stream: {item_data}")
                    yield make_chat_chunk(completion_id, request.model, created_ts, {"content": f"\n[执行异常: {item_data}]"})
                    break
                elif item_type == "progress":
                    trace_str = format_process_progress(item_data)
                    for chk in emit_process_trace(trace_str):
                        yield chk
                elif item_type == "graph":
                    for node_name, update in item_data.items():
                        msgs = update.get("messages", [])
                        for msg in msgs:
                            if isinstance(msg, AIMessage):
                                tool_calls = getattr(msg, "tool_calls", None)
                                if tool_calls:
                                    for tc in tool_calls:
                                        t_name = tc.get("name", "unknown") if isinstance(tc, dict) else getattr(tc, "name", "unknown")
                                        t_args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
                                        logger_tool.info(f"tool_start name={t_name} args={sanitize_and_truncate_text(t_args, 150)}")
                                    trace_str = format_process_tool_start(tool_calls)
                                    for chk in emit_process_trace(trace_str):
                                        yield chk

                                content = getattr(msg, "content", "")
                                if content and isinstance(content, str) and content.strip():
                                    yield make_chat_chunk(completion_id, request.model, created_ts, {"content": content})
                                    yielded_any_content = True
                            elif isinstance(msg, ToolMessage):
                                t_name = getattr(msg, "name", "tool")
                                content = getattr(msg, "content", "")
                                logger_tool.info(f"tool_end name={t_name} result_chars={len(str(content or ''))}")
                                trace_str = format_process_tool_result(msg, max_tool_chars)
                                for chk in emit_process_trace(trace_str):
                                    yield chk
                                if content and isinstance(content, str) and content.strip():
                                    fallback_tool_content = content.strip()

            if not yielded_any_content:
                out_text = fallback_tool_content or "服务已成功接收您的问答。由于目标数据库或大模型工具未返回文本内容，请确认数据库状态。"
                yield make_chat_chunk(completion_id, request.model, created_ts, {"content": out_text})

        finally:
            if not astream_task.done():
                astream_task.cancel()
                import contextlib

                with contextlib.suppress(asyncio.CancelledError):
                    await astream_task
            stream_duration_ms = int((time.perf_counter() - stream_start_time) * 1000)
            logger_sse.info(
                f"stream_end outcome={stream_outcome} duration_ms={stream_duration_ms} keepalive_count={keepalive_count} yielded_content={yielded_any_content}"
            )

        yield make_chat_chunk(completion_id, request.model, created_ts, {}, finish_reason="stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/chat")
async def chat_endpoint(request: ChatRequestMulti):
    """Standard non-streaming stateful chat endpoint. Persistent history is loaded and updated."""
    await lazy_init_app(model_name=request.model_name)

    actual_session_id = request.session_id or str(uuid.uuid4())
    config = {"configurable": {"thread_id": actual_session_id}}

    try:
        final_state = await langgraph_app.ainvoke(
            {"messages": [HumanMessage(content=request.question)]}, config=config
        )

        response_messages = [serialize_message(msg) for msg in final_state["messages"]]
        return {
            "session_id": actual_session_id,
            "messages": response_messages,
            "tool_call_summary": {
                msg["name"] if "name" in msg else msg["type"]: 1 for msg in response_messages if msg["role"] == "tool"
            },
        }
    except Exception as e:
        import traceback

        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Execution error: {str(e) or type(e).__name__}")


@app.post("/chat/stream")
async def chat_stream_endpoint(request: ChatRequestMulti):
    """Streaming stateful chat endpoint. Persistent history is loaded and updated."""
    await lazy_init_app(model_name=request.model_name)

    actual_session_id = request.session_id or str(uuid.uuid4())
    stream_queue = asyncio.Queue()
    config = {"configurable": {"thread_id": actual_session_id, "stream_queue": stream_queue}}

    async def run_graph():
        try:
            async for event in langgraph_app.astream(
                {"messages": [HumanMessage(content=request.question)]}, config=config, stream_mode="updates"
            ):
                await stream_queue.put({"type": "graph", "event": event})
            await stream_queue.put({"type": "done"})
        except Exception as e:
            import traceback

            traceback.print_exc()
            await stream_queue.put({"type": "error", "error": str(e) or type(e).__name__})

    async def event_generator():
        task = asyncio.create_task(run_graph())
        try:
            while True:
                item = await stream_queue.get()
                if item["type"] == "done":
                    break
                elif item["type"] == "error":
                    yield json.dumps({"error": item["error"]}, ensure_ascii=False) + "\n"
                    break
                elif item["type"] == "graph":
                    event = item["event"]
                    formatted_event = {"session_id": actual_session_id}
                    for node_name, update in event.items():
                        msgs = update.get("messages", [])
                        serialized_msgs = [serialize_message(msg) for msg in msgs]
                        formatted_event[node_name] = {"messages": serialized_msgs}
                    yield json.dumps(formatted_event, ensure_ascii=False) + "\n"
                elif item["type"] == "progress":
                    yield json.dumps(item["event"], ensure_ascii=False) + "\n"
        finally:
            task.cancel()

    return StreamingResponse(event_generator(), media_type="application/x-ndjson")


@app.get("/chat/history/{session_id}")
async def get_session_history(session_id: str):
    """Retrieve full conversation history of a specific session ID from checkpointer."""
    await lazy_init_app()

    config = {"configurable": {"thread_id": session_id}}
    try:
        state = await langgraph_app.aget_state(config)
        messages = state.values.get("messages", []) if state.values else []
        return {"session_id": session_id, "messages": [serialize_message(msg) for msg in messages]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch session history: {e}")


# ── Chat UI Static Hosting ────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def serve_ui_root():
    """Serve built-in React Chat UI at root URL."""
    ui_dist = os.path.join(os.path.dirname(__file__), "wren-chat-ui", "dist")
    index_html = os.path.join(ui_dist, "index.html")
    if os.path.isfile(index_html):
        return FileResponse(index_html)
    return HTMLResponse(
        content="""<!DOCTYPE html><html><head><title>Wren AI Chat</title></head>
<body><h2>Wren AI Chat UI is not built yet.</h2>
<p>To enable the web UI, build the frontend:</p>
<pre>cd sdk/wren-langchain/examples/wren-chat-ui && npm install && npm run build</pre>
</body></html>""",
        status_code=200,
    )


@app.get("/assets/{file_path:path}", include_in_schema=False)
async def serve_ui_assets(file_path: str):
    """Serve built-in React Chat UI static assets."""
    ui_dist = os.path.join(os.path.dirname(__file__), "wren-chat-ui", "dist")
    asset_file = os.path.join(ui_dist, "assets", file_path)
    if os.path.isfile(asset_file):
        return FileResponse(asset_file)
    raise HTTPException(status_code=404, detail="Asset not found")


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8201"))
    print(f"Starting Stateful FastAPI server on http://0.0.0.0:{port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)
