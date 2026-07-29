# WrenAI LangGraph Examples 架构文档

> 本文档梳理 `sdk/wren-langchain/examples` 目录的架构与端到端流程。
> 文中行号截至 2026-07-29 的 `dev` 分支，可能随代码演变而漂移；引用时请以当前源码为准。
> 安装与运行步骤请参见同目录 `README.md`，本文聚焦"架构与流程"，不重复安装说明。

---

## 1. 概述与定位

### 1.1 这是什么

`examples` 是 **WrenAI 语义层 + LangGraph ReAct Agent** 的可运行示例集合与部署模板。它把 Wren 的自然语言→语义 SQL→执行能力，封装成两种对外的 API 形态（MCP 工具、OpenAI 兼容协议），并附带一个离线可用的 React 聊天 UI 与容器化部署件。

核心是单文件服务 `langgraph_fastapi_multi.py`（1739 行）——一个"双轨"FastAPI 服务器，外加其姊妹 MCP 服务 `wren_mcp_server.py`。

### 1.2 仓库中的位置与三层依赖

```
examples（示例编排层：FastAPI/LangGraph/MCP/UI/部署）
        │
        │  依赖三个原语：from_project / get_tools() / system_prompt()
        ▼
wren-langchain（SDK 薄 facade，src/wren_langchain/）
        │
        │  from wren.engine import WrenEngine  /  MemoryStore / profile / MDL 机制
        ▼
wren-engine（wren-core 的 Python 包：语义引擎本体）
```

- **示例层**只做编排：路由、SSE 流式、会话持久化、MCP 客户端、UI、Docker。
- **`wren-langchain` 库**本身很薄，公开 API 仅 `WrenToolkit` + 两个异常类；没有 agent/retriever/vector-store/LLM 封装——这些职责留给 `langchain`/`langgraph`（在示例里）和 `wren-engine`（底层）。
- **示例统一通过 `from wren_langchain import WrenToolkit` 导入**，四个示例脚本都用同一个"三原语"模式。

---

## 2. 整体架构：双轨设计（核心）

整个示例集合的关键架构决策是：**一张 LangGraph ReAct 图编译两次**，对外暴露两条收敛到同一语义层的访问轨道。

### 2.1 一图两编译

```
          ┌──────────────────────── 同一个 build_app() ────────────────────────┐
          │  AgentState(messages)                                                  │
          │  START → agent ──┐                                                     │
          │          ▲       ▼ should_continue                                     │
          │          │     有 tool_calls → tools(ToolNode) → 回到 agent           │
          │          │     无 tool_calls → END                                     │
          │  工具 = wren_query/wren_dry_plan/wren_list_models                      │
          │       + get_current_time + 动态发现的 MCP 工具                         │
          │  LLM  = ChatOpenAI(temperature=0, 1M ctx).bind_tools(tools)           │
          └──────────────────────────────────────────────────────────────────────┘
                 │ 编译 A：checkpointer=MySQL/MemorySaver        │ 编译 B：checkpointer=None
                 ▼                                              ▼
   langgraph_app（有状态）                       langgraph_app_stateless（无状态）
   供原生 /chat、/chat/stream                     供 /v1/chat/completions
```

- **有状态版**（`langgraph_app`）：绑定 checkpointer，按 `thread_id`（= 会话 id）自动加载/持久化历史，支撑多轮对话。
- **无状态版**（`langgraph_app_stateless`）：无 checkpointer，假设 OpenAI 协议客户端每轮重发完整上下文。
- 两版共享同一 toolkit、同一工具集、同一 MCP 工具注册表、同一 LLM 客户端缓存。

### 2.2 架构总图

```
                          ┌─────────────────────────────────────────────┐
                          │            WrenAI 语义层（共享）             │
                          │  WrenToolkit.from_project(PROJECT_PATH)      │
                          │  → get_tools() + system_prompt()             │
                          │  → WrenEngine → MDL manifest → 目标数据库    │
                          └─────────────────────────────────────────────┘
                                   ▲                          ▲
                                   │ 同一 toolkit             │ 同一 toolkit
              ┌────────────────────┘                          └───────────────────┐
              │                                                                   │
   ╔═══════════════════════════╗ Track B                        ╔══════════════════════════╗ Track A
   ║ langgraph_fastapi_multi.py ║ 端口 8201                     ║   wren_mcp_server.py     ║ 端口 8202
   ║───────────────────────────║                                ║─────────────────────────║
   ║ GET  /                    ║ ← React UI(wren-chat-ui)       ║ FastMCP dual transport   ║ ← MCP 客户端
   ║ POST /chat, /chat/stream  ║ ← 原生会话(NDJSON)             ║  GET  /sse  (classic SSE) ║
   ║ POST /v1/chat/completions ║ ← OpenAI 协议客户端(DEEIX 等)  ║  *    /mcp  (Streamable)  ║ ← DEEIX
   ║ GET  /v1/models,/health   ║                                ║  wren_query / dry_plan /  ║
   ║ GET  /docs,/static/*      ║                                ║  list_models + memory    ║
   ╚═══════════════════════════╝                                ╚══════════════════════════╝
```

- **Track B（端口 8201，`langgraph_fastapi_multi.py`）**：同时承载原生 Wren 会话 API（有状态）与 OpenAI 兼容 API（无状态），并托管聊天 UI、Swagger 文档、静态资源。
- **Track A（端口 8202，`wren_mcp_server.py`）**：FastMCP **双传输**服务（经典 SSE `/sse` + Streamable HTTP `/mcp` 同端口并存），把同一 toolkit 包装成真实 MCP 工具，供 MCP 客户端（如 DEEIX-Chat）调用。DEEIX 仅支持 Streamable HTTP，应配置 `baseURL=…:8202/mcp`。
- 两条轨道**不在进程内互通**，各自独立实例化 `WrenToolkit`；它们是"同一语义层的两种暴露方式"。

---

## 3. 分层组件职责

### 3.1 库层 `wren-langchain`

示例能保持薄，是因为重活在库里完成。公开入口仅 `WrenToolkit`，三个原语：

| 原语 | 职责 | 源码位置 |
|---|---|---|
| `WrenToolkit.from_project(path, *, profile=None)` | 校验 `wren_project.yml` + `target/mdl.json`；加载 `<project>/.env`；装配 MDL 源、连接 profile、记忆 provider（按 `.wren/memory/` 自动选择 LanceDB 或 Noop） | `src/wren_langchain/_toolkit.py` |
| `toolkit.get_tools(*, include_memory_write=True, raise_on_error=False)` | 返回 3 个运行时工具 +（记忆开启时）最多 3 个记忆工具，均为 `langchain_core.tools.BaseTool` | `_tools.py` / `_tools_memory.py` |
| `toolkit.system_prompt(*, tools=None)` | 依据"实际绑定的工具列表"动态合成工作流提示词（recall→fetch→compose→dry_plan→query→store，缺工具则省对应步骤） | `_prompt.py` |

**运行时工具**（`_tools.py`）：

- `wren_query(sql, limit=100)` — 经语义层执行 SQL；硬上限 `MAX_QUERY_ROWS=1000`；返回 `data.rows`，内容预览 16KB。
- `wren_dry_plan(sql)` — 展开 MDL，返回目标方言 SQL 字符串。
- `wren_list_models()` — 列出 manifest 中的模型（列数、描述）。

**记忆工具**（`_tools_memory.py`，仅 `.wren/memory/` 存在时加入）：

- `wren_fetch_context(question, limit=5, ...)` — embedding 检索 schema/业务上下文。
- `wren_recall_queries(question, limit=3)` — 检索历史 NL→SQL 对作 few-shot。
- `wren_store_query(nl, sql, tags)` — 持久化已确认的 NL/SQL 对（受 `include_write` 控制）。

**Envelope 契约**（`_envelope.py`，工具 ↔ agent 间的统一返回结构）：

- 成功：`{"ok": True, "content": str, "data": dict, "warnings": list}`
- 错误：`{"ok": False, "content": str, "error": {code, phase, message, metadata}}`
- `phase`（`SQL_PARSING` / `METADATA_FETCHING`/`MDL_EXTRACTION` / `SQL_EXECUTION`）驱动 agent 的恢复路径。
- `_format.py` 渲染 `content` 字段（markdown 表格、代码块、按字节截断）；含 `redact_secrets`（password/secret/token/credential → `***`）。

