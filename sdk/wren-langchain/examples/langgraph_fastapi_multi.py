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


async def get_or_create_mcp_session(server_name: str, force_reconnect: bool = False) -> Any:
    """Get or establish an active session with the specified MCP server, recreating it if forced."""
    lock = mcp_locks.setdefault(server_name, asyncio.Lock())
    async with lock:
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
    async def _acall(**kwargs) -> str:
        try:
            session = await get_or_create_mcp_session(server_name)
        except Exception as e:
            print(f"[MCP CLIENT] Failed to connect to MCP server '{server_name}': {e}")
            return f"Error: Failed to connect to MCP server '{server_name}': {e}"
        try:
            print(f"[MCP CLIENT] Calling tool '{tool_name}' on server '{server_name}' with args: {kwargs}...")
            result = await session.call_tool(tool_name, kwargs)
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
                result = await session.call_tool(tool_name, kwargs)
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
    def _call(**kwargs) -> str:
        import anyio
        import functools
        try:
            return anyio.from_thread.run(functools.partial(_acall, **kwargs))
        except RuntimeError:
            # Fallback if no anyio event loop running in the current thread context
            return asyncio.run(_acall(**kwargs))
            
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


def build_app(toolkit: WrenToolkit, checkpointer, model_name: str = "gpt-4o"):
    """Compile a ReAct graph that uses Wren tools and binds the provided checkpointer."""
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
        if not messages or not isinstance(messages[0], SystemMessage):
            messages = [SystemMessage(content=system_prompt), *messages]
        
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
    
    return graph.compile(checkpointer=checkpointer)


# ── Global state and Lifespan ──────────────────────────────────────────

