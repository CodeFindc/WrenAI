"""Launcher script to run both Track A (FastMCP SSE Server) and Track B (OpenAI Proxy & FastAPI Server) concurrently."""

import os
import sys
import subprocess
import signal
import time


def check_mcp_installed() -> bool:
    """Check if mcp package with FastMCP support is available in current Python environment."""
    try:
        from mcp.server.fastmcp import FastMCP  # noqa: F401

        return True
    except ImportError:
        try:
            from fastmcp import FastMCP  # noqa: F401

            return True
        except ImportError:
            return False


def main():
    print("==================================================")
    print("  Starting WrenAI Dual-Track Services (Container) ")
    print("==================================================")
    print("  Track A (MCP dual transport):")
    print("    SSE:             http://0.0.0.0:8202/sse")
    print("    Streamable HTTP: http://0.0.0.0:8202/mcp  (DEEIX)")
    print("  Track B (OpenAI Proxy): http://0.0.0.0:8201/v1")
    print("==================================================")

    mcp_enabled = check_mcp_installed()
    mcp_proc = None

    if mcp_enabled:
        # Launch FastMCP dual-transport server (Track A: SSE + Streamable HTTP)
        mcp_env = os.environ.copy()
        mcp_env["PORT"] = os.environ.get("MCP_PORT", "8202")
        mcp_env["HOST"] = os.environ.get("MCP_HOST", "0.0.0.0")
        print("[INFO] Launching Track A FastMCP dual-transport server on port 8202...")
        mcp_proc = subprocess.Popen([sys.executable, "wren_mcp_server.py"], env=mcp_env)
    else:
        print(
            "[NOTICE] Track A (FastMCP Server) skipped: 'mcp>=1.2.0' package is not installed.\n"
            "         Track B (OpenAI Proxy & FastAPI API) will continue running on port 8201.\n"
            "         To enable Track A, run: pip install 'mcp>=1.2.0'",
            flush=True,
        )

    # Launch LangGraph FastAPI & OpenAI Proxy server (Track B)
    api_env = os.environ.copy()
    api_env["PORT"] = os.environ.get("PORT", "8201")
    print("[INFO] Launching Track B FastAPI & OpenAI Proxy server on port 8201...")
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
    MAX_MCP_RESTARTS = 5

    try:
        while True:
            time.sleep(2)
            if api_proc.poll() is not None:
                print(f"[ERROR] API server process exited with code {api_proc.returncode}")
                if mcp_proc and mcp_proc.poll() is None:
                    mcp_proc.terminate()
                sys.exit(api_proc.returncode)

            if mcp_enabled and mcp_proc and mcp_proc.poll() is not None:
                mcp_crash_count += 1
                return_code = mcp_proc.returncode
                print(
                    f"[WARNING] FastMCP server process exited with code {return_code}. (Crash #{mcp_crash_count}/{MAX_MCP_RESTARTS})",
                    flush=True,
                )

                if mcp_crash_count >= MAX_MCP_RESTARTS:
                    print(
                        f"[ERROR] FastMCP server (wren_mcp_server.py) repeatedly exited with code {return_code} after {MAX_MCP_RESTARTS} attempts.\n"
                        f"Stopping Track A auto-restart loop to prevent CPU saturation. Track B API server remains active.\n"
                        f"Common causes:\n"
                        f"  1. Port {os.environ.get('MCP_PORT', '8202')} is already in use by another process.\n"
                        f"  2. Direct exception during uvicorn startup (run: python wren_mcp_server.py to debug).",
                        flush=True,
                    )
                    mcp_enabled = False  # Stop retrying FastMCP
                else:
                    time.sleep(3)
                    print("[INFO] Restarting FastMCP server process...", flush=True)
                    mcp_proc = subprocess.Popen([sys.executable, "wren_mcp_server.py"], env=mcp_env)
    except KeyboardInterrupt:
        handle_signal(None, None)


if __name__ == "__main__":
    main()