**实现要点**：`_build_engine()` 每次调用新建 `WrenEngine`（每次从磁盘重读 manifest，便于 `wren context build` 后即时生效），但连接器在 toolkit 层缓存。直接 Python API（`toolkit.query/dry_plan/dry_run` 与 `toolkit.memory.*`）绕过 LangChain 工具层。

**示例对库的使用**：仅用三原语，外加两处读取私有属性 `toolkit._memory.enabled` 做状态展示（`langgraph_demo.py:145`、`langgraph_fastapi_multi.py:1215`）——这是示例越过公开 API 的唯一地方。

### 3.2 Track B：`langgraph_fastapi_multi.py`

按职责分块：

**应用骨架与生命周期**
- `FastAPI(...)`（945–952），CORS 全开（955–961）。
- `lifespan(app)`（779–941）：启动——`mcp_manager_worker` 后台任务 → `asyncio.to_thread(WrenToolkit.from_project, ...)`（含失败时的 lazy 回退）→ 加载 `MCP_CONFIG_DIR` 下 MCP 配置并连接/列工具 → 选 checkpointer（`CHAT_HISTORY_DB_URI`→MySQL，否则 MemorySaver）→ 编译两版图；关闭——取消重连轮询、经管理队列串行关闭 MCP、关 MySQL 栈。

**LangGraph 图** —— `build_app(toolkit, checkpointer=None, model_name="gpt-4o")`（483–565）
- 装配工具：`toolkit.get_tools()` + `get_current_time`（本地 `@tool`，452–459，给报表生成提供当前时间）+ `global_mcp_tools`。
- `get_model()`（505–522）缓存 `ChatOpenAI(temperature=0, request_timeout=60, extra_body.num_ctx=1048576).bind_tools(tools)`。
- `agent_node`（524–548）：幂等注入 Wren 系统提示词后调模型；异常则清缓存重试一次。
- `should_continue`（550–554）：末条 `AIMessage` 有 `tool_calls`→`"tools"`，否则 `END`。
- `tools` 节点 = `ToolNode(tools)`（预构建）。
- 边：`START→agent`、`agent→条件`、`tools→agent`——教科书式 ReAct 循环。
- `AgentState`（448–449）：`messages: Annotated[list[BaseMessage], add_messages]`。

**动态 MCP 工具发现**（ Track B 既是 MCP 服务器工具的**消费方**）
- `_real_get_or_create_mcp_session`（155–233）：按配置支持三种传输——`url`+`streamable_http`（或 URL 含 `/mcp` 自动判定）、`url`+SSE（默认）、`command`+`args`+`env` 的 stdio；超时由 `MCP_CONNECT_TIMEOUT`/`MCP_READ_TIMEOUT` 控制。
- `mcp_manager_worker`（236–278）：单任务串行处理所有 MCP 连接/断开——这是规避 `anyio` cancel-scope 跨任务报错的刻意设计。
- `convert_mcp_to_langchain`（343–443）：把 MCP 工具包成 `StructuredTool`；`json_schema_to_pydantic`（288–316）生成 args_schema；`_acall`（360–421）经 `get_or_create_mcp_session` 取会话、`session.call_tool(..., progress_callback=...)`，失败则强制重连重试一次；工具名加 `server_name_` 前缀避撞。
- `retry_failed_mcp_connections_loop`（576–623）：每 `MCP_RETRY_INTERVAL`（默认 30s）轮询失败的 MCP 服务器；成功后失效图缓存，使下次请求用扩大的工具集重建图。

**OpenAI 协议适配**
- `convert_openai_messages`（1249–1267）：OpenAI 消息 → LangChain 消息。
- 流式：`event_stream_generator`（1444–1553）——发初始 assistant chunk → 起 `asyncio.Queue` + 独立 `astream` 任务 → 按 keepalive 超时发心跳 → 遍历节点更新发 reasoning trace 与 content delta → MCP `progress_callback` 回注队列发进度 trace → 结束发 `finish_reason=stop` 与 `data: [DONE]`。
- 非流式：`ainvoke` 后从 messages 取末条 `AIMessage.content` + 聚合 reasoning traces，返回单条 `chat.completion`，token 用量按字符数/4 估算。
- `OPENAI_PROCESS_STREAM_MODE`（`off|text|reasoning|both`，默认 `reasoning`）决定是否在 SSE 中带 `delta.reasoning_content` 思考痕迹。
- `OPENAI_SSE_KEEPALIVE_*`（默认 15s、`comment` 风格 `: keepalive\n\n`）。
- `sanitize_and_truncate_text`（1298–1309）：出站 secret 脱敏 + 截断（保护上游 tool/DB 凭据不回流客户端）。

**会话持久化** —— `ReconnectingPyMySQLSaver`（629–769）
-继承 `PyMySQLSaver`，用 `threading.RLock`（635，规避可重入死锁）。
- `_ping_unlocked`（637–658）：每次操作前 ping，失败则用缓存的 `conn_args` 重连（`autocommit=True`、`lock_wait_timeout=5`）。
- 同步 `setup/get_tuple/list/put/put_writes` 包锁+ping；异步变体经 `run_in_executor` 委托同步版。
- `from_conn_string`（705–767）：解析 MySQL URI，套安全默认（`connect_timeout=5`、read/write_timeout=15、`ssl_disabled=True`、`autocommit=True`、`init_command="SET SESSION lock_wait_timeout=5"`），支持查询串覆盖合法 `pymysql.connect` kwargs。
- 启动时 setup 在工作线程内限时 10s（881–895）；任何失败即回退 `MemorySaver`。
- Windows 上 `localhost`→`127.0.0.1`（847–869），避免 socket 解析挂起。

**延迟初始化与序列化**
- `lazy_init_app`（1568–1623）：线程/异常安全的兜底初始化，被每个端点调用；镜像 lifespan 的 checkpointer 选择逻辑，保证 lifespan 失败也能启动。
- `serialize_message`（998–1022）：LangChain 消息 → JSON dict。
- `get_local_or_cdn`（1030–1054）：本地优先、CDN 兜底的静态资源取用，支持离线 Swagger。

**已知小瑕疵（如实标注）**：`langgraph_app_stateless` 在多处被读（909、1214、1385 等），但规范的全局声明块（775–776）只声明了 `toolkit` 与 `langgraph_app`；它在 `lifespan`/`lazy_init_app` 内被赋值，依赖 Python 模块全局语义——不影响运行，但属源码风格不一致。

### 3.3 Track A：`wren_mcp_server.py`

`FastMCP`（`mcp` 包）**双传输**服务，默认端口 8202（`MCP_HOST`/`MCP_PORT`/`HOST`/`PORT` 可配；路径可由 `MCP_SSE_PATH`/`MCP_STREAMABLE_HTTP_PATH` 覆盖）。延迟实例化 `WrenToolkit.from_project(PROJECT_PATH)`，把 **真实** `toolkit.get_tools()` 工具一对一暴露为 MCP tools（方案 C），由上游 LLM 自己做 ReAct——**不再**提供虚假的 NL2SQL 入口 `wren_semantic_query`。

| 传输 | 路径 | 说明 |
|---|---|---|
| Classic SSE | `GET /sse` + `POST /messages/` | 长连接事件流；兼容旧 MCP 客户端 |
| Streamable HTTP | `POST /mcp`（FastMCP 默认） | JSON-RPC；**DEEIX-Chat 仅支持此传输** |
| Health | `GET /health` | 返回双传输路径与工具清单 |

实现上合并 `mcp.sse_app()` 与 `mcp.streamable_http_app()` 的路由表到同一个 Starlette `app`，并用 `session_manager.run()` 作为 lifespan（Streamable HTTP 会话任务组必需）。`__main__` 走 `uvicorn.run(app, …)`，**不**再调用 `mcp.run(transport=…)`（后者一次只能启一种传输）。

