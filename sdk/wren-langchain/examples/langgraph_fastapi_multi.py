"""FastAPI server wrapper with PERSISTENT MULTI-TURN SESSIONS and OFFLINE Swagger UI support.

This version leverages LangGraph's native MemorySaver checkpointer by default,
or automatically upgrades to MySQL database persistence if the CHAT_HISTORY_DB_URI
environment variable is configured.

Prerequisites
=============
    pip install fastapi uvicorn httpx
    # For MySQL persistence (optional, automatically used if CHAT_HISTORY_DB_URI is set):
    pip install langgraph-checkpoint-mysql[pymysql]
    
    # standard dependencies matching langgraph_demo.py:
    pip install langchain-openai langgraph
    export OPENAI_API_KEY=sk-...
    export PROJECT_PATH=/path/to/your-wren-project

Running the Server
==================
    python examples/langgraph_fastapi_multi.py
    # Or: uvicorn examples.langgraph_fastapi_multi:app --host 0.0.0.0 --port 8000 --reload

API Endpoints
=============
1. Swagger UI (Offline-ready):
   GET /docs

2. Standard Session Chat:
   POST /chat
   Body: 
   {
       "question": "统计自杀事件数量",
       "session_id": "optional-uuid-or-session-name"  # If omitted, server generates and returns one
   }

3. Streaming Session Chat:
   POST /chat/stream
   Body: 
   {
       "question": "哪些数据模型被加载了？",
       "session_id": "optional-uuid-or-session-name"
   }
   Returns: NDJSON stream of LangGraph node updates (node by node).

4. Retrieve Session History:
   GET /chat/history/{session_id}
   Returns: The full history of the conversation for the given session.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
import glob
import asyncio
import threading
from typing import Annotated, TypedDict, Literal, Any, Type, Dict, List
from contextlib import asynccontextmanager, AsyncExitStack

try:
    from fastapi import FastAPI, HTTPException, Response
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import StreamingResponse, HTMLResponse
    from fastapi.openapi.docs import get_swagger_ui_html
    from pydantic import BaseModel, Field, create_model
    import uvicorn
    import httpx
except ImportError:
    sys.exit("fastapi, uvicorn, pydantic, and httpx are required.\nRun: pip install fastapi uvicorn pydantic httpx")

try:
    from langchain_openai import ChatOpenAI
except ImportError:
    sys.exit("langchain-openai is not installed.\nRun: pip install langchain-openai")

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import tool, StructuredTool
import datetime
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from wren_langchain import WrenToolkit


# ── Global MCP State & Helper Functions ─────────────────────────────────

mcp_sessions: dict[str, Any] = {}
mcp_configs_registry: dict[str, dict] = {}
mcp_exit_stacks: dict[str, AsyncExitStack] = {}
mcp_locks: dict[str, asyncio.Lock] = {}
global_mcp_tools: list[StructuredTool] = []
initialized_mcp_servers: set[str] = set()
mcp_retry_task: asyncio.Task | None = None
mcp_manager_queue: asyncio.Queue = asyncio.Queue()
mcp_manager_task: asyncio.Task | None = None
mysql_exit_stack: AsyncExitStack = AsyncExitStack()


async def _real_get_or_create_mcp_session(server_name: str, force_reconnect: bool = False) -> Any:
    """Get or establish an active session with the specified MCP server, recreating it if forced."""
    if not force_reconnect and server_name in mcp_sessions:
        return mcp_sessions[server_name]
        
    config = mcp_configs_registry.get(server_name)
    if not config:
        raise ValueError(f"No configuration found for MCP server '{server_name}'")
        
    # Clean up existing stack for this server if it exists
    if server_name in mcp_exit_stacks:
        print(f"Closing existing connection stack for MCP server '{server_name}'...")
        try:
            await mcp_exit_stacks[server_name].aclose()
        except Exception as e:
            print(f"Error closing exit stack for '{server_name}': {e}")
        mcp_exit_stacks.pop(server_name, None)
        mcp_sessions.pop(server_name, None)
        
    stack = AsyncExitStack()
    mcp_exit_stacks[server_name] = stack
    
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.sse import sse_client
        from mcp.client.streamable_http import streamable_http_client
        
        if "url" in config:
            url = config["url"]
            transport_type = config.get("type", "").lower()
            if not transport_type:
                if "/mcp" in url:
                    transport_type = "streamable_http"
                else:
                    transport_type = "sse"
            
            # Retrieve timeouts from environment variables with safe defaults (10 minutes read, 60s connect)
            connect_timeout = float(os.environ.get("MCP_CONNECT_TIMEOUT", "60.0"))
            read_timeout = float(os.environ.get("MCP_READ_TIMEOUT", "600.0"))
            
            headers = config.get("headers")
            if transport_type in ("streamable_http", "streamable-http", "http"):
                print(f"Connecting to remote MCP server '{server_name}' via Streamable HTTP: {url} (timeout: connect={connect_timeout}s, read={read_timeout}s)")
                client = await stack.enter_async_context(
                    httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(read_timeout, connect=connect_timeout))
                )
                res = await stack.enter_async_context(streamable_http_client(url, http_client=client))
                read_stream, write_stream = res[0], res[1]
            else:
                print(f"Connecting to remote MCP server '{server_name}' via SSE: {url} (timeout: connect={connect_timeout}s, read={read_timeout}s)")
                read_stream, write_stream = await stack.enter_async_context(
                    sse_client(url, headers=headers, timeout=connect_timeout, sse_read_timeout=read_timeout)
                )
            
            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()
            mcp_sessions[server_name] = session
            return session
        elif "command" in config:
            cmd = config["command"]
            args = config.get("args", [])
            env = os.environ.copy()
            if "env" in config and isinstance(config["env"], dict):
                env.update(config["env"])
            print(f"Starting local MCP server '{server_name}' via Stdio: {cmd} {' '.join(args)}")
            server_params = StdioServerParameters(command=cmd, args=args, env=env)
            read_stream, write_stream = await stack.enter_async_context(stdio_client(server_params))
            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()
            mcp_sessions[server_name] = session
            return session
        else:
            raise ValueError(f"Neither 'url' nor 'command' provided for MCP server '{server_name}'")
    except Exception as e:
        await stack.aclose()
        mcp_exit_stacks.pop(server_name, None)
        mcp_sessions.pop(server_name, None)
        raise e


async def mcp_manager_worker():
    """Persistent task executing all MCP connection/disconnection context operations on a single task context to prevent task-mismatch cancel scope errors."""
    while True:
        try:
            item = await mcp_manager_queue.get()
            if item is None:
                mcp_manager_queue.task_done()
                break
                
            cmd, args, future = item
            try:
                if cmd == "connect":
                    server_name, force_reconnect = args
                    session = await _real_get_or_create_mcp_session(server_name, force_reconnect)
                    if not future.cancelled():
                        future.set_result(session)
                elif cmd == "shutdown":
                    # Clean up all active sessions under this task's context
                    for server_name in list(mcp_exit_stacks.keys()):
                        try:
                            print(f"Closing exit stack for MCP server '{server_name}' inside manager task context...")
                            stack = mcp_exit_stacks.pop(server_name, None)
                            if stack:
                                await stack.aclose()
                        except Exception as e:
                            print(f"Error closing exit stack for '{server_name}': {e}")
                    mcp_sessions.clear()
                    mcp_exit_stacks.clear()
                    if not future.cancelled():
                        future.set_result(True)
                    mcp_manager_queue.task_done()
                    break
            except Exception as e:
                if not future.cancelled():
                    future.set_exception(e)
            finally:
                mcp_manager_queue.task_done()
        except asyncio.CancelledError:
            print("[MCP MANAGER] Manager worker task cancelled.")
            break
        except Exception as worker_err:
            print(f"[MCP MANAGER] Unexpected error in manager loop: {worker_err}")


async def get_or_create_mcp_session(server_name: str, force_reconnect: bool = False) -> Any:
    """Delegate establishment of an active session to the dedicated MCP manager task."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    await mcp_manager_queue.put(("connect", (server_name, force_reconnect), future))
    return await future


