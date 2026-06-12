# Wren LangGraph Stateful API Server & Examples

This directory contains production-ready examples demonstrating how to wrap a Wren AI semantic layer project inside a **stateful, multi-turn LangGraph Agent API Server** using FastAPI.

---

## Features

1. **Stateful Session Chat**: Preserves conversation history between turns using LangGraph checkpointers.
2. **Streaming Updates**: Standard SSE/NDJSON stream endpoint `/chat/stream` for real-time node-by-node updates.
3. **Adaptive Checkpointer**: Falls back to in-memory `MemorySaver` but automatically upgrades to persistent `MySQL` database storage if `CHAT_HISTORY_DB_URI` is provided.
4. **Offline-Ready Swagger UI**: Caches Swagger JS/CSS assets locally for air-gapped development.
5. **MCP Tool Extensions**: Dynamically loads external Model Context Protocol (MCP) servers (both local subprocesses via `stdio` and remote services via `sse`) from a directory specified by `MCP_CONFIG_DIR`.

---

## Installation & Setup

Install the required packages:

```bash
pip install -r requirements.txt
```

---

## Configuration (Environment Variables)

The server is configured via environment variables:

| Variable | Description | Example |
|---|---|---|
| `PROJECT_PATH` | Path to the prepared Wren project directory (holding `.wren`). | `/path/to/my-wren-project` |
| `OPENAI_API_KEY` | API Key for LLM. | `sk-proj-...` |
| `LLM_API_BASE` | Optional custom base URL (e.g. for vLLM, Ollama, OneAPI). | `https://api.openai.com/v1` |
| `LLM_MODEL_NAME` | Model to override default `gpt-4o`. | `gpt-4o-mini` |
| `CHAT_HISTORY_DB_URI` | Optional MySQL connection string for persistent thread saving. | `mysql+pymysql://user:pass@localhost:3306/db` |
| `MCP_CONFIG_DIR` | Optional directory to load `.json` MCP server configurations from. | `./mcp_configs` |
| `PORT` | Port number to run the FastAPI server on. | `8201` |

---

## Model Context Protocol (MCP) Tool Integration

You can extend the agent's capabilities beyond Wren query/memory tools by attaching external MCP servers. The server automatically scans `MCP_CONFIG_DIR` for `.json` files containing server setups (compatible with Claude Desktop configuration format).

### Configuration Formats

Create a JSON file (e.g., `mcp_config.json`) inside your config directory.

#### 1. Local Stdio Servers (Subprocess)
Runs a command locally inside a subprocess to interface with tools (e.g., SQLite, Filesystem, Git):
```json
{
  "mcpServers": {
    "git": {
      "command": "uvx",
      "args": ["mcp-server-git", "--repository", "/path/to/my/repo"]
    },
    "fetch": {
      "command": "uv",
      "args": ["run", "mcp-server-fetch"]
    }
  }
}
```

#### 2. Remote SSE Servers (Network-based)
Connects to a running remote MCP server over the network:
```json
{
  "mcpServers": {
    "my-remote-agent": {
      "url": "http://localhost:8080/mcp/sse"
    }
  }
}
```

### How it Works
1. At startup, the server uses `contextlib.AsyncExitStack` to establish connections to all declared MCP channels.
2. It fetches available tool lists from each server.
3. It maps each tool's JSON Schema to a Pydantic Model to feed the LangChain compiler.
4. When the agent decides to invoke an MCP tool, the server runs it on the respective session thread.
5. All connections are cleanly closed when the FastAPI server shuts down.

---

## Running the Server

1. Define your environment variables (e.g., via a `.env` file or terminal):
   ```bash
   export PROJECT_PATH="/absolute/path/to/wren-project"
   export OPENAI_API_KEY="sk-..."
   export MCP_CONFIG_DIR="./mcp_configs"
   ```
2. Run the script:
   ```bash
   python langgraph_fastapi_multi.py
   ```
   Or using Uvicorn:
   ```bash
   uvicorn langgraph_fastapi_multi:app --host 0.0.0.0 --port 8201
   ```

---

## API Endpoints & Usage

Once started, visit **`http://localhost:8201/docs`** to inspect the API via Swagger UI.

### 1. Chat Endpoint (`POST /chat`)
Ask a question under a specific session:
```bash
curl -X POST http://localhost:8201/chat \
  -H "Content-Type: application/json" \
  -d '{"question": "How many records are in our customers table?", "session_id": "session-123"}'
```

### 2. Streaming Chat Endpoint (`POST /chat/stream`)
Receive NDJSON stream events as the LangGraph agent executes nodes (Agent reasoning -> Tool executing -> Agent deciding):
```bash
curl -X POST http://localhost:8201/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"question": "Summarize user orders last month using our database", "session_id": "session-123"}'
```

### 3. Session History (`GET /chat/history/{session_id}`)
Retrieve the full transcript of a thread:
```bash
curl http://localhost:8201/chat/history/session-123
```