| MCP 工具 | 实现 |
|---|---|
| `wren_query(sql, limit=100)` | 转发 `toolkit.get_tools()` 中的同名 LangChain 工具；返回 envelope JSON |
| `wren_dry_plan(sql)` | 同上 |
| `wren_list_models()` | 同上 |
| `wren_fetch_context` / `wren_recall_queries` / `wren_store_query` | 同上；memory 未启用时返回明确错误（仍注册在 MCP 表面） |
| `wren_get_system_prompt()` | `toolkit.system_prompt(tools=实际可用工具列表)` |

Track A **不**内嵌 agent，全部经 `WrenToolkit`；NL→SQL 规划属于调用方 LLM。完整黑盒 agent 请用 Track B。

### 3.4 两个精简 demo

| 文件 | 模式 | 适用 |
|---|---|---|
| `langchain_demo.py` | `langchain.agents.create_agent(model, tools=..., system_prompt=...)`（langgraph 1.0+ 推荐工厂，`create_react_agent` 已弃用） | 想最快跑通一次问答 |
| `langgraph_demo.py` | 手搓 `StateGraph` + `ToolNode`，显式 `agent`/`should_continue`/`tools`，`STREAM=1` 时 `stream_mode="updates"` | 想自定义路由/状态/流式/中间件 |

两者皆用同一三原语，图结构同为 ReAct；区别仅在"用工厂"还是"自己拼"。`langgraph_demo` 的图结构在 12–27 行注释、79–106 行构建。

### 3.5 前端 `wren-chat-ui`

**React 19 + TypeScript + Vite 8 + Tailwind 4**，用 `vite-plugin-singlefile` 把整个应用编译进单个自包含 `dist/index.html`（无 CDN、无运行时外链，专为离线/气隙环境）。`vite.config.ts` 设 `base:'./'` + `viteSingleFile()`，`index.html` 为 `lang="zh-CN"`、标题"问数智能体 - Powered by WrenAI"。

关键文件：
- `src/App.tsx`（465 行）——会话/消息/后端 URL 编排；`getDefaultBackendUrl()`（63–72）默认指向页面自身 origin（被 FastAPI 托管时即回连同源）；`handleSendMessage()`（218–353）POST `/chat/stream` 并用 `parseNDJSONStream` 解析；会话持久化于 `localStorage`（`wren_recent_chats`/`wren_active_session_id`/`wren_messages_<id>`/`wren_backend_url`）；含 `safeLocalStorage`（13–38，`file://` 内存回退）与 `safeCrypto.randomUUID`（44–57）。
- `src/utils/stream.ts`（101 行）——`parseNDJSONStream()`：读 `Response.body` reader、按行切分、JSON 解析为 `StreamUpdate`（`{session_id, agent?, tools?, progress?, error?}`）。
- `src/components/Sidebar.tsx`（186 行，会话增删改名，标题"问数智能体"）、`ChatInput.tsx`（89 行，Enter 发送/Shift+Enter 换行）、`MessageList.tsx`（397 行，含轻量 Markdown 渲染 + 可折叠 `ToolCallCard` accordions）、`SettingsModal.tsx`（156 行，后端 URL + "测试连接" 命中 `/health`）。

前端**只走 Track B 同源**：`POST /chat/stream`（主聊天，NDJSON）、`GET /chat/history/{session_id}`（会话恢复）、`GET /health`（连通性测试）；不直接访问 Track A。若 `dist/index.html` 缺失，FastAPI 在 `GET /`（1057–1124）回退到一个展示双轨端点的中文状态面板。

### 3.6 静态资源 `static/`

三个预打包离线资源：`favicon.png`、`swagger-ui-bundle.js`（~1.5MB）、`swagger-ui.css`（~179KB）。经 `get_local_or_cdn()` 本地优先、CDN 兜底提供给 `/docs`、`/static/*`，使 Swagger UI 全离线可用。

---

## 4. 项目与数据源初始化

> 本节回答"示例运行前，Wren 项目里到底要有哪些文件、数据源怎么接通"。底层实现在 `core/wren`（CLI/SDK）与 `wren-langchain`（`WrenToolkit`）。文末行号为当前源码，可能漂移。

### 4.1 一个合法 Wren 项目的目录结构

权威布局见 `docs/core/guides/manage_project.md` 与 `docs/core/reference/mdl.md`。注意：**建模源是 YAML（`metadata.yml`），仓库里不存在 `.mdl` 文件**——`.mdl` 描述仅是历史叫法，勿在文档中沿用。

```
my_project/
├── wren_project.yml          # 项目清单（name / data_source / profile / schema_version …）
├── models/                   # 建模源：每个模型一个目录
│   └── <name>/
│       ├── metadata.yml      #   模型定义（table_reference 或 ref_sql + columns）
│       └── ref_sql.sql       #   可选；存在则覆盖 metadata.yml 的内联 ref_sql
├── views/<name>/             # 可选保存视图（metadata.yml + 可选 sql.yml）
├── cubes/<name>/             # 可选预聚合
├── relationships.yml         # 跨模型关联
├── instructions.md           # 给 agent 的业务指引（可选，会进 system_prompt）
├── queries.yml               # 策展 NL-SQL 对（可选，记忆种子）
├── AGENTS.md                 # agent 工作流指引（init 自动生成）
├── target/
│   └── mdl.json              # 构建产物（camelCase manifest；WrenEngine 实际消费）
└── .wren/                    # 项目本地 CLI 运行态（gitignore）
    ├── profiles.yml          #   数据源连接 profile（位置由 WREN_HOME 决定，见 4.4）
    └── memory/               #   LanceDB 索引（wren memory index 产出；存在即启用记忆）
```

**schema_version 与加载布局**（`core/wren/src/wren/context.py`）：支持 `schema_version ∈ {1,2,3,4}`（`_SUPPORTED_SCHEMA_VERSIONS` 435）。v1 为扁平文件（`models/*.yml`）；v2+ 为"每实体一目录"（`models/<name>/metadata.yml`，`_load_models_v2` 517）。`_LAYOUT_VERSION_MAP`（438）映射 `{1:1, 2:1, 3:2, 4:3}`——v3 引入模型/视图的 `dialect`，v4 引入复合主键（list 形 `primary_key`）。

**三个 CLI 命令及其产物**（`core/wren/src/wren/context_cli.py`）：

| 命令 | 行号 | 作用 |
|---|---|---|
| `wren context init [--empty]` | 152–386 | 脚手架新项目。`--empty` 跳过示例模型/视图（entrypoint/bat 用此）；否则建 `models/`/`views/`/`cubes/`、`wren_project.yml`（274–286）、`relationships.yml`、`instructions.md`、`AGENTS.md`、`queries.yml`。还支持 `--from-mdl`（导入现有 `mdl.json` 转 YAML）与 `--from-osi`（单向 OSI 迁移） |
| `wren context build` | 582–663 | 编译 YAML → `target/mdl.json`。先 `validate_project`（除非 `--no-validate`），调用 `build_json(project_path)`（context.py 713）：装 snake_case 清单 → `_convert_keys` 转 camel → 盖 `layoutVersion` → `save_target` 写 `indent=2, ensure_ascii=False`。≥200 模型时提示开启记忆 |
| `wren context validate` | 395–545 | 结构校验（`wren_project.yml` 有 `name`/`data_source`、模型有 `name`+列+唯一 `table_reference` 或 `ref_sql`、视图有 `statement`、关联引用存在模型…）+ 语义校验（对每视图 SQL 干跑；缺 `properties.description` 在 warning/strict 下告警）；并校验所钉 profile 存在于 `profiles.yml` |

### 4.2 `wren_project.yml` 字段与绑定

字段表（`docs/core/reference/mdl.md` 44–65；重写时字段顺序见 `context.py` 402–410 `_PROJECT_FIELD_ORDER`）：

| 字段 | 必需 | 含义 |
|---|---|---|
| `schema_version` | 是 | CLI 持有；`wren context upgrade` 升级。脚手架默认 3 |
| `name` | 是 | 项目名（缺失即校验报错） |
| `version` | 否 | 自由字符串，无解析效果 |
| `catalog` | 否（默认 `wren`） | **Wren 引擎内部命名空间**，**不是** DB catalog |
| `schema` | 否（默认 `public`） | **Wren 引擎内部命名空间**，**不是** DB schema |
| `data_source` | 是 | 数据源类型（取自 profile 的 `datasource`，见 4.5）。`DataSource` 枚举之一 |
| `profile` | 否 | `profiles.yml` 中的 profile 名；由 `wren context set-profile` 或 entrypoint Python 注入 |

