# MCP Server Config Samples

Track B (`langgraph_fastapi_multi.py`) can call **external** MCP servers in
addition to its own Wren tools. Point `MCP_CONFIG_DIR` (default
`./mcp_configs`) at a directory of JSON files; each file is either a flat
`{ "<server_name>": { ... } }` object or a Claude-Desktop-style
`{ "mcpServers": { "<server_name>": { ... } } }` wrapper. Multiple files are
merged.

Two transport kinds are supported (see `_real_get_or_create_mcp_session` in
`langgraph_fastapi_multi.py`):

| Kind | Required keys | Notes |
|---|---|---|
| Streamable HTTP | `url`, optional `headers`, optional `type` | `type` defaults to `streamable_http` when the URL contains `/mcp`, otherwise `sse`. |
| SSE | `url`, optional `headers` | Set `type: "sse"` or omit and use a non-`/mcp` URL. |
| Stdio | `command`, optional `args`, optional `env` | Spawns a local subprocess. |

The samples here use the `.example.json` extension so they are not loaded by
default. To enable one, copy it to a `.json` name (e.g.
`stdio.example.json` → `stdio.json`) and edit the values.

> Do **not** point this at the local Track A server (`wren_mcp_server.py` on
> :8202) in the default deployment — that would double-apply the Wren
> workflow (Track B already calls `WrenToolkit` directly). Only wire Track A
> in here if you understand the self-reference.
