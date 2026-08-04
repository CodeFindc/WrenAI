# Wren LangGraph Stateful API Server & Dual-Track Integration

[English](#wren-langgraph-stateful-api-server--dual-track-integration) | [中文说明](#wren-langgraph-有状态-api-服务与双轨集成)

---

<a id="wren-langgraph-有状态-api-服务与双轨集成"></a>
# Wren LangGraph 有状态 API 服务与双轨集成 (中文说明)

本目录包含完整的参考可运行实现，展示如何将 Wren AI 语义层项目封装入 **有状态、多轮 LangGraph Agent API 服务**（兼容 OpenAI API 规范）与 **FastMCP 双传输协议服务**（标准 SSE + Streamable HTTP）中，实现与 **DEEIX-Chat**、Dify、Open-WebUI 等 AI 平台的无缝集成。

---

## 1. 架构总览 (双轨集成模式)

```
                            ┌────────────────────────────────────────┐
                            │           DEEIX-Chat 平台             │
                            │  (Web UI / 计费 / 鉴权 / 路由转发)      │
                            └──────────────────┬─────────────────────┘
                                               │
                      ┌────────────────────────┴────────────────────────┐
                      ▼                                                 ▼
        【轨道 A: MCP 插件协议】                             【轨道 B: OpenAI API 协议】
       客户端作为标准 MCP Client                           客户端作为上游 LLM Client
   (由 LLM 驱动 ReAct 循环调用工具)                     (在模型列表中选择 "wren-agent")
                      │                                                 │
                      ▼ (端口 8202 - SSE /sse 与 Streamable HTTP /mcp)  ▼ (端口 8201 - OpenAI /v1 协议)
        ┌────────────────────────┐                        ┌────────────────────────┐
        │   wren_mcp_server.py   │                        │langgraph_fastapi_multi │
        │  (FastMCP 双传输服务)  │                        │ (/v1/chat/completions) │
        └───────────┬────────────┘                        └───────────┬────────────┘
                    │                                                 │
                    └────────────────────────┬────────────────────────┘
                                             ▼
                            ┌────────────────────────────────────────┐
                            │         WrenAI 语义层核心             │
                            │  (WrenToolkit; Track B 结合 LangGraph) │
                            └────────────────────────────────────────┘
```

---

## 2. 基础镜像打包指南 (Building Base Images)

为提升构建速度并预装 C 扩展依赖（如 `mysqlclient` 与 `pkg-config`），本项目支持打包预调教的基础镜像：

### 2.1 打包中国大陆极速 Base 镜像 (`wrenai-base:cn`)
预封装了**腾讯云 / 清华大学 APT 源**、**npmmirror 镜像源**与**腾讯云 PyPI 镜像源**，支持 2 秒内极速构建：

- **Windows 环境**：
  ```cmd
  build_base.bat
  ```
- **Linux / macOS 环境**：
  ```bash
  chmod +x build_base.sh && ./build_base.sh
  ```
- **手动 Docker 命令**：
  ```bash
  docker build -t wrenai-base:cn -f Dockerfile.cn.base .
  ```

### 2.2 打包标准 Base 镜像 (`wrenai-base:latest`)
```bash
docker build -t wrenai-base:latest -f Dockerfile.base .
```

---

## 3. 多种服务启动与部署方式 (Deployment Variants)

### 模式 1：中国大陆 2 秒增量构建极速模式 (推荐生产/极速迭代)
基于 `wrenai-base:cn` 基础镜像，修改代码后仅需 2 秒即可完成增量重建：
```bash
docker-compose -f docker-compose.fast.yaml up -d --build
```

### 模式 2：Claude CLI 自动化数据源接入模式 (数据源自动 Onboarding)
基于容器化 Claude CLI 结合 `.claude/skills/offline_wren_generate-mdl` 离线 Skill，**无需人工干预**，全自动探查目标数据库 Schema、规范化数据类型，并生成全量 MDL 物理模型与校验构建：

- **一键脚本启动**：
  - Windows: `init_datasource_claude.bat`
  - Linux / macOS: `./init_datasource_claude.sh`
- **Compose 编排启动**：
  ```bash
  docker-compose -f docker-compose.claude.yaml up -d --build
  ```
  *(注：已解决 `--dangerously-skip-permissions` 限制、`--verbose` 流式参数、Qwen Jinja2 `400 System message must be at the beginning` 代理自动合并及 `wrenai-claude-init` 独立项目名隔离)*

### 模式 3：精简体积镜像模式 (Slim Mode)
适合镜像部署空间受限的场景：
```bash
docker-compose -f docker-compose.slim.yaml up -d --build
```

### 模式 4：标准双轨模式 (Standard Dual-Track Mode)
```bash
docker-compose up -d --build
```

### 模式 5：本地直接开发启动 (Local Development)
- **Windows 批处理调起**：`start_server.bat`
- **Python 一键脚本调起**：`python start_dual_services.py`

---

## 4. Wren System Prompt 提示词位置与注入机制

### 4.1 提示词定义源
WrenAI 语义引擎依赖专属的系统提示词指导 LLM 进行表结构探索与 SQL 推演。定义源位于 `wren_langchain` 包的 `WrenToolkit` (`toolkit.system_prompt()`)。

### 4.2 源码位置与注入节点

| 场景 / 轨道 | 源码文件 | 核心函数 / 节点 | 运行机制 |
|---|---|---|---|
| **Track B (FastAPI Agent)** | [`examples/server/agent_graph.py`](file:///D:/dev/WrenAI/sdk/wren-langchain/examples/server/agent_graph.py#L40-L60) | `ensure_wren_system_prompt()` | 在 `agent_node` 调用 LLM 前，自动将 `toolkit.system_prompt()` 放置在消息列表 Index 0 位置。无论前端上传文件如何插入消息，均自动合并归集至最头部，完全规避上游 vLLM / Qwen 400 报错。 |
| **Track A (FastMCP Engine)** | [`examples/wren_mcp_server.py`](file:///D:/dev/WrenAI/sdk/wren-langchain/examples/wren_mcp_server.py#L180-L190) | `@mcp.tool` `wren_get_system_prompt()` | 暴露 MCP 工具供外部客户端（如 DEEIX-Chat、Dify 等）主动拉取 Wren 语义提示词。 |

---

## 5. 全量环境变量参考表 (Environment Variables Reference)

### 5.1 核心服务与网络绑定
| 变量名 | 说明 | 默认值 / 示例 |
|---|---|---|
| `PROJECT_PATH` | Wren AI 语义项目目录路径（包含 `.wren` 与 `models/`）。 | `/project` 或 `./wren_project` |
| `PORT` | Track B FastAPI / OpenAI 代理服务监听端口。 | `8201` |
| `MCP_HOST` / `MCP_PORT` | Track A FastMCP 服务监听主机与端口。 | `0.0.0.0` : `8202` |
| `MCP_SSE_PATH` | 传统 MCP SSE 挂载路径。 | `/sse` |
| `MCP_STREAMABLE_HTTP_PATH` | Streamable HTTP JSON-RPC 挂载路径（DEEIX `baseURL`）。 | `/mcp` |

### 5.2 上游 LLM 配置
| 变量名 | 说明 | 默认值 / 示例 |
|---|---|---|
| `OPENAI_API_KEY` / `LLM_API_KEY` | 上游 LLM 鉴权 API Key。 | `sk-proj-...` |
| `OPENAI_API_BASE` / `LLM_API_BASE` | 上游 LLM 服务地址（支持 vLLM, OneAPI, DeepSeek 等）。 | `http://192.168.110.209:8200/v1` |
| `LLM_MODEL_NAME` | 物理部署的模型名称（虚拟别名如 `wren-agent` 自动映射至此）。 | `Qwen3.6-27B-AEON-Ultimate-Uncensored-BF16` |
| `LLM_REQUEST_TIMEOUT` | 上游 LLM 请求超时时间（秒）。 | `60` |

### 5.3 容器化 Claude CLI & Anthropic API 路由 (Claude Onboarding)
| 变量名 | 说明 | 默认值 / 示例 |
|---|---|---|
| `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY` | Anthropic API / OneAPI 鉴权 Token。 | `sk-dummy` |
| `ANTHROPIC_BASE_URL` | Anthropic API / 中转服务 Base 地址。 | `http://192.168.110.209:8200/v1` |
| `ANTHROPIC_MODEL` | Claude CLI 调用的主模型名称。 | `Qwen3.6-27B-AEON-Ultimate-Uncensored-BF16` |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | 覆盖 Claude Sonnet 别名路由。 | `${ANTHROPIC_MODEL}` |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` | 覆盖 Claude Haiku 别名路由。 | `${ANTHROPIC_MODEL}` |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` | 覆盖 Claude Opus 别名路由。 | `${ANTHROPIC_MODEL}` |
| `CLAUDE_CODE_SUBAGENT_MODEL` | 覆盖 Claude SubAgent 子代理模型路由。 | `${ANTHROPIC_MODEL}` |
| `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` | 开启 Agent 多智能体团队协同。 | `1` |

### 5.4 Track B 思考流与客户端降级防护
| 变量名 | 说明 | 默认值 / 示例 |
|---|---|---|
| `OPENAI_EXPOSED_MODELS` | `GET /v1/models` 暴露的模型列表。 | `wren-agent,wrenai,wren-semantic-analyst` |
| `OPENAI_PROCESS_STREAM_MODE` | 思考推演流输出模式：`reasoning`（输出思考流）、`text`、`both`、`off`。 | `reasoning` |
| `OPENAI_SSE_KEEPALIVE_SECONDS` | SSE 心跳保护间隔（秒），防止长耗时推演被连接切断。 | `15` |
| `OPENAI_SSE_KEEPALIVE_STYLE` | 心跳数据包格式：`comment` (`: keepalive\n\n`) 或 `empty_delta`。 | `comment` |
| `CHAT_HISTORY_DB_URI` | 持续化多轮会话 MySQL URI（为空使用 `MemorySaver`）。 | `mysql+pymysql://root:pass@host:3306/db` |

---

## 6. DEEIX-Chat 接入与会话保持排错指南

1. **HTTP 200 OK 优雅降级与会话保持**：
   当上游 LLM 报错或数据库连接超时时，Track B 接口不会抛出 HTTP 500 硬崩溃，而是返回 200 OK 结构并在 `content` 中返回提示。DEEIX-Chat 能正常将其追加为回复，**绝对保证本地 Session 历史不丢失、对话链条不中断**。
2. **支持 `user` 字段与会话绑定**：
   请求体中的 `user` 字段（或 `x-session-id` Header）会自动作为 LangGraph 的 `thread_id` 关联 Checkpointer，实现多轮对话持久化。

---
---

<a id="wren-langgraph-stateful-api-server--dual-track-integration"></a>
# Wren LangGraph Stateful API Server & Dual-Track Integration (English)

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

---

## 2. Base Image Packaging Guide

To speed up image builds and pre-package C extension dependencies (`mysqlclient`, `pkg-config`), you can build the pre-packaged base image:

### 2.1 Build CN Accelerated Base Image (`wrenai-base:cn`)
Pre-configured with **Tencent Cloud APT mirrors**, **npmmirror**, and **Tsinghua/Tencent PyPI mirrors**:

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
- `PROJECT_PATH`: Path to Wren semantic project (`/project` or `./wren_project`).
- `PORT`: Track B FastAPI / OpenAI Proxy port (`8201`).
- `MCP_HOST` / `MCP_PORT`: Track A FastMCP host and port (`0.0.0.0` : `8202`).

### 5.2 Upstream LLM & Anthropic Routing
- `OPENAI_API_KEY` / `LLM_API_KEY`: Upstream LLM API key.
- `LLM_API_BASE` / `OPENAI_API_BASE`: Upstream LLM base URL.
- `LLM_MODEL_NAME`: Target model deployed upstream (`Qwen3.6-27B-AEON-Ultimate-Uncensored-BF16`).
- `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_BASE_URL`: Auth token & URL for Claude CLI.
- `ANTHROPIC_DEFAULT_SONNET_MODEL` / `ANTHROPIC_DEFAULT_HAIKU_MODEL` / `CLAUDE_CODE_SUBAGENT_MODEL`: Model alias overrides for local LLM routing.

---

## 6. Automated Tests

Run full test suite:
```bash
pytest examples/tests -v
```