**最易踩坑点**：项目级 `catalog`/`schema` 是 Wren 内部命名空间；每个模型的 `table_reference.catalog`/`.schema`/`.table` 才指向**底层库**位置。二者不可混淆。`save_project_config()` 用 `yaml.safe_dump` 重写，**会丢弃 YAML 注释**——注释只在 init 模板里存活，第一次 `set-profile`/`upgrade` 后即消失。

**`profile` 与 `data_source` 的绑定**：`profile` 指向 `profiles.yml` 中同名条目；`data_source` 在绑定（`set-profile`，context_cli.py 834–846）时**从 profile 的 `datasource` 字段拷贝**而来，二者由 `set-profile` 保持同步；若事后手改 `profiles.yml`，二者可能漂移，`validate` 会告警但不阻断。`set-profile` 还会在数据源已变且 `target/mdl.json` 存在时发 stale-MDL 警告（864–871）。

**真实样例**：仓库内无提交的 `wren_project.yml`。脚手架模板（context_cli.py 274–286）：
```yaml
schema_version: 3
name: my_project
version: "1.0"
catalog: wren
schema: public
data_source: postgres  # change to your datasource type
```
（脚手架**不含** `profile:`，需后续 `set-profile` 或 entrypoint 注入。）

### 4.3 建模源文件：模型 / 视图 / 关联 / 立方

**模型 `models/<name>/metadata.yml`**——二选一定义来源：

`table_reference` 模式（脚手架 context_cli.py 307–330）：
```yaml
name: customers
table_reference:
  catalog: ""        # 库 catalog（无则空）
  schema: public      # 库 schema
  table: customers    # 库表名
columns:
  - name: id
    type: INTEGER
    is_primary_key: true
    not_null: true
    properties: {}
  - name: first_name
    type: VARCHAR
primary_key: id
cached: false
properties: {}
```

`ref_sql` 模式（mdl.md 128–140）：
```yaml
name: revenue_summary
ref_sql: |
  SELECT DATE_TRUNC('month', order_date) AS month,
         SUM(total) AS total_revenue
  FROM orders GROUP BY 1
columns:
  - {name: month, type: DATE}
  - {name: total_revenue, type: DECIMAL}
```
`ref_sql` 可内联或放兄弟文件 `ref_sql.sql`——**兄弟文件优先**（context.py 540–545）。模型必须**恰好有** `table_reference` 或 `ref_sql` 之一（否则校验报错，context.py 830–847）。列字段：`name`(必)、`type`(必)、`is_calculated`、`expression`、`relationship`(使之成为 join 句柄列)、`not_null`、`is_primary_key`、`is_hidden`、`properties`。

**关联 `relationships.yml`**（mdl.md 197–213）：
```yaml
relationships:
  - name: orders_customers
    models: [orders, customers]
    join_type: MANY_TO_ONE
    condition: orders.customer_id = customers.customer_id
```

**视图 `views/<name>/metadata.yml`**：`name`+`statement`（完整 SELECT）+可选 `dialect`/`properties`；statement 可内联或放 `sql.yml`（后者优先，context.py 594–598）。**立方 `cubes/<name>/metadata.yml`** 为可选预聚合，结构与语义深度校验在 Rust 侧 `AnalyzedWrenMDL`。

### 4.4 `profiles.yml`：位置由 `WREN_HOME` 决定（关键微妙点）

`core/wren/src/wren/profile.py` 20–21：
```python
_WREN_HOME = Path(os.environ.get("WREN_HOME", Path.home() / ".wren"))
_PROFILES_FILE = _WREN_HOME / "profiles.yml"
```

不存在"项目本地 vs 全局两套代码路径"——始终是同一个 `_PROFILES_FILE`，只是 `WREN_HOME` 不同。**默认全局** `~/.wren/profiles.yml`；**示例 entrypoint 脚本把 `WREN_HOME` 改写为 `<project>/.wren`**（entrypoint.sh 57–58、start_server.bat 223–225），从而把 profiles 移到项目本地。这是获得项目本地 profile 的唯一方式。

`profiles.yml` 样例（`manage_project.md` 163–173；`_load_raw` 143–177 强约束）：
```yaml
active: default
profiles:
  default:
    datasource: mysql
    host: 127.0.0.1
    port: 3306
    database: my_database
    user: root
    password: ${DB_PASSWORD}
    ssl_mode: DISABLED
  pg-prod:
    datasource: postgres
    host: db.example.com
    port: '5432'
    user: ${POSTGRES_USER}
    password: ${POSTGRES_PASSWORD}
```
强约束：顶层为 mapping、`profiles` 为 mapping、`active` 为 str/null；其余 per-profile 字段自由（`_load_raw` 不校验字段集，权威字段集见 4.5 的 `DATASOURCE_MODELS`）。文件以 `0600` 原子写（`_save_raw` 180–196）。

**`${ENV_VAR}` 展开**（`expand_profile_secrets`，profile.py 121–140）：自定义 `string.Template` 子类 `idpattern=r"[_A-Z][_A-Z0-9]*"`——**仅大写占位符被替换**，小写 `${foo}`（可能出现在真实密码/URL 中）原样保留；`$$` 转义字面 `$`；递归 walk dict/list；未解析变量抛 `MissingSecretError` 快速失败。配套 `.env` 发现顺序（`_ensure_env_loaded` 44–91，`override=False` 即已存在的 env 不被覆盖）：① `$CWD/.env` ② 自 CWD 向上找含 `wren_project.yml` 的目录并读其 `.env` ③ `~/.wren/.env`。

**SDK 的关键偏离**：`WrenToolkit._load_project_dotenv`（`_toolkit.py` 199–218）在构造 `ProfileConnectionProvider` **之前**直接加载 `<project>/.env`，因此 `from_project("/any/path")` 能正确解析该项目的密钥，不受 CWD 影响（Core 的 CWD-walk 此时还不会触发）。回归测试 `test_toolkit_init.py` 57–104 锁定此行为。

### 4.5 支持的数据源与连接器

两套互补注册表：

**`DataSource` 枚举**（`core/wren/src/wren/model/data_source.py` 40–60）：`athena, bigquery, canner, clickhouse, datafusion, mssql, mysql, doris, oracle, postgres, redshift, snowflake, trino, local_file, s3_file, minio_file, gcs_file, duckdb, spark, databricks`。

**连接器工厂**（`core/wren/src/wren/connector/factory.py` 6–37）：每个 `DataSource` 映射到连接器模块；别名 `doris→mysql 连接器`、`canner→postgres 连接器`、所有 `*_file→duckdb 连接器`。`_INSTALL_EXTRA`（30–37）给别名映射 pip extra 名；`_NEEDS_DATA_SOURCE={mysql,doris,trino}`（39–43）需把 `DataSource` 透传给 `create_connector` 以判定方言。

**pip extras**（`core/wren`）：`postgres, mysql, bigquery, snowflake, clickhouse, trino, mssql, databricks, redshift, spark, athena, oracle, memory, all, dev`。DuckDB 含在 base，无需 extra。

**per-source 字段集**：`DATASOURCE_MODELS`（`core/wren/src/wren/model/field_registry.py` 43–70）把每个数据源映射到一个或多个 Pydantic `ConnectionInfo` 模型（BigQuery/Databricks/Redshift 为判别联合，有多种变体）；`DataSource._build_connection_info`（data_source.py 106–165）按枚举分派并校验 dict。`wren docs connection-info <ds>` 打印权威字段清单。

**安装注意**：`wren-langchain` 的 extras（`postgres/mysql/.../memory/all`）链式透传到 `wren-engine[<ds>]`。示例里 `Dockerfile` 装 `core/wren[all]` 且对 `wren-langchain` 做 `sed 's/wren-engine/wrenai/g'` 后装 `.[all]`（`Dockerfile` 35、41），使 `requirements.txt` 中各 DB 驱动在运行期可选。

### 4.6 SDK 的 profile 三层解析（`ProfileConnectionProvider`）

`wren-langchain` `src/wren_langchain/_providers/connection.py` 25–81，`_resolve_profile`（41–60）按优先级：

