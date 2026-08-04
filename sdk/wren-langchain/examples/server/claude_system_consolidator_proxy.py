"""
claude_system_consolidator_proxy.py — Lightweight proxy for Claude CLI.

Consolidates multiple Anthropic / OpenAI system messages into a single system message at index 0
to prevent vLLM / Qwen Jinja2 template errors ("System message must be at the beginning.").
"""

import os
import sys
import json
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler

UPSTREAM_BASE_URL = os.environ.get("REAL_ANTHROPIC_BASE_URL", "http://192.168.110.209:8200/v1").rstrip("/")
LISTEN_PORT = int(os.environ.get("CONSOLIDATOR_PROXY_PORT", "8080"))


def consolidate_payload(data: dict) -> dict:
    """Consolidate all system instructions into a single system field or message[0]."""
    if not isinstance(data, dict):
        return data

    system_parts = []

    # 1. Extract Anthropic top-level `system` field
    if "system" in data:
        sys_val = data["system"]
        if isinstance(sys_val, str) and sys_val.strip():
            system_parts.append(sys_val.strip())
        elif isinstance(sys_val, list):
            for part in sys_val:
                if isinstance(part, str) and part.strip():
                    system_parts.append(part.strip())
                elif isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                    system_parts.append(part["text"].strip())

    # 2. Extract any `role == "system"` from `messages` array
    new_messages = []
    if "messages" in data and isinstance(data["messages"], list):
        for msg in data["messages"]:
            if isinstance(msg, dict):
                role = msg.get("role")
                content = msg.get("content")
                if role == "system":
                    if isinstance(content, str) and content.strip():
                        system_parts.append(content.strip())
                    elif isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict) and c.get("text"):
                                system_parts.append(c["text"].strip())
                            elif isinstance(c, str):
                                system_parts.append(c.strip())
                else:
                    new_messages.append(msg)
        data["messages"] = new_messages

    # 3. Re-assign consolidated system content
    if system_parts:
        data["system"] = "\n\n---\n\n".join(system_parts)

    return data


class ConsolidatorProxyHandler(BaseHTTPRequestHandler):
    def _forward_request(self, method: str):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""

        target_url = f"{UPSTREAM_BASE_URL}{self.path}"

        if body and method == "POST":
            try:
                payload = json.loads(body.decode("utf-8"))
                consolidated = consolidate_payload(payload)
                body = json.dumps(consolidated).encode("utf-8")
            except Exception:
                pass

        req = urllib.request.Request(target_url, data=body if body else None, method=method)

        for header, value in self.headers.items():
            if header.lower() not in ("host", "content-length"):
                req.add_header(header, value)
        if body:
            req.add_header("Content-Length", str(len(body)))

        try:
            with urllib.request.urlopen(req) as resp:
                self.send_response(resp.status)
                for h, v in resp.headers.items():
                    if h.lower() not in ("transfer-encoding", "content-length"):
                        self.send_header(h, v)
                resp_body = resp.read()
                self.send_header("Content-Length", str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body)
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            for h, v in e.headers.items():
                if h.lower() not in ("transfer-encoding", "content-length"):
                    self.send_header(h, v)
            err_body = e.read()
            self.send_header("Content-Length", str(len(err_body)))
            self.end_headers()
            self.wfile.write(err_body)
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f"Proxy Error: {e}".encode("utf-8"))

    def do_POST(self):
        self._forward_request("POST")

    def do_GET(self):
        self._forward_request("GET")

    def log_message(self, format, *args):
        pass


def main():
    server = HTTPServer(("0.0.0.0", LISTEN_PORT), ConsolidatorProxyHandler)
    print(f"[ConsolidatorProxy] Listening on 0.0.0.0:{LISTEN_PORT} -> {UPSTREAM_BASE_URL}")
    server.serve_forever()


if __name__ == "__main__":
    main()
