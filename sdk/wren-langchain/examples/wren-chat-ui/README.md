# wren-chat-ui

Offline single-page chat UI for the WrenAI dual-track example. It talks
directly to Track B (`langgraph_fastapi_multi.py`, port 8201) — **not** the
OpenAI `/v1` adapter, but the native NDJSON streaming endpoints.

## What it does

- Multi-session chat sidebar (create / rename / delete), persisted in
  `localStorage` and synced with the server via `GET /chat/history/{id}`.
- Streams assistant replies from `POST /chat/stream` (NDJSON, one JSON
  object per line). Each chunk carries `agent` / `tools` message deltas or
  a `progress` event — rendered as a live tool-progress indicator.
- Settings modal to override the backend URL. Defaults to
  `window.location.origin`, so when the FastAPI server serves the built UI
  at `http://<host>:8201/` the UI calls back to that same origin — no
  configuration needed for the common case.
- Runs fully offline once built: `vite-plugin-singlefile` inlines all JS
  and CSS into a single `dist/index.html`, so there is no `dist/assets/`
  to mount and no CDN dependency.

## Build

```bash
cd wren-chat-ui
npm install
npm run build      # tsc -b && vite build → dist/index.html (self-contained)
```

`dist/` is gitignored. The FastAPI server's `GET /` route reads
`dist/index.html` and returns it; if `dist/` has not been built it falls
back to the built-in status dashboard. So after `npm run build`, the chat
UI is served at `http://<host>:8201/` automatically.

## Dev server

```bash
npm run dev        # Vite dev server with HMR (default http://localhost:5173)
```

In dev mode the UI runs on a different origin than the API, so open
**Settings** (sidebar) and set the backend URL to `http://<host>:8201`.
Make sure CORS is allowed for the dev origin on the Track B server.

## Backend contract

| Endpoint | Method | Purpose |
|---|---|---|
| `/chat/stream` | POST | NDJSON stream of `{agent, tools, progress, error}` chunks |
| `/chat/history/{session_id}` | GET | Full message history for a session |

Request body for `/chat/stream`:
```json
{ "question": "...", "session_id": "<uuid>", "model_name": "gpt-4o" }
```

`src/utils/stream.ts` parses the NDJSON stream; the chunk shape is defined
by the `StreamUpdate` interface there.

## Project layout

```
wren-chat-ui/
├── index.html
├── vite.config.ts          # singlefile + base: './' → one-file build
├── src/
│   ├── main.tsx
│   ├── App.tsx             # session state, streaming, localStorage sync
│   ├── components/
│   │   ├── Sidebar.tsx          # session list (create/rename/delete)
│   │   ├── MessageList.tsx      # renders Human/AI/Tool messages + progress
│   │   ├── ChatInput.tsx
│   │   └── SettingsModal.tsx    # backend URL override + connection test
│   └── utils/stream.ts          # NDJSON stream parser + StreamUpdate types
└── public/                 # favicon, icons
```

## Tech stack

React 19 + TypeScript + Vite 8 + Tailwind CSS 4 + lucide-react icons.
ESLint config in `eslint.config.js`. TypeScript paths in
`tsconfig.app.json` / `tsconfig.node.json`.
