"""Launcher script to run both Track A (FastMCP SSE Server) and Track B (OpenAI Proxy & FastAPI Server) concurrently."""

import os
import sys
import subprocess
import signal
import time

def main():
    print("==================================================")
    print("  Starting WrenAI Dual-Track Services (Container) ")
    print("==================================================")
    print("  Track A (MCP dual transport):")
    print("    SSE:             http://0.0.0.0:8202/sse")
    print("    Streamable HTTP: http://0.0.0.0:8202/mcp  (DEEIX)")
    print("  Track B (OpenAI Proxy): http://0.0.0.0:8201/v1")
    print("==================================================")

    # Launch FastMCP dual-transport server (Track A: SSE + Streamable HTTP)
    mcp_env = os.environ.copy()
    mcp_env["PORT"] = os.environ.get("MCP_PORT", "8202")
    mcp_env["HOST"] = os.environ.get("MCP_HOST", "0.0.0.0")
    mcp_proc = subprocess.Popen([sys.executable, "wren_mcp_server.py"], env=mcp_env)

    # Launch LangGraph FastAPI & OpenAI Proxy server (Track B)
    api_env = os.environ.copy()
    api_env["PORT"] = os.environ.get("PORT", "8201")
    api_proc = subprocess.Popen([sys.executable, "langgraph_fastapi_multi.py"], env=api_env)

    def handle_signal(sig, frame):
        print("\nShutting down dual-track services...")
        for proc in (mcp_proc, api_proc):
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    mcp_crash_count = 0
    try:
        while True:
            time.sleep(2)
            if api_proc.poll() is not None:
                print(f"[ERROR] API server process exited with code {api_proc.returncode}")
                mcp_proc.terminate()
                sys.exit(api_proc.returncode)
            if mcp_proc.poll() is not None:
                mcp_crash_count += 1
                print(f"[WARNING] FastMCP server process exited with code {mcp_proc.returncode}. (Crash #{mcp_crash_count})", flush=True)
                if mcp_crash_count >= 5:
                    print(
                        f"[ERROR] FastMCP server (wren_mcp_server.py) repeatedly exited with code {mcp_proc.returncode}.\n"
                        f"Common causes:\n"
                        f"  1. Port {mcp_env.get('PORT', '8202')} is already in use by another process.\n"
                        f"  2. Direct exception during uvicorn startup (check traceback above).\n"
                        f"To debug directly, run: python wren_mcp_server.py",
                        flush=True
                    )
                time.sleep(3)
                print("[INFO] Restarting FastMCP server process...", flush=True)
                mcp_proc = subprocess.Popen([sys.executable, "wren_mcp_server.py"], env=mcp_env)
    except KeyboardInterrupt:
        handle_signal(None, None)

if __name__ == "__main__":
    main()
