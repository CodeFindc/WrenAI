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

---

## Environment Variables Reference

The Dual-Track services (FastMCP SSE and OpenAI-compatible API) can be fully customized via environment variables:

### 1. Core Services & Network Binding
| Variable | Description | Default / Example |
|---|---|---|
| `PROJECT_PATH` | Path to the prepared Wren AI semantic project directory (containing `.wren`). | `/project` or `./wren_project` |
| `PORT` | Listening port for Track B FastAPI / OpenAI Proxy server (`/v1/*` & `/chat*`). | `8201` |
| `MCP_HOST` | Host binding for Track A FastMCP SSE Server. | `0.0.0.0` |
| `MCP_PORT` | Listening port for Track A FastMCP SSE Server. | `8202` |

### 2. Upstream LLM Configuration
| Variable | Description | Default / Example |
|---|---|---|
| `OPENAI_API_KEY` | API Key for upstream LLM used by LangGraph agent logic. | `sk-proj-...` |
| `LLM_API_BASE` / `OPENAI_API_BASE` | Base URL for LLM service (supports vLLM, Ollama, OneAPI, DeepSeek, etc.). | `http://192.168.110.209:8200/v1` |
| `LLM_MODEL_NAME` | Model name passed to the upstream LLM provider. | `gpt-4o` or `Qwen3.6-27B` |

### 3. Track B OpenAI Adapter & Thinking Stream
| Variable | Description | Default / Example |
|---|---|---|
| `OPENAI_EXPOSED_MODELS` | Virtual model IDs returned by `GET /v1/models` (comma-separated). | `wren-agent,wren-semantic-analyst` |
| `OPENAI_PROCESS_STREAM_MODE` | ReAct reasoning & tool execution trace mode: `reasoning` (default, emits `delta.reasoning_content` for UI thinking trace), `text` (inline), `both`, or `off`. | `reasoning` |
| `OPENAI_PROCESS_MAX_TOOL_CHARS` | Character truncation limit for tool execution summary in thinking trace. | `400` |

### 4. SSE Stream Keepalive (Prevent Client Idle Timeout)
| Variable | Description | Default / Example |
|---|---|---|
| `OPENAI_SSE_KEEPALIVE_SECONDS` | Heartbeat interval in seconds during long tool/LLM inference windows (`<=0` to disable). | `15` |
| `OPENAI_SSE_KEEPALIVE_STYLE` | Heartbeat byte format: `comment` (`: keepalive\n\n`, zero UI noise) or `empty_delta` (`delta: {}`). | `comment` |

### 5. Diagnostics & Persistence
| Variable | Description | Default / Example |
|---|---|---|
| `LOG_LEVEL` | Application logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`). | `INFO` |
| `CHAT_HISTORY_DB_URI` | Connection URI for persistent MySQL checkpointer (`mysql+pymysql://...`). Defaults to `MemorySaver`. | `mysql+pymysql://root:pass@host:3306/db` |
| `MCP_CONFIG_DIR` | Directory containing JSON definition files for external MCP servers. | `./mcp_configs` |

### 6. Automatic Data Source Profile Setup (`entrypoint.sh`)
| Variable | Description | Default / Example |
|---|---|---|
| `ACTIVE_PROFILE` | Name of the active profile generated in `.wren/profiles.yml`. | `default` |
| `DATASOURCE` | Data source type (`mysql`, `postgres`, `duckdb`, `bigquery`, etc.). | `mysql` |
| `DB_HOST` / `DB_PORT` | Target database hostname and port. | `127.0.0.1:3306` |
| `DB_NAME` / `DB_USER` / `DB_PASSWORD` | Database credentials and target database name. | `root` / `secret` |
| `SSL_MODE` | Database SSL connection mode (`DISABLED`, `REQUIRED`, etc.). | `DISABLED` |

---

## Structured Logging & Request Traceability

All application logs follow a structured format tagged with `[req=<request_id>]` for end-to-end request tracing:

```text
2026-07-24 15:40:01 INFO [wren.sse] [req=chatcmpl-a0e2] stream_start route=/v1/chat/completions model=wren-agent process_mode=reasoning keepalive=15.0s style=comment
2026-07-24 15:40:02 INFO [wren.tool] [req=chatcmpl-a0e2] tool_start name=wren_semantic_query args={"question":"Summary of sales..."}
2026-07-24 15:40:03 INFO [wren.tool] [req=chatcmpl-a0e2] tool_end name=wren_semantic_query result_chars=1200
2026-07-24 15:40:04 INFO [wren.llm] [req=chatcmpl-a0e2] invoke_start model=gpt-4o base=default messages=6
2026-07-24 15:40:18 INFO [wren.llm] [req=chatcmpl-a0e2] invoke_end duration_ms=14012 has_tool_calls=false content_chars=256
2026-07-24 15:40:18 INFO [wren.sse] [req=chatcmpl-a0e2] stream_end outcome=ok duration_ms=17050 keepalive_count=1 yielded_content=true
```

### Useful Log Inspection Commands

```bash
# 1. Trace a specific request end-to-end
docker logs wren-langgraph-api 2>&1 | grep 'req=chatcmpl-a0e2'

# 2. Monitor stream lifecycle and outcomes (ok/error)
docker logs wren-langgraph-api 2>&1 | grep 'stream_end'

# 3. Inspect errors and exceptions with stack traces
docker logs wren-langgraph-api 2>&1 | grep ' ERROR '
```

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

---

## Troubleshooting Reverse Proxies & Streaming Interruption

If UI clients (such as DEEIX-Chat) report streaming interruptions during long-running tool queries:

1. **Proxy Buffering**: Ensure reverse proxies (Nginx / Ingress / Traefik) do not buffer SSE streams (`X-Accel-Buffering: no` is set by default in responses).
2. **Comment Stripping**: If proxies strip SSE comment lines (`: keepalive\n\n`), switch to empty delta chunks by setting:
   ```bash
   OPENAI_SSE_KEEPALIVE_STYLE=empty_delta
   ```
3. **Verify Keepalive with cURL**:
   ```bash
   curl -N -X POST http://localhost:8201/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{"model":"wren-agent","messages":[{"role":"user","content":"Run complex query"}],"stream":true}'
   ```
   Look for periodic `: keepalive` ping comments every 15 seconds during long inference windows.
- **History Retrieval**: `GET /chat/history/{session_id}`