def json_schema_to_pydantic(name: str, schema: dict[str, Any]) -> Type[BaseModel]:
    """Convert a JSON Schema dict into a Pydantic BaseModel class for tool validation."""
    fields = {}
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    
    for prop_name, prop_info in properties.items():
        prop_type = Any
        type_str = prop_info.get("type")
        if type_str == "string":
            prop_type = str
        elif type_str == "integer":
            prop_type = int
        elif type_str == "number":
            prop_type = float
        elif type_str == "boolean":
            prop_type = bool
        elif type_str == "array":
            prop_type = list
        elif type_str == "object":
            prop_type = dict
            
        desc = prop_info.get("description", "")
        # Set default to Ellipsis if required, otherwise default to None or specified default
        default = ... if prop_name in required else prop_info.get("default", None)
        
        fields[prop_name] = (prop_type, Field(default=default, description=desc))
        
    return create_model(name, **fields)


def load_mcp_configs(config_dir: str) -> dict[str, dict]:
    """Scan and merge MCP configurations from all .json files in the specified directory."""
    servers = {}
    if not os.path.isdir(config_dir):
        print(f"Warning: MCP_CONFIG_DIR '{config_dir}' is not a directory.")
        return servers

    # Scan all JSON files in the config directory
    json_pattern = os.path.join(config_dir, "*.json")
    for file_path in glob.glob(json_pattern):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    if "mcpServers" in data and isinstance(data["mcpServers"], dict):
                        servers.update(data["mcpServers"])
                    else:
                        servers.update(data)
        except Exception as e:
            print(f"Error reading MCP config file {file_path}: {e}")
            
    return servers


def convert_mcp_to_langchain(server_name: str, mcp_tool: Any) -> StructuredTool:
    """Convert an MCP Tool definition into a LangChain StructuredTool."""
    tool_name = mcp_tool.name
    tool_desc = mcp_tool.description
    input_schema = mcp_tool.inputSchema
    
    # Dynamically create Pydantic args_schema for validation
    args_schema = None
    if isinstance(input_schema, dict) and input_schema.get("properties"):
        try:
            class_name = f"MCP_{server_name}_{tool_name}_Args"
            args_schema = json_schema_to_pydantic(class_name, input_schema)
        except Exception as e:
            print(f"Warning: failed to generate Pydantic model for {server_name}.{tool_name}: {e}")
            
    # Asynchronous tool execution
    # Asynchronous tool execution
    async def _acall(*args, **kwargs) -> str:
        # Extract config if present (LangChain passes it as keyword or positional argument)
        config = kwargs.pop("config", None)
        if not config and args:
            from langchain_core.runnables import RunnableConfig
            if isinstance(args[0], RunnableConfig):
                config = args[0]
                args = args[1:]

        # Extract queue if present in config
        stream_queue = None
        if config and "configurable" in config:
            stream_queue = config["configurable"].get("stream_queue")

        async def progress_callback(progress: float, total: float | None = None, message: str | None = None):
            print(f"[MCP PROGRESS] {server_name}.{tool_name}: {progress}/{total} - {message}")
            if stream_queue:
                progress_event = {
                    "session_id": config["configurable"].get("thread_id"),
                    "progress": {
                        "server_name": server_name,
                        "tool_name": tool_name,
                        "progress": int(progress),
                        "total": int(total) if total is not None else 10,
                        "message": message or f"Processing step {progress}"
                    }
                }
                await stream_queue.put({"type": "progress", "event": progress_event})

        try:
            session = await get_or_create_mcp_session(server_name)
        except Exception as e:
            print(f"[MCP CLIENT] Failed to connect to MCP server '{server_name}': {e}")
            return f"Error: Failed to connect to MCP server '{server_name}': {e}"
        try:
            print(f"[MCP CLIENT] Calling tool '{tool_name}' on server '{server_name}' with args: {kwargs}...")
            result = await session.call_tool(tool_name, kwargs, progress_callback=progress_callback)
            print(f"[MCP CLIENT] Tool '{tool_name}' returned result content length: {len(result.content)}")
            text_contents = []
            for content in result.content:
                if hasattr(content, "text"):
                    text_contents.append(content.text)
                elif isinstance(content, dict) and "text" in content:
                    text_contents.append(content["text"])
            return "\n".join(text_contents)
        except Exception as e:
            print(f"[MCP CLIENT] Error invoking tool '{tool_name}' on server '{server_name}': {e}. Attempting reconnection...")
            try:
                session = await get_or_create_mcp_session(server_name, force_reconnect=True)
                print(f"[MCP CLIENT] Reconnected. Retrying tool '{tool_name}' with args: {kwargs}...")
                result = await session.call_tool(tool_name, kwargs, progress_callback=progress_callback)
                print(f"[MCP CLIENT] Retry succeeded. Result content length: {len(result.content)}")
                text_contents = []
                for content in result.content:
                    if hasattr(content, "text"):
                        text_contents.append(content.text)
                    elif isinstance(content, dict) and "text" in content:
                        text_contents.append(content["text"])
                return "\n".join(text_contents)
            except Exception as retry_err:
                print(f"[MCP CLIENT] Retry failed: {retry_err}")
                return f"Error invoking MCP tool '{tool_name}' on server '{server_name}' after retry: {retry_err}"
            
    # Synchronous wrapper calling asynchronous execute
    def _call(*args, **kwargs) -> str:
        import anyio
        import functools
        try:
            return anyio.from_thread.run(functools.partial(_acall, *args, **kwargs))
        except RuntimeError:
            # Fallback if no anyio event loop running in the current thread context
            return asyncio.run(_acall(*args, **kwargs))
            
    # Prefix the tool name to avoid collisions across different servers
    prefixed_name = f"{server_name}_{tool_name}"
    prefixed_name = prefixed_name.replace("-", "_").replace(" ", "_")
    
    return StructuredTool(
        name=prefixed_name,
        description=tool_desc or f"Invoke {tool_name} from MCP server {server_name}",
        func=_call,
        coroutine=_acall,
        args_schema=args_schema
    )