1. **显式 kwarg** `WrenToolkit.from_project(path, profile="...")`——最高优先；`is not None` 判定，故空串会报"profile not found"而非穿透。
2. **`wren_project.yml` 的 `profile:` 字段**——经 `load_project_config` 读（71–74）。
3. **全局 active profile**——`wren.profile.get_active_profile()`（54）；无则抛 `WrenToolkitInitError`（56–59）。

命名 profile 查找 `_lookup_named_profile`（62–69）调 `list_profiles`，缺失则抛 `WrenToolkitInitError` 并附可用名列表。查找后施加 `expand_profile_secrets`，弹 `datasource` 入 `self._datasource`，余下入 `self._connection_info`（38–39）。Core 侧 `resolve_profile_for_project`（profile.py 219–266）对 CLI 命令做类似两步（项目钉选→全局 active），缺 profile 时 `SystemExit`；SDK 包装为 `WrenToolkitInitError`。

### 4.7 连接器构建与缓存

**引擎侧**（`core/wren/src/wren/engine.py` 237–240）：`_get_connector` 懒调 `get_connector(self.data_source, self.connection_info)` 并挂 `self._connector`；`WrenEngine.__init__`（55–81）若 connection_info 非空则经 `data_source.get_connection_info(dict)` 构造类型化 `ConnectionInfo`。

**SDK 缓存**（`_toolkit.py` 43–49、138–153）：toolkit 在 `_build_engine` 调用间缓存 `self._connector_cache`。**每次调用重建新 `WrenEngine`**（每次从磁盘重读 manifest 以 read-through 方式吸取 `wren context build` 更新，见 `ProjectMDLSource.load_manifest` `mdl_source.py` 23–38），但首次查询后把连接器从引擎提升回 toolkit（`self._connector_cache = engine._connector`，line 67）并注入下次引擎（`engine._connector = self._connector_cache`，line 152）。即 **DB 鉴权每 toolkit 生命周期一次；manifest 始终新鲜**。

### 4.8 记忆层：`wren memory index` / LanceDB / 自动启用

**`wren memory index`**（`core/wren/src/wren/memory/cli.py` 150–241）：`--mdl`（默认 `<project>/target/mdl.json`）、`--path`（默认 `<project>/.wren/memory/`，`_default_memory_path` 47–54）定位清单与 LanceDB 目录；可选把 `instructions.md` 折为 `manifest["_instructions"]`（173–193）；懒导入 `wren.memory.store`，缺 `lancedb`/`sentence_transformers`/`pyarrow` 时友好退出；`mem_store.index_schema(manifest, seed_queries=not no_seed)`（196），entrypoint 用 `--no-seed` 跳过种子生成；并容忍性地把 `queries.yml` 策展对载入 `query_history`（203–240）。

**索引内容**（`extract_schema_items`，`schema_indexer.py` 220–259）：每模型/列/关联/视图/立方各一条记录，含合成 `text`（embedding 用）、结构化元数据（`item_type`/`model_name`/`item_name`/`data_type`/`expression`/`is_calculated`）与 `mdl_hash`（仅 schema 部分的 SHA-256，排除 `_instructions` 等内部键，故指令编辑不使 schema 缓存失效）。

**LanceDB 存储 `MemoryStore`**（`core/wren/src/wren/memory/store.py` 73–97）：默认路径 `~/.wren/memory`，但 CLI/entrypoint 显式传 `<project>/.wren/memory`，故示例中 LanceDB 在 `<project>/.wren/memory/`；`mkdir(parents=True, exist_ok=True)`+`lancedb.connect`；两表 `schema_items`（34–48）与 `query_history`（51–62）显式 pyarrow schema。embedding 由 `WREN_EMBEDDING_PROVIDER` 选 `openai`/`openai-compatible`（命中 `${EM_OPENAI_BASE_URL}/embeddings`，embeddings.py 19–46）或默认 `sentence-transformers`（默认模型 `paraphrase-multilingual-MiniLM-L12-v2`，384 维）。`index_schema` 默认 `replace=True`（重建表）；`seed_queries=True` 时调 `_upsert_seed_queries` 生成规范 NL-SQL 对并保留用户已确认项。

**SDK 自动启用**（`_toolkit.py` 220–229 `_resolve_memory_provider`）：**仅以目录存在与否为信号**——`.wren/memory/` 是目录（`is_dir()`，防普通文件/坏软链）即返回 `LocalLanceDBMemoryProvider`（`enabled=True`，懒开 `MemoryStore`；模型加载昂贵，故 toolkit 层缓存 `self._memory_store_cache`），否则 `NoopMemoryProvider`（`enabled=False`，`open()` 抛 `MemoryNotEnabledError`，`get_tools()` 因此省略记忆工具，`_toolkit.py` 100–109）。**无 kwarg 覆盖**——文档明示："启用就跑 `wren memory index`；禁用就删目录。"

**阈值行为**：`SCHEMA_DESCRIBE_THRESHOLD=30000`（schema_indexer.py 36）。`MemoryStore.get_context`（store.py 211–238）在全文 `describe_schema` ≤ 阈值时返回 `strategy="full"`（不走 embedding 检索），越过才用 embedding 检索；但索引仍为 `recall`（NL-SQL few-shot）与 `schema_is_current` 校验而建。

### 4.9 `WrenToolkit.from_project` 的校验路径

入口 `from_project`（`_toolkit.py` 155–197）；示例在 `langgraph_fastapi_multi.py:796` 经 `asyncio.to_thread(WrenToolkit.from_project, project_path)` 调用，失败回退 `lazy_init_app:1582`。**按序校验**：

1. **`wren_project.yml` 存在**（171–175）——纯存在性检查，不校验内容（conftest 仅写 `"schema_version: 1\n"` 即过关）。
2. **`target/mdl.json` 存在**（177–181）——纯存在性；内容在首次 `load_manifest()` 懒校验，坏 JSON 重抛 `WrenToolkitInitError`（`mdl_source.py` 33–38）。
3. **加载 `<project>/.env`**（`_load_project_dotenv` 199–218，`override=False`）——见 4.4 的偏离说明。
4. **装配三 provider**（185–197）：`ProjectMDLSource`（read-through manifest）、`ProfileConnectionProvider`（4.6 三层解析，缺 profile 抛 `WrenToolkitInitError`）、`_resolve_memory_provider`（4.8 目录探测）。

**`WrenToolkitInitError`**（`exceptions.py` 4–9）由：① ② 两存在性检查、`ProfileConnectionProvider` 无可解析 profile（connection.py 56–59）或命名 profile 缺失（65–68）、`ProjectMDLSource.load_manifest` 坏 JSON 抛出。契约测试见 `test_toolkit_init.py` 14–104。

**`from_project` 故意不检查**：无 `models/` 检查（空 models + 陈旧 `mdl.json` 仍过关）、无 `wren_project.yml` 的 `data_source`/`profile` 字段检查（仅查存在性，profile 懒读到全局 active 兜底）、无 `instructions.md`/`relationships.yml`/`views/`/`cubes/` 检查、无连接校验（`WrenEngine` 每查询重建，连接器懒建——profile 配错首错出现在查询时而非 `from_project`）。契约刻意最小：**`wren_project.yml` 存在 + `target/mdl.json` 存在 + profile 可解析**，其余延后。

### 4.10 示例脚本的自动化初始化

`entrypoint.sh`（容器）与 `start_server.bat`（Windows）实现同一套自动流水线，消费 `.env.example` 55–68 与 `docker-compose.yaml` 48–57 的环境变量：

| 变量 | 默认 | 用途 |
|---|---|---|
| `ACTIVE_PROFILE` | `default` | 要创建/激活的 profile 名 |
| `DATASOURCE` | 无 | 数据源类型；未设则跳过 profile 生成 |
| `DB_HOST/PORT/NAME/USER/PASSWORD` | 无 | 写入 `profiles.yml` 的 `host/port/database/user/password` |
| `SSL_MODE` | 无 | 写入 `ssl_mode` |
| `EXTRA_PROFILE_KEYS` | 无 | JSON 对象，附加 YAML 键 |

