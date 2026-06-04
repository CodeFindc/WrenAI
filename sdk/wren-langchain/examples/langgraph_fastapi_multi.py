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
from typing import Annotated, TypedDict, Literal
from contextlib import asynccontextmanager

try:
    from fastapi import FastAPI, HTTPException, Response
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import StreamingResponse, HTMLResponse
    from fastapi.openapi.docs import get_swagger_ui_html
    from pydantic import BaseModel, Field
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
from langchain_core.tools import tool
import datetime
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from wren_langchain import WrenToolkit


# ── LangGraph Setup (adapted from langgraph_demo.py with Adaptive Checkpointer) ───

class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


@tool("get_current_time")
def get_current_time() -> str:
    """Get the current system date and time of the host machine.
    
    Call this tool when you need to write reports, resolve date ranges,
    or know the current date and time for statistical purposes.
    """
    now = datetime.datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S")


def build_app(toolkit: WrenToolkit, checkpointer, model_name: str = "gpt-4o"):
    """Compile a ReAct graph that uses Wren tools and binds the provided checkpointer."""
    tools = toolkit.get_tools()
    tools.append(get_current_time)
    system_prompt = toolkit.system_prompt()
    
    # Allow custom API base and key via environment variables for compatibility
    api_base = os.getenv("LLM_API_BASE") or os.getenv("OPENAI_API_BASE")
    api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    model_to_use = os.getenv("LLM_MODEL_NAME", model_name)

    print(f"--- Configured LLM: {model_to_use} | API Base: {api_base} ---")

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

    def agent_node(state: AgentState) -> dict:
        messages = state["messages"]
        if not messages or not isinstance(messages[0], SystemMessage):
            messages = [SystemMessage(content=system_prompt), *messages]
        
        print(f"\n[LLM Request] Model to call: {model_to_use}")
        response = model_with_tools.invoke(messages)
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
    """Lifecycle context manager to initialize the Wren Toolkit at startup."""
    global toolkit, langgraph_app
    project_path = os.environ.get("PROJECT_PATH")
    db_uri = os.environ.get("CHAT_HISTORY_DB_URI")

    if not os.environ.get("OPENAI_API_KEY"):
        print("WARNING: OPENAI_API_KEY is not set. Custom endpoint configuration or key will be required.")

    if project_path:
        try:
            print(f"Initializing WrenToolkit from project: {project_path}")
            toolkit = WrenToolkit.from_project(project_path)
        except Exception as e:
            print(f"Error initializing WrenToolkit: {e}")
            sys.exit(1)

    # ── Lifespan-level Adaptive Checkpointer Lifecycle ────────────────────
    if db_uri and toolkit:
        print("CHAT_HISTORY_DB_URI detected. Attempting to initialize MySQL checkpointer...")
        try:
            # Lazy import so standard run doesn't crash if packages are missing
            from langgraph.checkpoint.mysql.pymysql import PyMySQLSaver
            
            # Autocommit=True is mandatory for .setup() to succeed in MySQL
            if "autocommit" not in db_uri.lower():
                separator = "&" if "?" in db_uri else "?"
                db_uri = f"{db_uri}{separator}autocommit=true"
            
            # Enter the context manager at startup so the pool stays alive during app yield
            with PyMySQLSaver.from_conn_string(db_uri) as checkpointer:
                checkpointer.setup()
                print("Successfully initialized persistent MySQL checkpointer!")
                langgraph_app = build_app(toolkit, checkpointer)
                yield  # Let the FastAPI server run
        except ImportError:
            print("\nWARNING: 'langgraph-checkpoint-mysql' or 'pymysql' is not installed.")
            print("To enable MySQL persistence, please run: pip install langgraph-checkpoint-mysql[pymysql]")
            print("Falling back to in-memory MemorySaver...")
            from langgraph.checkpoint.memory import MemorySaver
            checkpointer = MemorySaver()
            langgraph_app = build_app(toolkit, checkpointer)
            yield
        except Exception as e:
            print(f"\nERROR initializing MySQL checkpointer: {e}")
            print("Falling back to in-memory MemorySaver...")
            from langgraph.checkpoint.memory import MemorySaver
            checkpointer = MemorySaver()
            langgraph_app = build_app(toolkit, checkpointer)
            yield
    else:
        if toolkit:
            print("CHAT_HISTORY_DB_URI not set. Using in-memory MemorySaver (data will clear on server reload).")
            from langgraph.checkpoint.memory import MemorySaver
            checkpointer = MemorySaver()
            langgraph_app = build_app(toolkit, checkpointer)
        yield


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
async def serve_chat_ui():
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
async def custom_swagger_ui_html():
    """Serve Swagger UI HTML overriding the CDN URLs with local paths."""
    return get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title=app.title + " - Docs",
        swagger_js_url="/static/swagger-ui-bundle.js",
        swagger_css_url="/static/swagger-ui.css",
        swagger_favicon_url="/static/favicon.png",
    )


@app.get("/static/swagger-ui-bundle.js", include_in_schema=False)
async def get_swagger_js():
    return get_local_or_cdn(
        "swagger-ui-bundle.js",
        "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js",
        "application/javascript"
    )


@app.get("/static/swagger-ui.css", include_in_schema=False)
async def get_swagger_css():
    return get_local_or_cdn(
        "swagger-ui.css",
        "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css",
        "text/css"
    )


@app.get("/static/favicon.png", include_in_schema=False)
async def get_swagger_favicon():
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
async def health_check():
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
        final_state = langgraph_app.invoke(
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
            for event in langgraph_app.stream(
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
        state = langgraph_app.get_state(config)
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