# Global toolkit and compiled langgraph app instances
toolkit: WrenToolkit | None = None
langgraph_app = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle context manager to initialize the Wren Toolkit, MCP sessions, and checkpointer at startup."""
    global toolkit, langgraph_app, mcp_sessions, global_mcp_tools
    
    # 1. Initialize Wren Toolkit
    project_path = os.environ.get("PROJECT_PATH")
    if project_path:
        project_path = project_path.strip().strip('"').strip("'")
        try:
            print(f"Initializing WrenToolkit from project: {project_path}")
            toolkit = WrenToolkit.from_project(project_path)
        except Exception as e:
            print(f"Error initializing WrenToolkit: {e}")
            sys.exit(1)

    if not os.environ.get("OPENAI_API_KEY"):
        print("WARNING: OPENAI_API_KEY is not set. Custom endpoint configuration or key will be required.")

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

                        await asyncio.wait_for(_connect_server(), timeout=15.0)
                    except asyncio.TimeoutError:
                        print(f"Failed to connect to MCP server '{server_name}': Connection or initialization timed out after 15.0 seconds.")
                    except Exception as e:
                        print(f"Failed to connect to MCP server '{server_name}': {e}")
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
    mysql_exit_stack = AsyncExitStack()
    
    if db_uri and toolkit:
        print("CHAT_HISTORY_DB_URI detected. Attempting to initialize MySQL checkpointer...")
        try:
            from langgraph.checkpoint.mysql.pymysql import PyMySQLSaver
            from contextlib import contextmanager

            class ReconnectingPyMySQLSaver(PyMySQLSaver):
                def __init__(self, *args, conn_args: dict = None, **kwargs):
                    super().__init__(*args, **kwargs)
                    self.conn_args = conn_args

                def _ping(self):
                    try:
                        if self.conn:
                            self.conn.ping(reconnect=True)
                    except Exception as e:
                        print(f"Failed to ping/reconnect MySQL database: {e}. Attempting clean reconnection...")
                        try:
                            try:
                                self.conn.close()
                            except Exception:
                                pass
                            import pymysql
                            if self.conn_args:
                                self.conn_args.setdefault("autocommit", True)
                                self.conn = pymysql.connect(**self.conn_args)
                            else:
                                self.conn.connect()
                            print("Successfully re-established clean MySQL connection!")
                        except Exception as conn_err:
                            print(f"Failed to force clean MySQL connection: {conn_err}")
                            raise conn_err

                def setup(self, *args, **kwargs):
                    self._ping()
                    return super().setup(*args, **kwargs)

                def get_tuple(self, *args, **kwargs):
                    self._ping()
                    return super().get_tuple(*args, **kwargs)

                def list(self, *args, **kwargs):
                    self._ping()
                    return super().list(*args, **kwargs)

                def put(self, *args, **kwargs):
                    self._ping()
                    return super().put(*args, **kwargs)

                def put_writes(self, *args, **kwargs):
                    self._ping()
                    return super().put_writes(*args, **kwargs)

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
                    
                    conn_args = {
                        "host": parsed.hostname or "localhost",
                        "port": parsed.port or 3306,
                        "user": user,
                        "password": password or "",
                        "database": database,
                    }
                    
                    if parsed.query:
                        params = urllib.parse.parse_qs(parsed.query)
                        for k, v in params.items():
                            val = v[0]
                            if val.lower() == "true":
                                val = True
                            elif val.lower() == "false":
                                val = False
                            elif val.isdigit():
                                val = int(val)
                            conn_args[k] = val
                            
                    # Force ssl_disabled=True by default to prevent any SSL/TLS upgrades
                    conn_args.setdefault("ssl_disabled", True)
                    conn_args.setdefault("autocommit", True)
                    
                    with pymysql.connect(**conn_args) as conn:
                        saver = cls(conn=conn, serde=None, conn_args=conn_args)
                        yield saver

            if "autocommit" not in db_uri.lower():
                separator = "&" if "?" in db_uri else "?"
                db_uri = f"{db_uri}{separator}autocommit=true"

            checkpointer = mysql_exit_stack.enter_context(ReconnectingPyMySQLSaver.from_conn_string(db_uri))
            checkpointer.setup()
            print("Successfully initialized persistent MySQL checkpointer with auto-reconnection!")
        except ImportError:
            print("\nWARNING: 'langgraph-checkpoint-mysql' or 'pymysql' is not installed.")
            print("Falling back to in-memory MemorySaver...")
        except Exception as e:
            print(f"\nERROR initializing MySQL checkpointer: {e}")
            print("Falling back to in-memory MemorySaver...")

    if not checkpointer:
        if toolkit:
            print("Using in-memory MemorySaver (data will clear on server reload).")
            from langgraph.checkpoint.memory import MemorySaver
            checkpointer = MemorySaver()

    # Build LangGraph app
    if toolkit and checkpointer:
        langgraph_app = build_app(toolkit, checkpointer)

    try:
        yield
    finally:
        print("Cleaning up lifespan resources...")
        mysql_exit_stack.close()
        for server_name, stack in list(mcp_exit_stacks.items()):
            try:
                print(f"Closing exit stack for MCP server '{server_name}'...")
                await stack.aclose()
            except Exception as e:
                print(f"Error closing exit stack for '{server_name}': {e}")
        mcp_exit_stacks.clear()
        mcp_sessions.clear()
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
    """Serve the compiled single-file offline chat UI at the root path."""
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
            return HTMLResponse(content=f"<h3>Error reading built HTML: {e}</h3>", status_code=500)
    return HTMLResponse(
        content=(
            "<h3>Wren Chat UI is not compiled yet.</h3>"
            "<p>Please build the UI first by running: <code>cmd /c npm run build</code> "
            "inside the <code>examples/wren-chat-ui/</code> directory.</p>"
        ),
        status_code=404
    )


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
    """Simple health endpoint."""
    return {
        "status": "healthy",
        "project_loaded": toolkit is not None,
        "memory_enabled": toolkit._memory.enabled if toolkit else False
    }


@app.post("/chat")
async def chat_endpoint(request: ChatRequestMulti):
    """Standard non-streaming stateful chat endpoint. Persistent history is loaded and updated."""
    global langgraph_app, toolkit
    if not langgraph_app:
        project_path = os.environ.get("PROJECT_PATH")
        if not project_path:
            raise HTTPException(
                status_code=500,
                detail="WrenToolkit not initialized. Please set PROJECT_PATH environment variable."
            )
        try:
            toolkit = WrenToolkit.from_project(project_path)
            # Default lazy load fallback uses safe in-memory MemorySaver
            from langgraph.checkpoint.memory import MemorySaver
            checkpointer = MemorySaver()
            langgraph_app = build_app(toolkit, checkpointer, model_name=request.model_name)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to lazy initialize WrenToolkit: {e}")

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
        raise HTTPException(status_code=500, detail=f"Execution error: {e}")


@app.post("/chat/stream")
async def chat_stream_endpoint(request: ChatRequestMulti):
    """Streaming stateful chat endpoint. Persistent history is loaded and updated."""
    global langgraph_app, toolkit
    if not langgraph_app:
        project_path = os.environ.get("PROJECT_PATH")
        if not project_path:
            raise HTTPException(
                status_code=500,
                detail="WrenToolkit not initialized. Please set PROJECT_PATH environment variable."
            )
        try:
            toolkit = WrenToolkit.from_project(project_path)
            from langgraph.checkpoint.memory import MemorySaver
            checkpointer = MemorySaver()
            langgraph_app = build_app(toolkit, checkpointer, model_name=request.model_name)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to lazy initialize WrenToolkit: {e}")

    # Generate session ID if not provided
    actual_session_id = request.session_id or str(uuid.uuid4())
    config = {"configurable": {"thread_id": actual_session_id}}

    async def event_generator():
        try:
            # Yield events node by node, persisting history into the thread
            async for event in langgraph_app.astream(
                {"messages": [HumanMessage(content=request.question)]}, 
                config=config, 
                stream_mode="updates"
            ):
                formatted_event = {
                    "session_id": actual_session_id
                }
                for node_name, update in event.items():
                    msgs = update.get("messages", [])
                    serialized_msgs = [serialize_message(msg) for msg in msgs]
                    formatted_event[node_name] = {"messages": serialized_msgs}
                
                yield json.dumps(formatted_event, ensure_ascii=False) + "\n"
        except Exception as e:
            yield json.dumps({"error": str(e)}, ensure_ascii=False) + "\n"

    return StreamingResponse(event_generator(), media_type="application/x-ndjson")


@app.get("/chat/history/{session_id}")
async def get_session_history(session_id: str):
    """Retrieve full conversation history of a specific session ID from checkpointer."""
    global langgraph_app
    if not langgraph_app:
        raise HTTPException(status_code=500, detail="Server not initialized. Please run a chat query first.")
    
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