# ── LangGraph Setup (adapted from langgraph_demo.py with Adaptive Checkpointer) ───

class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


@tool("get_current_time")
def get_current_time() -> str:
    """Get the current system date and time of the host machine.
    
    Call this tool when you need to write reports, resolve date ranges,
    or know the current date and time for statistical purposes.
    """
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_wren_system_prompt(messages: list[BaseMessage], system_prompt: str) -> list[BaseMessage]:
    """Ensure the Wren system prompt is present in the message list without duplicating or overriding existing system slots."""
    if not system_prompt:
        return list(messages)
        
    # Check if a SystemMessage with identical content already exists
    for msg in messages:
        if isinstance(msg, SystemMessage) and msg.content == system_prompt:
            return list(messages)
            
    # Find the boundary of leading SystemMessages (if any)
    insert_idx = 0
    while insert_idx < len(messages) and isinstance(messages[insert_idx], SystemMessage):
        insert_idx += 1
        
    # Insert Wren system prompt right after leading system messages (or at 0 if no SystemMessage)
    new_messages = list(messages)
    new_messages.insert(insert_idx, SystemMessage(content=system_prompt))
    return new_messages


def build_app(toolkit: WrenToolkit, checkpointer: Any = None, model_name: str = "gpt-4o"):
    """Compile a ReAct graph that uses Wren tools and binds the provided checkpointer (or compiles statelessly if checkpointer=None)."""
    tools = toolkit.get_tools()
    tools.append(get_current_time)
    
    # Extend with discovered MCP tools
    if global_mcp_tools:
        print(f"Binding {len(global_mcp_tools)} MCP tools to the agent graph...")
        tools.extend(global_mcp_tools)
        
    system_prompt = toolkit.system_prompt()
    
    # Allow custom API base and key via environment variables for compatibility
    api_base = os.getenv("LLM_API_BASE") or os.getenv("OPENAI_API_BASE")
    api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    model_to_use = os.getenv("LLM_MODEL_NAME", model_name)

    print(f"--- Configured LLM: {model_to_use} | API Base: {api_base} ---")

    # Keep a cached instance to preserve connection pooling/performance under normal conditions
    model_with_tools = None

    def get_model():
        nonlocal model_with_tools
        if model_with_tools is None:
            model_with_tools = ChatOpenAI(
                model=model_to_use, 
                base_url=api_base,
                api_key=api_key,
                temperature=0,
                request_timeout=60.0, # 60s request timeout to prevent LLM network/inference hang
                model_kwargs={
                    "extra_body": {
                        "option": {
                            "num_ctx": 1048576  # 1M tokens
                        }
                    }
                }
            ).bind_tools(tools)
        return model_with_tools

    def agent_node(state: AgentState) -> dict:
        nonlocal model_with_tools
        messages = state["messages"]
        messages = ensure_wren_system_prompt(messages, system_prompt)
        
        print(f"\n[LLM Request] Model to call: {model_to_use}")
        
        try:
            model = get_model()
            response = model.invoke(messages)
        except Exception as e:
            # Catch LLM connection/invocation errors, reset client session and retry
            print(f"LLM invocation failed: {e}. Resetting client connection pool and retrying...")
            model_with_tools = None  # Discard the broken client session
            
            # Retry with a fresh client session
            model = get_model()
            response = model.invoke(messages)
            
        print(f"[LLM Response Metadata] Received metadata: {response.response_metadata}\n")
        
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


# ── Global state and Lifespan ──────────────────────────────────────────

# Global toolkit and compiled langgraph app instances
toolkit: WrenToolkit | None = None
langgraph_app = None
langgraph_app_stateless = None