**`entrypoint.sh` 八步**（行号见 5.4）：① 建 `/project`；② `WREN_HOME=/project/.wren`、`chmod 700`（使 profile 项目本地）；③ `auto_generate_profile`（86–125）按上方字段产 `profiles.yml`（仅缺失/空时生成；已存在则按需追加并改 `active:`，160），`chmod 600`；④ 缺 `wren_project.yml` 时 `wren context init --empty`（176），再用 Python 把 `profile:`+`data_source:` 写入 `wren_project.yml`（182–211，镜像 `set-profile` 但无校验/告警）；⑤ `models/` 非空则每次重启都 `wren context build`（216–229，使模型编辑即时生效）；⑥ `target/mdl.json` 存在且装了 memory extra 则 `wren memory index --path .wren/memory --mdl target/mdl.json --no-seed`（231–251）；⑧ `exec "$@"` 交棒 CMD。

**`start_server.bat` 十步**：镜像同名步骤，但两点差异——① 项目建在 `examples/wren_project/`（line 216）而非 `/project`；⑤ `wren context init --empty`（269–284）**不绑定** `profile`/`data_source` 到 `wren_project.yml`（与 entrypoint 的 step 4 不同），故 Windows 上 `from_project` 会穿透到全局 active profile（4.6 第三层），除非用户手动 `wren context set-profile`——这是 notable divergence。

**docker-compose**：`PROJECT_PATH=/project`（46）为容器内项目路径；`./wren_project:/project` 卷挂载（61）使项目持久化到主机 `examples/wren_project/`；全部 `DB_*`/`DATASOURCE`/`ACTIVE_PROFILE`/`SSL_MODE`/`EXTRA_PROFILE_KEYS` 转发。

---

## 5. 端到端请求流程

### 5.1 部署/初始化链

```
Dockerfile（两阶段）
  Stage1 node: 构建 wren-chat-ui → dist/index.html
  Stage2 python: 装 core/wren[all] + wren-langchain(sed 's/wren-engine/wrenai/g')
                 + requirements.txt → EXPOSE 8201 8202
                 ENTRYPOINT entrypoint.sh  CMD start_dual_services.py
        │
        ▼
entrypoint.sh（容器启动，8 步）
  ① 建 /project  ② WREN_HOME=/project/.wren  ③ flock 并发锁
  ④ 由 DB_* env 生成 profiles.yml  ⑤ wren context init --empty + 绑定 profile
  ⑥ wren context build（models/*.mdl → target/mdl.json）  ⑦ wren memory index
  ⑧ 打印配置摘要 → exec "$@"（交棒 CMD）
        │
        ▼
start_dual_services.py（双进程 + 看门狗）
  Track A: python wren_mcp_server.py      (8202，/sse + /mcp 双传输，崩溃自动重启)
  Track B: python langgraph_fastapi_multi.py (8201，前台；其退出则整体退出)
```

### 5.2 原生有状态生命周期（`POST /chat`、`POST /chat/stream`）

```
Client(React UI / curl)
  │ POST {question, session_id?, model_name}
  ▼
lazy_init_app(model_name)  → 确保 toolkit / langgraph_app 就绪（1661）
actual_session_id = session_id or uuid4()
config = {"configurable": {"thread_id": actual_session_id, [stream_queue=...]}}
  │
  ▼ 归一化后──┐
   (NDJSON) 一行/节点更新，前端 parseNDJSONStream 实时渲染 AIMessage(tool_calls) + ToolMessage
  │
  ▼
持久化 checkpoint（按 thread_id）
```

要点：`session_id` 即 LangGraph `thread_id`；checkpointer 自动合并历史与新 `HumanMessage`——这是多轮机制；`/chat` 返回整段序列化历史 + `session_id`，`/chat/stream` 每个节点更新序列化为一行 NDJSON。

### 5.3 OpenAI 兼容生命周期（`POST /v1/chat/completions`）

```
OpenAI 协议客户端(ALS/DEEIX-Chat/curl)
  │ POST {model=wren-agent, messages[], stream?}
  ▼
lazy_init_app(model_name=request.model)
convert_openai_messages(messages) → LangChain 消息
completion_id/created_ts；request_id_var.set(completion_id)
  │
  ├─ 非流式 ─► langgraph_app_stateless.ainvoke(graph_input)
  │            → 取末条 AIMessage.content + 聚合 reasoning_traces
  │            → 单条 chat.completion + 字符数/4 估算 usage
  │
  └─ 流式   ─► StreamingResponse(event_stream_generator, text/event-stream)
               ① 初始 assistant chunk
               ② asyncio.Queue + 独立 astream 任务(astream, stream_mode=updates)
               ③ 循环：keepalive 超时→心跳；graph项→发reasoning/content delta；
                       progress项(MCP callback)→进度trace；error→错误chunk；end→break
               ④ finish_reason=stop + data: [DONE]
```

此端点**无状态**——靠客户端每轮重发完整 `messages[]`，用 `langgraph_app_stateless`，不碰 checkpointer。`model=wren-agent` 等是虚拟 id（`OPENAI_EXPOSED_MODELS`，默认 `wren-agent,wren-semantic-analyst`），非底层 LLM id。

### 5.4 工具调用子流程

```
agent_node: ChatOpenAI.bind_tools(tools).invoke(messages)
   │  产出 AIMessage(含 tool_calls?)
   ▼
should_continue ──无──► END
   │ 有
   ▼
ToolNode 分发每个调用到对应 StructuredTool
   ├─ wren_query(sql,limit)
   │     → toolkit.query(sql,limit)
   │     → 新建 WrenEngine（重读 manifest）
   │     → 经缓存的连接器查目标库 → pyarrow.Table
   │     → format_query_content → envelope{columns,rows,row_count,...}
   ├─ wren_dry_plan / wren_list_models / get_current_time
   └─ MCP 工具 _acall:
         get_or_create_mcp_session（→管理队列串行）
         session.call_tool(name, kwargs, progress_callback=...)
            │ progress_callback 把 {"type":"progress",...} 回注 per-request stream_queue
            │ （仅在 /v1 流式与 /chat/stream 被消费为 reasoning/进度 trace）
         失败→强制重连→重试一次
   │
   ▼ ToolMessage 追加到状态
回到 agent_node（下一轮 LLM）……直到无 tool_calls → END
```

记忆工具的 recall/fetch/store 循环发生在 `agent_node` 的决策内，由 `system_prompt()` 的工作流规则引导（默认"先 recall、先 store"）。

### 5.5 多轮与多会话

- **多轮**：`thread_id` 键控的 checkpointer 自动加载/合并历史；同一 `session_id` 多次调用即连续对话。
- **多会话**：不同 `session_id` 互相隔离；`GET /chat/history/{session_id}` 用 `langgraph_app.aget_state(config)` 读 checkpoint 返回历史。
- **记忆层**：LanceDB 向量记忆（`.wren/memory/`）跨会话共享 NL→SQL 对，受记忆工具调用驱动，与 checkpointer 是两套独立持久化。

---

## 6. 部署拓扑

### 6.1 Docker 两阶段构建（`Dockerfile`）

- **Stage 1**（`node:20-slim`）：克隆 `CodeFindc/WrenAI` dev 分支（经 GitHub 代理），`npm ci && npm run build` 编译 `wren-chat-ui`，剥离 `node_modules`。
- **Stage 2**（`python:3.14`）：拷入构建产物；装 `core/wren[all]`，再装 `wren-langchain`（`pyproject.toml` 内 `sed 's/wren-engine/wrenai/g'`）；装 `requirements.txt`；`chmod +x entrypoint.sh`。`EXPOSE 8201 8202`，`ENTRYPOINT ["./entrypoint.sh"]`，`CMD ["python3","start_dual_services.py"]`。

### 6.2 `Dockerfile.cn`（国内加速变体）

逻辑相同，但配置腾讯云 APT 镜像、`npmmirror` registry、腾讯/阿里 PyPI 镜像、`PIP_PREFER_BINARY=true`，并配 GitHub 代理双回退（`cdn.gh-proxy.org`→`mirror.ghproxy.com`）。

### 6.3 docker-compose

