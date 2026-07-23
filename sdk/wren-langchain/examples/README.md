# Wren LangGraph Stateful API Server & Dual-Track Integration

This directory contains production-ready code demonstrating how to wrap a Wren AI semantic layer project inside a **stateful, multi-turn LangGraph Agent API Server** (with OpenAI compatibility) and a **FastMCP SSE Server** for seamless integration with AI platforms like **DEEIX-Chat**.

---

## Architecture Overview (Dual-Track Mode)

```
                            ┌────────────────────────────────────────┐
                            │           DEEIX-Chat Platform          │
                            │  (Web UI / Billing / Auth / Routing)   │
                            └──────────────────┬─────────────────────┘
                                               │
                      ┌────────────────────────┴────────────────────────┐
                      ▼                                                 ▼
        【Track A: MCP Plugin Protocol】                    【Track B: OpenAI API Adapter Protocol】
       DEEIX-Chat / Client as MCP Client                  DEEIX-Chat / Client as Upstream Client
  (Any LLM calls Wren Semantic Tools over MCP)           (Select "wren-agent" from model list)
                      │                                                 │
                      ▼ (Port 8202 - FastMCP SSE)                       ▼ (Port 8201 - OpenAI /v1 Protocol)
        ┌────────────────────────┐                        ┌────────────────────────┐
        │   wren_mcp_server.py   │                        │langgraph_fastapi_multi │
        │  (FastMCP SSE Server)  │                        │ (/v1/chat/completions) │
        └───────────┬────────────┘                        └───────────┬────────────┘
                    │                                                 │
                    └────────────────────────┬────────────────────────┘
                                             ▼
                            ┌────────────────────────────────────────┐
                            │         WrenAI Semantic Layer          │
                            │    (WrenToolkit / LangGraph Agent)     │
                            └────────────────────────────────────────┘
```

---

## Features

1. **Track A: FastMCP SSE Server (Port 8202)**:
   - Exposes Wren AI semantic layer abilities (`wren_semantic_query`, `wren_get_system_prompt`, `wren_list_tools`) over Model Context Protocol (MCP) using SSE transport.
   - Allows platform LLMs (GPT-4o, Claude 3.5, DeepSeek, etc.) to autonomously trigger NL2SQL and semantic data queries.

2. **Track B: OpenAI API Compatible Adapter (Port 8201)**:
   - Provides `/v1/models` and `/v1/chat/completions` endpoints supporting both non-streaming JSON responses and SSE streaming (`stream=true`).
   - Automatically translates LangGraph node execution steps into standard OpenAI SSE delta chunks (`chat.completion.chunk`).

3. **Stateful Session Chat**:
   - Preserves multi-turn conversation history using LangGraph checkpointers (`MemorySaver` or MySQL database checkpointer via `CHAT_HISTORY_DB_URI`).

4. **MCP Client Extension**:
   - Dynamically loads external Model Context Protocol (MCP) servers (stdio / streamable_http / SSE) configured via `MCP_CONFIG_DIR`.

5. **Container & Dual Service Management**:
   - Out-of-the-box `docker-compose.yaml`, `Dockerfile`, and `start_dual_services.py` for launching both Track A and Track B concurrently.

---

## Environment Variables

Configure the services via environment variables:

| Variable | Description | Default / Example |
|---|---|---|
| `PROJECT_PATH` | Path to the prepared Wren project directory (holding `.wren`). | `/project` or `./wren_project` |
| `PORT` | Port for Track B FastAPI / OpenAI Proxy server. | `8201` |
| `MCP_HOST` | Host binding for Track A FastMCP SSE server. | `0.0.0.0` |
| `MCP_PORT` | Port for Track A FastMCP SSE server. | `8202` |
| `OPENAI_API_KEY` | API Key for upstream LLM used by LangGraph. | `sk-proj-...` |
| `LLM_API_BASE` | Custom base URL for LLM service (e.g., vLLM, Ollama, OneAPI). | `http://localhost:8000/v1` |
| `LLM_MODEL_NAME` | Model name override. | `gpt-4o` |
| `CHAT_HISTORY_DB_URI` | Optional MySQL connection string for thread checkpointer. | `mysql+pymysql://user:pass@localhost:3306/db` |
| `MCP_CONFIG_DIR` | Optional directory containing external MCP JSON configs. | `./mcp_configs` |

---

## Running the Dual-Track Services

### Option 1: Docker Compose (Recommended for Production)

Standard build & launch:
```bash
docker-compose up -d --build
```

**China Mainland Network Accelerated Build (中国大陆网络极速构建)**:
```bash
docker-compose -f docker-compose.cn.yaml build --no-cache
docker-compose -f docker-compose.cn.yaml up -d
```
*(Uses Tsinghua APT mirror, npmmirror registry, PyPI Tsinghua mirror, and GitHub proxy for ultra-fast build in Mainland China)*

- **Track B (OpenAI Proxy & API)**: `http://localhost:8201/v1`
- **Track A (FastMCP SSE Server)**: `http://localhost:8202/sse`

### Option 2: Windows Batch Script (Local Development)

```cmd
start_server.bat
```

### Option 3: Python Launcher

```bash
python start_dual_services.py
```

---

## API Endpoints & Usage

### Track A: FastMCP SSE Endpoint (`http://localhost:8202/sse`)

Test SSE connection:
```bash
curl -N http://localhost:8202/sse
```

Available Tools in FastMCP:
- `wren_semantic_query(question: str)`: Executes natural language query against semantic layer.
- `wren_get_system_prompt()`: Returns system prompt instructions & schema definition.
- `wren_list_tools()`: Lists sub-tools and internal schemas.

---

### Track B: OpenAI API Adapter (`http://localhost:8201/v1`)

#### 1. Models Discovery (`GET /v1/models`)
```bash
curl http://localhost:8201/v1/models
```

#### 2. Chat Completions - Non-Streaming (`POST /v1/chat/completions`)
```bash
curl -X POST http://localhost:8201/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "wren-agent",
    "messages": [
      {"role": "user", "content": "How many records are in our database?"}
    ],
    "stream": false
  }'
```

#### 3. Chat Completions - Streaming SSE (`POST /v1/chat/completions`)
```bash
curl -X POST http://localhost:8201/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "wren-agent",
    "messages": [
      {"role": "user", "content": "Summarize total customer orders for last month"}
    ],
    "stream": true
  }'
```

---

### Legacy Endpoints

- **Swagger UI**: `http://localhost:8201/docs`
- **Native Chat API**: `POST /chat`
- **Native Stream API**: `POST /chat/stream`
- **History Retrieval**: `GET /chat/history/{session_id}`