async def retry_failed_mcp_connections_loop():
    """Background task to periodically retry connecting to failed MCP servers and load their tools."""
    global langgraph_app, langgraph_app_stateless
    retry_interval = float(os.environ.get("MCP_RETRY_INTERVAL", "30.0"))
    while True:
        try:
            failed_servers = set(mcp_configs_registry.keys()) - initialized_mcp_servers
            if not failed_servers:
                print("[MCP RETRY] All MCP servers are successfully initialized. Background retry loop exiting.")
                break
                
            await asyncio.sleep(retry_interval)
            
            # Check again after sleep
            failed_servers = set(mcp_configs_registry.keys()) - initialized_mcp_servers
            if not failed_servers:
                break
                
            print(f"[MCP RETRY] Retrying connection for failed MCP servers: {list(failed_servers)}")
            for server_name in failed_servers:
                try:
                    async def _try_connect():
                        session = await get_or_create_mcp_session(server_name, force_reconnect=True)
                        tools_result = await session.list_tools()
                        print(f"[MCP RETRY] Successfully connected to '{server_name}'! Discovered {len(tools_result.tools)} tools: {[t.name for t in tools_result.tools]}")
                        
                        new_tools = []
                        for mcp_tool in tools_result.tools:
                            lc_tool = convert_mcp_to_langchain(server_name, mcp_tool)
                            new_tools.append(lc_tool)
                        
                        global_mcp_tools.extend(new_tools)
                        initialized_mcp_servers.add(server_name)
                        
                        global langgraph_app, langgraph_app_stateless
                        langgraph_app = None
                        langgraph_app_stateless = None
                        print(f"[MCP RETRY] Registered tools for '{server_name}' and cleared compiled LangGraph app cache.")

                    await asyncio.wait_for(_try_connect(), timeout=15.0)
                except Exception as e:
                    print(f"[MCP RETRY] Connection or initialization attempt to '{server_name}' failed: {e}")
        except asyncio.CancelledError:
            print("[MCP RETRY] Background retry loop cancelled.")
            break
        except Exception as loop_err:
            print(f"[MCP RETRY] Error in background loop: {loop_err}")
            await asyncio.sleep(retry_interval)# ── Module-Level Reconnecting PyMySQL Checkpointer ─────────────────────

try:
    from langgraph.checkpoint.mysql.pymysql import PyMySQLSaver
    from contextlib import contextmanager

    class ReconnectingPyMySQLSaver(PyMySQLSaver):
        """Thread-safe and reconnecting PyMySQL Saver using threading.RLock to prevent reentrant deadlocks."""
        def __init__(self, *args, conn_args: dict = None, **kwargs):
            super().__init__(*args, **kwargs)
            self.conn_args = conn_args
            # CRITICAL FIX: Override parent's Lock with RLock to prevent reentrant deadlocks during super().setup() / super().get_tuple()
            self.lock = threading.RLock()

        def _ping_unlocked(self):
            try:
                if self.conn:
                    self.conn.ping()
            except Exception as e:
                print(f"Failed to ping/reconnect MySQL database: {e}. Attempting clean reconnection...")
                try:
                    try:
                        self.conn.close()
                    except Exception:
                        pass
                    import pymysql
                    if self.conn_args:
                        conn_kwargs = dict(self.conn_args)
                        conn_kwargs.setdefault("autocommit", True)
                        self.conn = pymysql.connect(**conn_kwargs)
                    else:
                        self.conn.connect()
                    print("Successfully re-established clean MySQL connection!")
                except Exception as conn_err:
                    print(f"Failed to force clean MySQL connection: {conn_err}")
                    raise conn_err

        def setup(self, *args, **kwargs):
            with self.lock:
                self._ping_unlocked()
                return super().setup(*args, **kwargs)

        def get_tuple(self, *args, **kwargs):
            with self.lock:
                self._ping_unlocked()
                return super().get_tuple(*args, **kwargs)

        def list(self, *args, **kwargs):
            with self.lock:
                self._ping_unlocked()
                return list(super().list(*args, **kwargs))

        def put(self, *args, **kwargs):
            with self.lock:
                self._ping_unlocked()
                return super().put(*args, **kwargs)

        def put_writes(self, *args, **kwargs):
            with self.lock:
                self._ping_unlocked()
                return super().put_writes(*args, **kwargs)

        async def aget_tuple(self, config: RunnableConfig):
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self.get_tuple, config)

        async def aput(self, config: RunnableConfig, checkpoint, metadata, new_versions):
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self.put, config, checkpoint, metadata, new_versions)

        async def aput_writes(self, config: RunnableConfig, writes, task_id, task_path=""):
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self.put_writes, config, writes, task_id, task_path)

        async def alist(self, config: RunnableConfig | None, *, filter=None, before=None, limit=None):
            loop = asyncio.get_running_loop()
            def _sync_list():
                return list(self.list(config, filter=filter, before=before, limit=limit))
            items = await loop.run_in_executor(None, _sync_list)
            for item in items:
                yield item

        @classmethod
        @contextmanager
        def from_conn_string(cls, conn_string: str):
            import urllib.parse
            import pymysql
            
            parsed = urllib.parse.urlparse(conn_string)
            user = parsed.username
            password = parsed.password
            if password:
                password = urllib.parse.unquote(password)
            if user:
                user = urllib.parse.unquote(user)
            database = parsed.path.lstrip('/')
            if database:
                database = urllib.parse.unquote(database)
            
            # Valid keyword arguments for pymysql.connect
            valid_keys = {
                "host", "port", "user", "password", "database", "charset",
                "sql_mode", "read_default_file", "conv", "use_unicode",
                "client_flag", "cursorclass", "ssl", "read_timeout",
                "write_timeout", "connect_timeout", "autocommit", "ssl_disabled"
            }

            conn_args = {
                "host": parsed.hostname or "localhost",
                "port": parsed.port or 3306,
                "user": user,
                "password": password or "",
                "database": database,
                "connect_timeout": 5,
                "read_timeout": 15,
                "write_timeout": 15,
                "ssl_disabled": True,
                "autocommit": True,
                "init_command": "SET SESSION lock_wait_timeout = 5"
            }

            if parsed.query:
                params = urllib.parse.parse_qs(parsed.query)
                for k, v in params.items():
                    k_lower = k.lower()
                    if k_lower in valid_keys and k_lower != "init_command":
                        val = v[0]
                        if val.lower() == "true":
                            val = True
                        elif val.lower() == "false":
                            val = False
                        elif val.isdigit():
                            val = int(val)
                        conn_args[k_lower] = val
            
            print(f"[MySQL] Connecting to MySQL checkpointer at {conn_args['host']}:{conn_args['port']}/{database} (timeout: 5s)...", flush=True)
            conn = pymysql.connect(**conn_args)
            try:
                saver = cls(conn=conn, serde=None, conn_args=conn_args)
                yield saver
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
except ImportError:
    ReconnectingPyMySQLSaver = None