`docker-compose.yaml`：单服务 `wren-langgraph-api`，build context 为仓库根，**单容器双 track**。端口 `8201:8201`、`8202:8202`。卷 `./wren_project:/project`（主机挂载，持久化 models/MDL/profiles）。`restart: unless-stopped`。关键 env 含双轨端口、embedding（`WREN_EMBEDDING_PROVIDER=openai`、`WREN_EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B`）、上游 LLM（`OPENAI_API_KEY`、`LLM_MODEL_NAME=gpt-4o`）、OpenAI 适配虚拟模型、`OPENAI_PROCESS_STREAM_MODE=reasoning`、SSE keepalive 15s、`CHAT_HISTORY_DB_URI`（MySQL checkpointer）、`PROJECT_PATH=/project`、完整 DB 连接块；`STREAM=1` 硬编码。

`docker-compose.cn.yaml`：同上但引用 `Dockerfile.cn` 并注入 PIP 镜像 env。

### 6.4 本地 Windows：`start_server.bat`

10 步批处理，做 `entrypoint.sh` 的全部工作 **再加**依赖安装：探测根目录→把 Python `Scripts/` 加 PATH（使 `wren` CLI 可用）→修复中断的 `pyproject.toml` 备份→装 `core/wren[all]` 与 `wren-langchain`（含 `wren-engine`→`wrenai` 替换）→若 `dist/index.html` 缺失则 `npm install`+`npm run build`→加载 `.env`（敏感值掩码）→建 `wren_project/` 与 `WREN_HOME`→由 env 自动生成 `profiles.yml`→`wren context init --empty`/`build`/`memory index`→后台起 Track A、前台起 Track B。

### 6.5 `start_dual_services.py`

并发起两子进程；`SIGINT`/`SIGTERM` 处理（28–39）；看门狗循环（42–50）——Track B 死则整体退出，Track A 崩则自动重启。是容器 `CMD`。

---

## 7. 配置参考

`.env.example` 分组（行号见原文件）：

| 分组 | 关键变量 | 说明 |
|---|---|---|
| Embedding | `WREN_EMBEDDING_PROVIDER=openai`、`WREN_EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B`、`EM_OPENAI_BASE_URL/API_KEY` | 记忆层 embedding |
| OpenAI LLM | `OPENAI_API_KEY`、`OPENAI_API_BASE` | 上游模型密钥 |
| LLM override | `LLM_API_BASE/API_KEY/MODEL_NAME=gpt-4o` | 优先于 OpenAI_* 的一组通用覆盖 |
| OpenAI adapter / thinking stream | `OPENAI_EXPOSED_MODELS=wren-agent,wren-semantic-analyst`、`OPENAI_PROCESS_STREAM_MODE=reasoning`、`OPENAI_PROCESS_MAX_TOOL_CHARS=400`、`OPENAI_SSE_KEEPALIVE_SECONDS=15`、`OPENAI_SSE_KEEPALIVE_STYLE=comment` | Track B 适配器行为 |
| 诊断 | `LOG_LEVEL=INFO` | `wren` logger 级别 |
| 持久化历史 | `CHAT_HISTORY_DB_URI` | MySQL URI→持久 checkpointer；空→`MemorySaver` |
| 数据源 profile | `ACTIVE_PROFILE`、`DATASOURCE=mysql`、`DB_HOST/PORT/NAME/USER/PASSWORD`、`SSL_MODE`、`EXTRA_PROFILE_KEYS`(JSON) | 由 entrypoint/bat 生成 `profiles.yml` |
| FunAI | `FUNAI_SKILLS_PATH`、`FUNAI_SESSIONS_PATH` | 技能/会话路径 |
| MCP 扩展 | `MCP_CONFIG_DIR` | 外部 MCP 服务器 JSON 定义目录 |

**关键运行时 env**（直接被 `langgraph_fastapi_multi.py` 读取）：`PROJECT_PATH`、`PORT`、`CHAT_HISTORY_DB_URI`、`MCP_CONFIG_DIR`、`MCP_CONNECT_TIMEOUT`/`MCP_READ_TIMEOUT`/`MCP_RETRY_INTERVAL`、`LLM_*`/`OPENAI_API_BASE`/`OPENAI_API_KEY`、`OPENAI_PROCESS_STREAM_MODE`/`OPENAI_PROCESS_MAX_TOOL_CHARS`、`OPENAI_SSE_KEEPALIVE_*`、`OPENAI_EXPOSED_MODELS`、`LOG_LEVEL`、`STREAM`（demo 用）。

**Profile 三层解析回退**（库 `_providers/connection.py`）：显式 `from_project(path, profile=)` → `wren_project.yml: profile:` → 全局 active profile（`~/.wren/profiles.yml`）；`${ENV_VAR}` 经 Core 的 `expand_profile_secrets` 展开。详见 §4.4–§4.6。

---

## 8. 可观测性、弹性与已知约束

### 8.1 结构化日志

- `request_id_var: ContextVar[str]`（67）承载每请求 id；`StructuredRequestFormatter`（69–73）注入 `[req=<id>]`；`setup_logging()`（75–93）配 `wren` stdout logger，级别随 `LOG_LEVEL`。
- 子模块 logger（97–107）：`wren.lifespan`/`mysql`/`mcp`/`llm`/`tool`/`api`/`sse`/`chat`。
- **缺口**：OpenAI 端点在 1374 行设 `request_id`，但原生 `/chat` 系列未设——跨原生端点的日志会缺 `req_id` 关联。

### 8.2 弹性模式

| 关注点 | 机制 | 行号 |
|---|---|---|
| LLM 调用失败 | `agent_node` 清缓存重试一次 | 524–548 |
| MCP 工具调用失败 | 强制重连 + 重试一次 | 380–421 |
| MCP 服务器启动失败 | `retry_failed_mcp_connections_loop` 每 30s 轮询；成功后失效图缓存 | 576–623 |
| MCP 跨任务开/关 AsyncExitStack | `mcp_manager_worker` 单任务串行化（规避 anyio cancel-scope） | 236–278 |
| MySQL 断连 | `ReconnectingPyMySQLSaver` 每次 op 前 ping + 重连 | 629–683 |
| MySQL setup 卡住 | 工作线程限时 10s，失败回退 `MemorySaver` | 881–895 |
| Windows localhost 解析挂起 | URI 内 `localhost`→`127.0.0.1` | 847–869 |
| SSE 空闲断连 | keepalive（默认 15s，comment 或 empty_delta） | 1284–1296 |

**已知局限（如实标注）**：MCP 后发现工具经 `retry_failed_mcp_connections_loop` 失效了 `langgraph_app`/`langgraph_app_stateless` 缓存，但**不失效** `model_with_tools` 的 `bind_tools` 缓存——即已编译图不会因此给 LLM 重绑新工具。

### 8.3 安全说明

**本服务自身无鉴权、CORS 全开**（`allow_origins=["*"]`）。`/v1/chat/completions` 忽略 `Authorization`，任何调用方都能触发昂贵 LLM。唯一的"密钥"处理是**出站脱敏** `sanitize_and_truncate_text`（1298–1309），防止上游 tool/DB 凭据经 reasoning trace 回流。**部署时须置于反代/网关之后**由其加鉴权，不可直接暴露。

### 8.4 其他已知约束

- 工具为同步，故 `from_project` 等经 `asyncio.to_thread` 包裹。
- 一个 agent 绑一个 toolkit（库 v0.1 限制）。
- 记忆由 `.wren/memory/` 自动探测，无 override kwarg；profile 不能热重载；勿并发跑 `wren memory index`。

---

## 9. 关键文件与行号索引

### 9.1 `langgraph_fastapi_multi.py`

