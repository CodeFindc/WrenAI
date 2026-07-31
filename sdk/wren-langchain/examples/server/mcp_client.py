"""Dynamic MCP (Model Context Protocol) client manager and tool adapter."""

from __future__ import annotations

import os
import glob
import json
import asyncio
from typing import Any, Type
from contextlib import AsyncExitStack
import httpx
from pydantic import BaseModel, Field, create_model
from langchain_core.tools import StructuredTool

mcp_sessions: dict[str, Any] = {}
mcp_configs_registry: dict[str, dict] = {}
mcp_exit_stacks: dict[str, AsyncExitStack] = {}
mcp_locks: dict[str, asyncio.Lock] = {}
global_mcp_tools: list[StructuredTool] = []
initialized_mcp_servers: set[str] = set()
mcp_retry_task: asyncio.Task | None = None
mcp_manager_queue: asyncio.Queue = asyncio.Queue()
mcp_manager_task: asyncio.Task | None = None


async def _real_get_or_create_mcp_session(server_name: str, force_reconnect: bool = False) -> Any:
    """Get or establish an active session with the specified MCP server, recreating it if forced."""
    if not force_reconnect and server_name in mcp_sessions:
        return mcp_sessions[server_name]

    config = mcp_configs_registry.get(server_name)
    if not config:
        raise ValueError(f"No configuration found for MCP server '{server_name}'")

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

            connect_timeout = float(os.environ.get("MCP_CONNECT_TIMEOUT", "60.0"))
            read_timeout = float(os.environ.get("MCP_READ_TIMEOUT", "600.0"))

            headers = config.get("headers")
            if transport_type in ("streamable_http", "streamable-http", "http"):
                print(
                    f"Connecting to remote MCP server '{server_name}' via Streamable HTTP: {url} (timeout: connect={connect_timeout}s, read={read_timeout}s)"
                )
                client = await stack.enter_async_context(
                    httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(read_timeout, connect=connect_timeout))
                )
                res = await stack.enter_async_context(streamable_http_client(url, http_client=client))
                read_stream, write_stream = res[0], res[1]
            else:
                print(
                    f"Connecting to remote MCP server '{server_name}' via SSE: {url} (timeout: connect={connect_timeout}s, read={read_timeout}s)"
                )
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


async def mcp_manager_worker() -> None:
    """Persistent task executing all MCP connection/disconnection context operations on a single task context."""
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
        default = ... if prop_name in required else prop_info.get("default", None)

        fields[prop_name] = (prop_type, Field(default=default, description=desc))

    return create_model(name, **fields)


def load_mcp_configs(config_dir: str) -> dict[str, dict]:
    """Scan and merge MCP configurations from all .json files in the specified directory."""
    servers = {}
    if not os.path.isdir(config_dir):
        print(f"Warning: MCP_CONFIG_DIR '{config_dir}' is not a directory.")
        return servers

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

    args_schema = None
    if isinstance(input_schema, dict) and input_schema.get("properties"):
        try:
            class_name = f"MCP_{server_name}_{tool_name}_Args"
            args_schema = json_schema_to_pydantic(class_name, input_schema)
        except Exception as e:
            print(f"Warning: failed to generate Pydantic model for {server_name}.{tool_name}: {e}")

    async def _acall(*args: Any, **kwargs: Any) -> str:
        config = kwargs.pop("config", None)
        if not config and args:
            from langchain_core.runnables import RunnableConfig

            if isinstance(args[0], RunnableConfig):
                config = args[0]
                args = args[1:]

        stream_queue = None
        if config and "configurable" in config:
            stream_queue = config["configurable"].get("stream_queue")

        async def progress_callback(progress: float, total: float | None = None, message: str | None = None) -> None:
            print(f"[MCP PROGRESS] {server_name}.{tool_name}: {progress}/{total} - {message}")
            if stream_queue:
                progress_event = {
                    "session_id": config["configurable"].get("thread_id"),
                    "progress": {
                        "server_name": server_name,
                        "tool_name": tool_name,
                        "progress": int(progress),
                        "total": int(total) if total is not None else 10,
                        "message": message or f"Processing step {progress}",
                    },
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

    def _call(*args: Any, **kwargs: Any) -> str:
        import anyio
        import functools

        try:
            return anyio.from_thread.run(functools.partial(_acall, *args, **kwargs))
        except RuntimeError:
            return asyncio.run(_acall(*args, **kwargs))

    prefixed_name = f"{server_name}_{tool_name}"
    prefixed_name = prefixed_name.replace("-", "_").replace(" ", "_")

    return StructuredTool(
        name=prefixed_name,
        description=tool_desc or f"Invoke {tool_name} from MCP server {server_name}",
        func=_call,
        coroutine=_acall,
        args_schema=args_schema,
    )


async def retry_failed_mcp_connections_loop(on_reconnect_success_cb: Any = None) -> None:
    """Background task to periodically retry connections to uninitialized MCP servers."""
    retry_interval = float(os.environ.get("MCP_RETRY_INTERVAL", "30.0"))
    while True:
        try:
            await asyncio.sleep(retry_interval)
            failed_servers = set(mcp_configs_registry.keys()) - initialized_mcp_servers
            if not failed_servers:
                break

            print(f"[MCP RETRY] Retrying connection for failed MCP servers: {list(failed_servers)}")
            for server_name in failed_servers:
                try:

                    async def _try_connect():
                        session = await get_or_create_mcp_session(server_name, force_reconnect=True)
                        tools_result = await session.list_tools()
                        print(
                            f"[MCP RETRY] Successfully connected to '{server_name}'! Discovered {len(tools_result.tools)} tools: {[t.name for t in tools_result.tools]}"
                        )

                        new_tools = []
                        for mcp_tool in tools_result.tools:
                            lc_tool = convert_mcp_to_langchain(server_name, mcp_tool)
                            new_tools.append(lc_tool)

                        global_mcp_tools.extend(new_tools)
                        initialized_mcp_servers.add(server_name)

                        if on_reconnect_success_cb:
                            on_reconnect_success_cb(server_name)
                        print(f"[MCP RETRY] Registered tools for '{server_name}' and triggered cache clear.")

                    await asyncio.wait_for(_try_connect(), timeout=15.0)
                except Exception as e:
                    print(f"[MCP RETRY] Connection or initialization attempt to '{server_name}' failed: {e}")
        except asyncio.CancelledError:
            print("[MCP RETRY] Background retry loop cancelled.")
            break
        except Exception as loop_err:
            print(f"[MCP RETRY] Error in background loop: {loop_err}")