# ── Global state and Lifespan ──────────────────────────────────────────

# Global toolkit and compiled langgraph app instances
toolkit: WrenToolkit | None = None
langgraph_app = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle context manager to initialize the Wren Toolkit, MCP sessions, and checkpointer at startup."""
    global toolkit, langgraph_app, mcp_sessions, global_mcp_tools, mcp_retry_task, mcp_manager_task, mysql_exit_stack
    
    print("[LIFESPAN] Starting dual-track server lifespan initialization...", flush=True)

    # Start persistent MCP manager task
    mcp_manager_task = asyncio.create_task(mcp_manager_worker())
    
    # 1. Initialize Wren Toolkit
    project_path = os.environ.get("PROJECT_PATH")
    if project_path:
        project_path = project_path.strip().strip('"').strip("'")
        try:
            print(f"[LIFESPAN] Initializing WrenToolkit from project: {project_path}...", flush=True)
            # Run in worker thread to prevent blocking Uvicorn startup loop
            toolkit = await asyncio.to_thread(WrenToolkit.from_project, project_path)
            print("[LIFESPAN] Successfully initialized WrenToolkit!", flush=True)
        except Exception as e:
            print(f"[LIFESPAN Warning] Error initializing WrenToolkit during lifespan startup: {e}. Will lazy-initialize on first request.", flush=True)

    if not os.environ.get("OPENAI_API_KEY"):
        print("[LIFESPAN Warning] OPENAI_API_KEY is not set. Custom endpoint configuration or key will be required.", flush=True)

    # 2. Initialize MCP Sessions
    mcp_config_dir = os.environ.get("MCP_CONFIG_DIR")
    if mcp_config_dir:
        mcp_config_dir = mcp_config_dir.strip().strip('"').strip("'")
        mcp_configs = load_mcp_configs(mcp_config_dir)
        if mcp_configs:
            print(f"Loaded {len(mcp_configs)} MCP server configs: {list(mcp_configs.keys())}")
            mcp_configs_registry.update(mcp_configs)
            try:
                from mcp import ClientSession
                
                for server_name in mcp_configs.keys():
                    try:
                        async def _connect_server():
                            session = await get_or_create_mcp_session(server_name)
                            tools_result = await session.list_tools()
                            print(f"Discovered {len(tools_result.tools)} tools from MCP server '{server_name}': {[t.name for t in tools_result.tools]}")
                            
                            for mcp_tool in tools_result.tools:
                                lc_tool = convert_mcp_to_langchain(server_name, mcp_tool)
                                global_mcp_tools.append(lc_tool)
                            initialized_mcp_servers.add(server_name)

                        await asyncio.wait_for(_connect_server(), timeout=15.0)
                    except asyncio.TimeoutError:
                        print(f"Failed to connect to MCP server '{server_name}': Connection or initialization timed out after 15.0 seconds.")
                    except Exception as e:
                        print(f"Failed to connect to MCP server '{server_name}': {e}")
                
                # Start background reconnect task for failed servers
                failed_servers = set(mcp_configs.keys()) - initialized_mcp_servers
                if failed_servers:
                    global mcp_retry_task
                    print(f"Starting background reconnect task for failed MCP servers: {list(failed_servers)}")
                    mcp_retry_task = asyncio.create_task(retry_failed_mcp_connections_loop())
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
                        host_port = "127.0.0.1" + host_port[len("localhost"):]
                    elif host_port == "localhost":
                        host_port = "127.0.0.1"
                    new_netloc = f"{user_pass}@{host_port}"
                else:
                    if netloc.startswith("localhost:"):
                        new_netloc = "127.0.0.1" + netloc[len("localhost"):]
                    elif netloc == "localhost":
                        new_netloc = "127.0.0.1"
                parsed = parsed._replace(netloc=new_netloc)
                db_uri = urlunparse(parsed)
                print(f"Auto-resolved 'localhost' to '127.0.0.1' in database URI for Windows compatibility.")
        except Exception as e:
            print(f"Warning: Failed to auto-resolve localhost in CHAT_HISTORY_URI: {e}")

    checkpointer = None
    
    if db_uri and toolkit and ReconnectingPyMySQLSaver:
        print("CHAT_HISTORY_DB_URI detected. Attempting to initialize MySQL checkpointer...")
        try:
            if "autocommit" not in db_uri.lower():
                separator = "&" if "?" in db_uri else "?"
                db_uri = f"{db_uri}{separator}autocommit=true"

            # Connect inside thread pool to prevent blocking main loop if socket hangs
            checkpointer_candidate = await asyncio.to_thread(
                lambda: mysql_exit_stack.enter_context(ReconnectingPyMySQLSaver.from_conn_string(db_uri))
            )
            print("[MySQL] Setting up checkpointer database tables (timeout: 10s)...", flush=True)
            try:
                await asyncio.wait_for(asyncio.to_thread(checkpointer_candidate.setup), timeout=10.0)
                checkpointer = checkpointer_candidate
                print("[MySQL] Successfully initialized persistent MySQL checkpointer with auto-reconnection!", flush=True)
            except (asyncio.TimeoutError, Exception) as setup_err:
                print(f"[MySQL Warning] Checkpointer table setup failed/timed out: {setup_err}. Cleaning up connection leakage and falling back to in-memory MemorySaver.", flush=True)
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

    # Build LangGraph apps (both stateful for native endpoints and stateless for OpenAI/DEEIX endpoint)
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
                
        # Gracefully shut down MCP sessions inside the manager task context
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


# Disable default docs routes to intercept them for offline support
app = FastAPI(
    title="Wren LangGraph Stateful API Server",
    description="Expose a Wren-aware LangGraph ReAct agent via stateful, persistent session-based endpoints.",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)

# Enable CORS middleware to allow cross-origin requests from browsers
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Pydantic Request Models ────────────────────────────────────────────

class ChatRequestMulti(BaseModel):
    question: str = Field(..., description="The user's analytical question.")
    session_id: str | None = Field(
        default=None, 
        description="Optional unique session identifier. If not provided, a new one will be generated."
    )
    model_name: str = Field(
        default="gpt-4o", 
        description="Model name to override if needed."
    )


class OpenAIMessage(BaseModel):
    role: str = Field(..., description="Role of the message: user, assistant, system, or tool")
    content: str | list[dict[str, Any]] | None = Field(default="", description="Message content")
    name: str | None = None
    tool_call_id: str | None = None


class OpenAIChatCompletionRequest(BaseModel):
    model: str = Field(default="wren-agent", description="Model name requested")
    messages: list[OpenAIMessage] = Field(..., description="Array of conversation messages")
    stream: bool = Field(default=False, description="Whether to stream response chunks over SSE")
    temperature: float | None = 0.7
    top_p: float | None = 1.0
    user: str | None = Field(default=None, description="Optional user/session identifier")




# ── Helpers for message serialization ───────────────────────────────────

def serialize_message(msg: BaseMessage) -> dict:
    """Format a LangChain Message object into a JSON-compatible dictionary."""
    kind = type(msg).__name__
    role = "user"
    if isinstance(msg, AIMessage):
        role = "assistant"
    elif isinstance(msg, SystemMessage):
        role = "system"
    elif isinstance(msg, ToolMessage):
        role = "tool"
        
    result = {
        "role": role,
        "type": kind,
        "content": getattr(msg, "content", ""),
    }
    
    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls:
        result["tool_calls"] = tool_calls
    if isinstance(msg, ToolMessage):
        result["name"] = msg.name
        result["tool_call_id"] = msg.tool_call_id
        
    return result


# ── Automated Offline/Online Hybrid Caching Swagger Assets ───────────────

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def get_local_or_cdn(filename: str, cdn_url: str, media_type: str) -> Response:
    """Retrieve the asset locally if cached; otherwise fetch from CDN and save locally."""
    os.makedirs(STATIC_DIR, exist_ok=True)
    local_path = os.path.join(STATIC_DIR, filename)
    
    if os.path.exists(local_path):
        with open(local_path, "r", encoding="utf-8") as f:
            return Response(content=f.read(), media_type=media_type)
            
    # Try fetching from CDN and caching locally
    try:
        print(f"Downloading {filename} from CDN to cache for offline usage...")
        response = httpx.get(cdn_url, timeout=15.0)
        response.raise_for_status()
        content = response.text
        # Save locally for future offline runs
        with open(local_path, "w", encoding="utf-8") as f:
            f.write(content)
        return Response(content=content, media_type=media_type)
    except Exception as e:
        print(f"Failed to fetch {filename} from CDN: {e}")
        raise HTTPException(
            status_code=503, 
            detail=f"Static asset {filename} is not cached locally, and the CDN is unreachable."
        )


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def serve_chat_ui():
    """Serve the compiled frontend chat UI or an interactive status dashboard at the root path."""
    dist_html_path = os.path.join(
        os.path.dirname(__file__), 
        "wren-chat-ui", 
        "dist", 
        "index.html"
    )
    if os.path.exists(dist_html_path):
        try:
            with open(dist_html_path, "r", encoding="utf-8") as f:
                return HTMLResponse(content=f.read())
        except Exception as e:
            pass
            
    dashboard_html = f"""
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>WrenAI Dual-Track Agent Platform</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: #0f172a; color: #f8fafc; margin: 0; padding: 40px; display: flex; justify-content: center; }}
            .card {{ background: #1e293b; border: 1px solid #334155; border-radius: 16px; padding: 32px; max-width: 720px; width: 100%; box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.3); }}
            h1 {{ color: #38bdf8; margin-top: 0; display: flex; align-items: center; gap: 12px; font-size: 24px; }}
            .status-badge {{ background: #166534; color: #4ade80; padding: 4px 12px; border-radius: 9999px; font-size: 12px; font-weight: 600; text-transform: uppercase; }}
            p {{ color: #94a3b8; line-height: 1.6; }}
            .endpoint-group {{ margin-top: 24px; background: #0f172a; border-radius: 8px; padding: 16px; border: 1px solid #1e293b; }}
            .endpoint-title {{ font-size: 14px; font-weight: 600; color: #cbd5e1; margin-bottom: 8px; }}
            .url-box {{ background: #1e293b; padding: 8px 12px; border-radius: 6px; font-family: monospace; color: #a5f3fc; font-size: 13px; word-break: break-all; margin-bottom: 8px; display: block; }}
            a {{ color: #38bdf8; text-decoration: none; font-weight: 500; }}
            a:hover {{ text-decoration: underline; }}
            .tag {{ background: #334155; color: #e2e8f0; font-size: 11px; padding: 2px 8px; border-radius: 4px; font-family: monospace; }}
        </style>
    </head>
    <body>
        <div class="card">
            <h1>WrenAI Service Active <span class="status-badge">Running</span></h1>
            <p>WrenAI 语义数据层 Agent 服务已成功启动在端口 <code>8201</code>，已原生支持 DEEIX-Chat / OpenAI 标准协议及 MCP 双轨接入。</p>
            
            <div class="endpoint-group">
                <div class="endpoint-title">🚀 路径 B：OpenAI 标准协议端点 (DEEIX-Chat 自定义 Provider)</div>
                <span class="url-box">POST http://&lt;host&gt;:8201/v1/chat/completions</span>
                <span class="url-box">GET  http://&lt;host&gt;:8201/v1/models</span>
                <p style="font-size: 12px; margin: 4px 0 0 0;">在 DEEIX-Chat 后台填入 Base URL <code>http://&lt;host&gt;:8201/v1</code> 即可直接对话。</p>
            </div>

            <div class="endpoint-group">
                <div class="endpoint-title">🔌 路径 A：FastMCP SSE 插件端点</div>
                <span class="url-box">http://&lt;host&gt;:8202/sse</span>
                <p style="font-size: 12px; margin: 4px 0 0 0;">在 DEEIX-Chat 后台 MCP 插件管理添加 URL 即可使用 Wren 工具链。</p>
            </div>

            <div class="endpoint-group">
                <div class="endpoint-title">🛠️ 常用开发与测试入口</div>
                <ul style="padding-left: 20px; margin: 8px 0; color: #cbd5e1;">
                    <li><a href="/docs" target="_blank">Swagger API 在线测试文档 (/docs)</a></li>
                    <li><a href="/health" target="_blank">服务健康状态监控 (/health)</a></li>
                    <li><a href="/v1/models" target="_blank">模型发现列表 (/v1/models)</a></li>
                </ul>
            </div>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=dashboard_html, status_code=200)


@app.get("/docs", include_in_schema=False)
def custom_swagger_ui_html():
    """Serve Swagger UI HTML overriding the CDN URLs with local paths."""
    return get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title=app.title + " - Docs",
        swagger_js_url="/static/swagger-ui-bundle.js",
        swagger_css_url="/static/swagger-ui.css",
        swagger_favicon_url="/static/favicon.png",
    )


@app.get("/static/swagger-ui-bundle.js", include_in_schema=False)
def get_swagger_js():
    return get_local_or_cdn(
        "swagger-ui-bundle.js",
        "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js",
        "application/javascript"
    )


@app.get("/static/swagger-ui.css", include_in_schema=False)
def get_swagger_css():
    return get_local_or_cdn(
        "swagger-ui.css",
        "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css",
        "text/css"
    )


@app.get("/static/favicon.png", include_in_schema=False)
def get_swagger_favicon():
    os.makedirs(STATIC_DIR, exist_ok=True)
    local_path = os.path.join(STATIC_DIR, "favicon.png")
    
    if os.path.exists(local_path):
        with open(local_path, "rb") as f:
            return Response(content=f.read(), media_type="image/png")
            
    try:
        response = httpx.get("https://fastapi.tiangolo.com/img/favicon.png", timeout=15.0)
        response.raise_for_status()
        content = response.content
        with open(local_path, "wb") as f:
            f.write(content)
        return Response(content=content, media_type="image/png")
    except Exception as e:
        return Response(content=b"", media_type="image/png")


# ── Endpoints ──────────────────────────────────────────────────────────

@app.get("/health")
def health_check():
    """Comprehensive health check endpoint providing DB connectivity and toolkit diagnostic status."""
    target_db_status = False
    target_db_error = None
    tools_count = 0

    if toolkit:
        try:
            tools = toolkit.get_tools()
            tools_count = len(tools)
            target_db_status = tools_count > 0
        except Exception as e:
            target_db_error = str(e)
    else:
        target_db_error = "WrenToolkit not initialized. Please verify PROJECT_PATH and models directory."

    checkpointer_status = False
    checkpointer_type = "MemorySaver"
    if langgraph_app and hasattr(langgraph_app, "checkpointer"):
        cp = langgraph_app.checkpointer
        checkpointer_type = type(cp).__name__
        if "MySQL" in checkpointer_type or hasattr(cp, "conn"):
            checkpointer_status = True
        else:
            checkpointer_status = True

    return {
        "status": "healthy" if (toolkit is not None and target_db_status) else "degraded",
        "project_loaded": toolkit is not None,
        "tools_count": tools_count,
        "target_db_connected": target_db_status,
        "target_db_error": target_db_error,
        "checkpointer_type": checkpointer_type,
        "checkpointer_connected": checkpointer_status,
        "stateless_graph_ready": langgraph_app_stateless is not None,
        "memory_enabled": toolkit._memory.enabled if toolkit and hasattr(toolkit, "_memory") else False
    }


def get_exposed_openai_models() -> list[str]:
    """Parse OPENAI_EXPOSED_MODELS environment variable (comma-separated). Fallback to defaults if empty."""
    raw = os.getenv("OPENAI_EXPOSED_MODELS", "").strip()
    if not raw:
        return ["wren-agent", "wren-semantic-analyst"]
    ids = [m.strip() for m in raw.split(",") if m.strip()]
    return ids or ["wren-agent", "wren-semantic-analyst"]


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


def convert_openai_messages(messages: list[OpenAIMessage]) -> list[BaseMessage]:
    """Convert a list of OpenAI API format messages into LangChain BaseMessage objects."""
    lc_messages = []
    for msg in messages:
        content = msg.content or ""
        if isinstance(content, list):
            parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
            content = " ".join(parts)
            
        role = msg.role.lower()
        if role == "user":
            lc_messages.append(HumanMessage(content=content))
        elif role == "assistant":
            lc_messages.append(AIMessage(content=content))
        elif role == "system":
            lc_messages.append(SystemMessage(content=content))
        elif role == "tool":
            lc_messages.append(ToolMessage(content=content, tool_call_id=msg.tool_call_id or "tool_call"))
    return lc_messages


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
            sanitized = re.sub(rf"('{kw}'|\"{kw}\"|{kw})\s*[:=]\s*['\"]?[^'\";\s]+['\"]?", r"\1: ***", sanitized, flags=re.IGNORECASE)
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
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason
            }
        ]
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

def make_sse_keepalive_chunk(completion_id: str, model: str, created_ts: int, style: str) -> str:
    """Generate an SSE keepalive byte stream (either a comment line or an empty delta chunk)."""
    if style == "empty_delta":
        return make_chat_chunk(completion_id, model, created_ts, {})
    return ": keepalive\n\n"


@app.post("/v1/chat/completions")
async def openai_chat_completions(request: OpenAIChatCompletionRequest):
    """OpenAI API compatible chat completion endpoint. Stateless: relies strictly on request messages[]."""
    lazy_init_app(model_name=request.model)

    if not request.messages:
        raise HTTPException(status_code=400, detail="messages field cannot be empty.")

    # 1. Convert full messages array sent by DEEIX-Chat / OpenAI clients into LangChain message objects
    input_messages = convert_openai_messages(request.messages)
    if not input_messages:
        raise HTTPException(status_code=400, detail="No valid messages parsed from request.")

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:16]}"
    created_ts = int(datetime.datetime.now().timestamp())
    graph_input = {"messages": input_messages}
    
    stream_mode = get_openai_process_stream_mode()
    max_tool_chars = get_openai_process_max_tool_chars()

    if not request.stream:
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

            msg_payload = {
                "role": "assistant",
                "content": final_content
            }
            if stream_mode in ("reasoning", "both") and reasoning_traces:
                msg_payload["reasoning_content"] = "\n\n".join(reasoning_traces)

            return {
                "id": completion_id,
                "object": "chat.completion",
                "created": created_ts,
                "model": request.model,
                "choices": [
                    {
                        "index": 0,
                        "message": msg_payload,
                        "finish_reason": "stop"
                    }
                ],
                "usage": {
                    "prompt_tokens": sum(len(str(m.content)) for m in input_messages) // 4,
                    "completion_tokens": len(final_content) // 4,
                    "total_tokens": (sum(len(str(m.content)) for m in input_messages) + len(final_content)) // 4
                }
            }
        except Exception as e:
            import traceback
            traceback.print_exc()
            raise HTTPException(status_code=500, detail=f"Execution error: {str(e)}")

    # Streaming mode (SSE)
    async def event_stream_generator():
        # Initial chunk specifying assistant role
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
                chunks.append(make_chat_chunk(completion_id, request.model, created_ts, {"reasoning_content": trace_text + "\n\n"}))
            if stream_mode in ("text", "both"):
                chunks.append(make_chat_chunk(completion_id, request.model, created_ts, {"content": f"_{trace_text}_\n\n"}))
            return chunks

        keepalive_s = get_openai_sse_keepalive_seconds()
        keepalive_style = get_openai_sse_keepalive_style()

        try:
            while True:
                try:
                    if keepalive_s > 0:
                        item_type, item_data = await asyncio.wait_for(stream_queue.get(), timeout=keepalive_s)
                    else:
                        item_type, item_data = await stream_queue.get()
                except asyncio.TimeoutError:
                    keepalive_count += 1
                    if keepalive_count == 1 or keepalive_count % 4 == 0:
                        print(f"[SSE KEEPALIVE] Sent keepalive ping #{keepalive_count} for request {completion_id} (style: {keepalive_style})", flush=True)
                    yield make_sse_keepalive_chunk(completion_id, request.model, created_ts, keepalive_style)
                    continue

                if item_type == "end":
                    break
                elif item_type == "error":
                    print(f"[SSE STREAM ERROR] Exception during stream for {completion_id}: {item_data}", flush=True)
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
                                    trace_str = format_process_tool_start(tool_calls)
                                    for chk in emit_process_trace(trace_str):
                                        yield chk
                                
                                content = getattr(msg, "content", "")
                                if content and isinstance(content, str) and content.strip():
                                    yield make_chat_chunk(completion_id, request.model, created_ts, {"content": content})
                                    yielded_any_content = True
                            elif isinstance(msg, ToolMessage):
                                trace_str = format_process_tool_result(msg, max_tool_chars)
                                for chk in emit_process_trace(trace_str):
                                    yield chk
                                content = getattr(msg, "content", "")
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

        yield make_chat_chunk(completion_id, request.model, created_ts, {}, finish_reason="stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )




def lazy_init_app(model_name: str = "gpt-4o"):
    """Thread-safe and exception-safe lazy initialization for stateful and stateless LangGraph applications."""
    global langgraph_app, langgraph_app_stateless, toolkit
    if langgraph_app and langgraph_app_stateless:
        return
        
    project_path = os.environ.get("PROJECT_PATH")
    if not project_path:
        raise HTTPException(
            status_code=500,
            detail="WrenToolkit not initialized. Please set PROJECT_PATH environment variable."
        )
    try:
        if not toolkit:
            toolkit = WrenToolkit.from_project(project_path)
            
        if not langgraph_app:
            # Check if DB URI is set to use MySQL checkpointer even during lazy load
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
                            fut.result(timeout=10.0) # 10s timeout alignment with lifespan
                            checkpointer = cand
                            print("Lazy-initialized persistent MySQL checkpointer!")
                        except (concurrent.futures.TimeoutError, Exception) as setup_err:
                            print(f"Warning during lazy MySQL setup: {setup_err}. Cleaning up connection leak & falling back to MemorySaver.")
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
        raise HTTPException(status_code=500, detail=f"Failed to lazy initialize app: {e}")


@app.post("/chat")
async def chat_endpoint(request: ChatRequestMulti):
    """Standard non-streaming stateful chat endpoint. Persistent history is loaded and updated."""
    lazy_init_app(model_name=request.model_name)

    # Generate session ID if not provided
    actual_session_id = request.session_id or str(uuid.uuid4())
    config = {"configurable": {"thread_id": actual_session_id}}
    
    try:
        # LangGraph checkpointer automatically merges this HumanMessage with previous thread checkpoints
        final_state = await langgraph_app.ainvoke(
            {"messages": [HumanMessage(content=request.question)]}, 
            config=config
        )
        
        # Return full history of current session along with the session_id
        response_messages = [serialize_message(msg) for msg in final_state["messages"]]
        return {
            "session_id": actual_session_id,
            "messages": response_messages,
            "tool_call_summary": {
                msg["name"] if "name" in msg else msg["type"]: 1 
                for msg in response_messages if msg["role"] == "tool"
            }
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Execution error: {str(e) or type(e).__name__}")


@app.post("/chat/stream")
async def chat_stream_endpoint(request: ChatRequestMulti):
    """Streaming stateful chat endpoint. Persistent history is loaded and updated."""
    lazy_init_app(model_name=request.model_name)

    # Generate session ID if not provided
    actual_session_id = request.session_id or str(uuid.uuid4())
    stream_queue = asyncio.Queue()
    config = {
        "configurable": {
            "thread_id": actual_session_id,
            "stream_queue": stream_queue
        }
    }

    async def run_graph():
        try:
            # Yield events node by node, persisting history into the thread
            async for event in langgraph_app.astream(
                {"messages": [HumanMessage(content=request.question)]}, 
                config=config, 
                stream_mode="updates"
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
                    formatted_event = {
                        "session_id": actual_session_id
                    }
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
    lazy_init_app()
    
    config = {"configurable": {"thread_id": session_id}}
    try:
        # Retrieve state from checkpointer
        state = await langgraph_app.aget_state(config)
        messages = state.values.get("messages", []) if state.values else []
        return {
            "session_id": session_id,
            "messages": [serialize_message(msg) for msg in messages]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch session history: {e}")


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8201"))
    print(f"Starting Stateful FastAPI server on http://0.0.0.0:{port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)