| 类/函数 | 行号 | 职责 |
|---|---|---|
| `setup_logging` / 子 logger | 75–107 | 结构化日志 |
| `_real_get_or_create_mcp_session` | 155–233 | MCP 会话建立（stdio/SSE/streamable-http） |
| `mcp_manager_worker` | 236–278 | MCP 连接操作单任务串行化 |
| `json_schema_to_pydantic` | 288–316 | JSON Schema → Pydantic args_schema |
| `load_mcp_configs` | 319–340 | 合并 `MCP_CONFIG_DIR/*.json` |
| `convert_mcp_to_langchain` | 343–443 | MCP 工具 → LangChain StructuredTool |
| `AgentState` | 448–449 | 图状态 schema |
| `get_current_time` | 452–459 | 本地 `@tool` |
| `ensure_wren_system_prompt` | 462–480 | 幂等注入系统提示词 |
| `build_app` | 483–565 | 编译 ReAct 图（含 `get_model`/`agent_node`/`should_continue`） |
| `retry_failed_mcp_connections_loop` | 576–623 | MCP 重连轮询 + 图缓存失效 |
| `ReconnectingPyMySQLSaver` | 629–769 | 自重连 MySQL checkpointer |
| `lifespan` | 779–941 | 启动/关闭生命周期 |
| FastAPI app + CORS | 945–961 | 应用骨架 |
| `ChatRequestMulti` / OpenAI Pydantic 模型 | 966–991 | 请求模型 |
| `serialize_message` | 998–1022 | 消息序列化 |
| `get_local_or_cdn` | 1030–1054 | 离线静态资源 |
| `serve_chat_ui` / 状态面板 | 1057–1124 | `GET /` |
| `/docs` + `/static/*` | 1127–1174 | 离线 Swagger |
| `/health` | 1179–1216 | 健康检查 |
| `/v1/models` | 1228–1246 | OpenAI 模型发现 |
| `convert_openai_messages` | 1249–1267 | OpenAI→LangChain 消息 |
| OpenAI 适配助手（mode/keepalive/sanitize/format/make_chunk） | 1270–1357 | SSE 格式化 |
| `/v1/chat/completions` | 1360–1563 | OpenAI 兼容端点 |
| `lazy_init_app` | 1568–1623 | 兜底初始化 |
| `/chat` | 1626–1655 | 原生有状态（非流） |
| `/chat/stream` (`event_generator`) | 1658–1713 | 原生有状态 NDJSON 流 |
| `/chat/history/{session_id}` | 1716–1731 | 读历史 |
| `__main__` | 1734–1737 | 入口（默认 8201） |

### 9.2 `wren_mcp_server.py`

| 项 | 说明 |
|---|---|
| toolkit 延迟实例化 | `get_toolkit()` / `_get_tools_by_name()` |
| `_ainvoke_tool` | 按名转发到 `toolkit.get_tools()`，返回 envelope JSON |
| `wren_query` / `wren_dry_plan` / `wren_list_models` | 运行时工具（与 SDK 同名同参） |
| `wren_fetch_context` / `wren_recall_queries` / `wren_store_query` | 记忆工具（memory 关闭时明确报错） |
| `wren_get_system_prompt` | 元工具：返回 `toolkit.system_prompt()` |
| `_build_dual_app()` / `app` | 合并 SSE + Streamable HTTP 路由；`uvicorn.run(app)` |

### 9.3 库侧 `wren-langchain`（`src/wren_langchain/`）

| 项 | 文件 |
|---|---|
| `WrenToolkit.from_project` / 三 provider 装配 / `_build_engine` | `_toolkit.py` |
| 运行时工具 `wren_query`/`wren_dry_plan`/`wren_list_models` | `_tools.py` |
| 记忆工具 `wren_fetch_context`/`wren_recall_queries`/`wren_store_query` | `_tools_memory.py` |
| `build_system_prompt`（工具驱动工作流） | `_prompt.py` |
| envelope `make_success`/`make_error` + `format_error`(phase) | `_envelope.py` |
| `format_*`（content 渲染/截断/secret 脱敏） | `_format.py` |
| `ProfileConnectionProvider`（三层解析） | `_providers/connection.py` |
| `ProjectMDLSource` | `_providers/mdl_source.py` |
| `LocalLanceDBMemoryProvider` / `NoopMemoryProvider` | `_providers/memory.py` |
| `_MemoryAPI` | `_memory_api.py` |
| `WrenToolkitInitError` / `MemoryNotEnabledError` | `exceptions.py` |

### 9.5 项目/数据源初始化（`core/wren`）

| 项 | 文件 | 关键行号 |
|---|---|---|
| `wren context init` / `--empty` 脚手架 + `wren_project.yml` 模板 | `src/wren/context_cli.py` | 152–386, 274–286 |
| `wren context build` → `target/mdl.json` | 同上 / `src/wren/context.py` | 582–663 / 678–730 |
| `wren context validate` | `context_cli.py` | 395–545 |
| `set-profile`（写 `profile`+`data_source`） | `context_cli.py` | 779–871 |
| `wren_project.yml` 字段顺序 `_PROJECT_FIELD_ORDER` | `context.py` | 402–410 |
| schema_version 与布局分派 `_LAYOUT_VERSION_MAP` | `context.py` | 435, 438, 517 |
| `profiles.yml` 位置 `_PROFILES_FILE` / `_load_raw` | `src/wren/profile.py` | 20–21, 143–177 |
| `${VAR}` 展开 `expand_profile_secrets` / `.env` 发现 `_ensure_env_loaded` | `profile.py` | 29–34, 99–140, 44–91 |
| `DataSource` 枚举 | `model/data_source.py` | 40–60 |
| 连接器工厂 + extras `get_connector` | `connector/factory.py` | 6–37 |
| per-source 字段 `DATASOURCE_MODELS` | `model/field_registry.py` | 43–70 |
| `WrenEngine` 连接器懒建 `_get_connector` | `engine.py` | 55–81, 237–240 |
| `wren memory index` CLI | `memory/cli.py` | 150–241 |
| `MemoryStore`（LanceDB）/ 索引 `index_schema` | `memory/store.py` | 73–160 |
| `extract_schema_items`（索引内容） | `memory/schema_indexer.py` | 220–259 |
| embedding 选择 `get_embedding_function` | `memory/embeddings.py` | 12–59 |

### 9.4 路由总表（Track B）

| Method | Path | 函数 | 行号 | 用途 |
|---|---|---|---|---|
| GET | `/` | `serve_chat_ui` | 1057 | UI 或状态面板 |
| GET | `/docs` | `custom_swagger_ui_html` | 1127 | Swagger |
| GET | `/static/*` | `get_swagger_js/css/favicon` | 1139–1174 | 离线静态 |
| GET | `/health` | `health_check` | 1179 | 探针 |
| GET | `/v1/models` | `list_openai_models` | 1228 | OpenAI 模型列表 |
| POST | `/v1/chat/completions` | `openai_chat_completions` | 1360 | OpenAI 兼容（流/非流） |
| POST | `/chat` | `chat_endpoint` | 1626 | 原生非流 |
| POST | `/chat/stream` | `chat_stream_endpoint` | 1658 | 原生 NDJSON 流 |
| GET | `/chat/history/{session_id}` | `get_session_history` | 1716 | 读历史 |

---

## 10. 术语表

- **MDL**（Modeling Definition Language）：Wren 的语义建模清单，`target/mdl.json`；`wren context build` 由 `models/*.mdl` 编译产出。
- **WrenToolkit**：`wren-langchain` 的唯一公开 facade，封装 MDL/profile/memory/引擎装配与 LangChain 工具适配。
- **checkpointer / thread_id**：LangGraph 的状态持久化机制与键；本示例中 `thread_id = session_id`，键控多轮/多会话。
- **ReAct**：Reason+Act 循环——LLM 决策调工具→执行→回喂→再决策，至无工具调用结束。
- **envelope**：工具返回的统一 JSON 结构（`ok/content/data/warnings` 或 `error.{code,phase,message,metadata}`），是工具↔agent 间的契约。
- **Track A / Track B**：两条对外轨道——A 为 MCP 双传输（8202：`/sse` + `/mcp`），B 为 OpenAI 兼容 + 原生会话 + UI（8201），收敛到同一语义层。
- **reasoning trace**：OpenAI SSE 中以 `delta.reasoning_content` 携带的"思考过程"，含工具调用起止/结果/进度的文本化呈现，由 `OPENAI_PROCESS_STREAM_MODE` 控制。
- **NDJSON**：Newline-Delimited JSON，`/chat/stream` 的传输格式，一行一个节点更新 JSON。
- **single-file build**：`vite-plugin-singlefile` 把 React 应用编译进单个自包含 `dist/index.html`，无运行时外链，支持离线/气隙。
- **MCP**（Model Context Protocol）：工具暴露协议；本示例既是 MCP 客户端（Track B 消费外部 MCP 工具）又经 Track A 作为 MCP 服务器暴露能力。
