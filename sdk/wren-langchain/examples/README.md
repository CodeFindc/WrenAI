[English] | [中文说明](README.cn.md)

# Wren LangGraph Stateful API Server & Dual-Track Integration

This directory contains a runnable reference implementation demonstrating how to wrap a Wren AI semantic layer project inside a **stateful, multi-turn LangGraph Agent API Server** (with OpenAI compatibility) and a **FastMCP dual-transport Server** (classic SSE + Streamable HTTP) for seamless integration with AI platforms like **DEEIX-Chat**.

---

## 1. Architecture Overview (Dual-Track Mode)

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
   (LLM ReAct over wren_query / dry_plan / …)             (Select "wren-agent" from model list)
                      │                                                 │
                      ▼ (Port 8202 - SSE /sse + Streamable HTTP /mcp)   ▼ (Port 8201 - OpenAI /v1 Protocol)
        ┌────────────────────────┐                        ┌────────────────────────┐
        │   wren_mcp_server.py   │                        │langgraph_fastapi_multi │
        │  (FastMCP dual xport)  │                        │ (/v1/chat/completions) │
        └───────────┬────────────┘                        └───────────┬────────────┘
                    │                                                 │
                    └────────────────────────┬────────────────────────┘
                                             ▼
                            ┌────────────────────────────────────────┐
                            │         WrenAI Semantic Layer          │
                            │  (WrenToolkit; Track B adds LangGraph) │
                            └────────────────────────────────────────┘
```

### Key Features

1. **Track A: FastMCP dual-transport Server (Port 8202)**:
   - Exposes real WrenToolkit tools (`wren_query`, `wren_dry_plan`, `wren_list_models`, memory tools, plus `wren_get_system_prompt`) over Model Context Protocol (MCP) on **both** classic SSE (`/sse`) and Streamable HTTP (`/mcp`, JSON-RPC used by DEEIX-Chat).
2. **Track B: OpenAI API Compatible Adapter (Port 8201)**:
   - Provides `/v1/models` and `/v1/chat/completions` endpoints supporting both non-streaming JSON responses and SSE streaming (`stream=true`).
   - Automatically translates LangGraph node execution steps into standard OpenAI SSE delta chunks (`chat.completion.chunk`).
3. **Stateful Session Chat & Session Preservation**:
   - Preserves multi-turn conversation history using LangGraph checkpointers (`MemorySaver` or MySQL checkpointer via `CHAT_HISTORY_DB_URI`).
   - Graceful HTTP 200 OK fallback on LLM/tool exceptions ensures client sessions are never lost or dropped.

---

## 2. Base Image Packaging Guide

To speed up image builds and pre-package C extension dependencies (`mysqlclient`, `pkg-config`), you can build the pre-packaged base image:

### 2.1 Build CN Accelerated Base Image (`wrenai-base:cn`)
Pre-configured with **Tsinghua/Tencent Cloud APT mirrors**, **npmmirror**, and **Tsinghua/Tencent PyPI mirrors**:

- **Windows**:
  ```cmd
  build_base.bat
  ```
- **Linux / macOS**:
  ```bash
  chmod +x build_base.sh && ./build_base.sh
  ```
- **Manual Docker Command**:
  ```bash
  docker build -t wrenai-base:cn -f Dockerfile.cn.base .
  ```

### 2.2 Build Standard Base Image (`wrenai-base:latest`)
```bash
docker build -t wrenai-base:latest -f Dockerfile.base .
```

---

## 3. Deployment & Startup Variants

### Variant 1: Fast Build Mode (CN Accelerated - 2-Second Incremental Rebuild)
Inherits `wrenai-base:cn` base image for ultra-fast incremental rebuilds:
```bash
docker-compose -f docker-compose.fast.yaml up -d --build
```

### Variant 2: Claude CLI Automated Datasource Onboarding Mode
Runs containerized Claude CLI with pre-loaded `.claude/skills/offline_wren_generate-mdl` to discover schema and generate MDL models **100% unattended**:

- **Helper Scripts**:
  - Windows: `init_datasource_claude.bat`
  - Linux / macOS: `./init_datasource_claude.sh`
- **Docker Compose**:
  ```bash
  docker-compose -f docker-compose.claude.yaml up -d --build
  ```

### Variant 3: Slim Image Mode
Optimized container size for memory-constrained host environments:
```bash
docker-compose -f docker-compose.slim.yaml up -d --build
```

### Variant 4: Standard Dual-Track Mode
```bash
docker-compose up -d --build
```

### Variant 5: Local Development
- **Windows Launcher**: `start_server.bat`
- **Python Launcher**: `python start_dual_services.py`

---

## 4. Wren System Prompt & Injection Points

### 4.1 Definition Source
Wren System Prompts are generated by `WrenToolkit` in `wren_langchain` (`toolkit.system_prompt()`).

### 4.2 Code Locations

| Track | Source File | Function / Node | Description |
|---|---|---|---|
| **Track B (FastAPI Agent)** | [`examples/server/agent_graph.py`](file:///D:/dev/WrenAI/sdk/wren-langchain/examples/server/agent_graph.py#L40-L60) | `ensure_wren_system_prompt()` | Merges system instructions into a single `SystemMessage` at index 0 prior to calling LLM, preventing vLLM / Qwen 400 errors. |
| **Track A (FastMCP Engine)** | [`examples/wren_mcp_server.py`](file:///D:/dev/WrenAI/sdk/wren-langchain/examples/wren_mcp_server.py#L180-L190) | `@mcp.tool` `wren_get_system_prompt()` | Exposes tool for MCP clients (DEEIX-Chat, Dify) to fetch Wren system prompt. |

---

## 5. Environment Variables Reference

See `.env.example` for a complete template.

### 5.1 Core & Network Binding
| Variable | Description | Default / Example |
|---|---|---|
| `PROJECT_PATH` | Path to Wren semantic project (`/project` or `./wren_project`). | `/project` |
| `PORT` | Track B FastAPI / OpenAI Proxy port. | `8201` |
| `MCP_HOST` / `MCP_PORT` | Track A FastMCP host and port. | `0.0.0.0` : `8202` |
| `MCP_SSE_PATH` | Classic MCP SSE endpoint path. | `/sse` |
| `MCP_STREAMABLE_HTTP_PATH` | Streamable HTTP JSON-RPC path (DEEIX `baseURL`). | `/mcp` |

### 5.2 Upstream LLM & Anthropic Routing
| Variable | Description | Default / Example |
|---|---|---|
| `OPENAI_API_KEY` / `LLM_API_KEY` | Upstream LLM API key. | `sk-proj-...` |
| `LLM_API_BASE` / `OPENAI_API_BASE` | Upstream LLM base URL. | `http://192.168.110.209:8200/v1` |
| `LLM_MODEL_NAME` | Target model deployed upstream. | `Qwen3.6-27B-AEON-Ultimate-Uncensored-BF16` |
| `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_BASE_URL` | Auth token & URL for Claude CLI. | `http://192.168.110.209:8200/v1` |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` / `CLAUDE_CODE_SUBAGENT_MODEL` | Model alias overrides for local LLM routing. | `${ANTHROPIC_MODEL}` |

---

## 6. Automated Tests

Run full test suite:
```bash
pytest examples/tests -v
```
