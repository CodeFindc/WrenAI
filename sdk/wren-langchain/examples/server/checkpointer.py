"""Checkpointer persistence logic (ReconnectingPyMySQLSaver & MemorySaver fallback)."""

from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from typing import Any
from langchain_core.runnables import RunnableConfig

try:
    from langgraph.checkpoint.mysql.pymysql import PyMySQLSaver

    class ReconnectingPyMySQLSaver(PyMySQLSaver):
        """Thread-safe and reconnecting PyMySQL Saver using threading.RLock to prevent reentrant deadlocks."""

        def __init__(self, *args: Any, conn_args: dict | None = None, **kwargs: Any):
            super().__init__(*args, **kwargs)
            self.conn_args = conn_args
            # Override parent's Lock with RLock to prevent reentrant deadlocks during super().setup() / super().get_tuple()
            self.lock = threading.RLock()

        def _ping_unlocked(self) -> None:
            try:
                if self.conn:
                    self.conn.ping()
            except Exception as e:
                print(f"Failed to ping/reconnect MySQL database: {e}. Attempting clean reconnection...")
                try:
                    try:
                        self.conn.close()
                    except Exception:
                        pass
                    import pymysql

                    if self.conn_args:
                        conn_kwargs = dict(self.conn_args)
                        conn_kwargs.setdefault("autocommit", True)
                        self.conn = pymysql.connect(**conn_kwargs)
                    else:
                        self.conn.connect()
                    print("Successfully re-established clean MySQL connection!")
                except Exception as conn_err:
                    print(f"Failed to force clean MySQL connection: {conn_err}")
                    raise conn_err

        def setup(self, *args: Any, **kwargs: Any) -> None:
            with self.lock:
                self._ping_unlocked()
                return super().setup(*args, **kwargs)

        def get_tuple(self, *args: Any, **kwargs: Any) -> Any:
            with self.lock:
                self._ping_unlocked()
                return super().get_tuple(*args, **kwargs)

        def list(self, *args: Any, **kwargs: Any) -> list:
            with self.lock:
                self._ping_unlocked()
                return list(super().list(*args, **kwargs))

        def put(self, *args: Any, **kwargs: Any) -> Any:
            with self.lock:
                self._ping_unlocked()
                return super().put(*args, **kwargs)

        def put_writes(self, *args: Any, **kwargs: Any) -> Any:
            with self.lock:
                self._ping_unlocked()
                return super().put_writes(*args, **kwargs)

        async def aget_tuple(self, config: RunnableConfig) -> Any:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self.get_tuple, config)

        async def aput(self, config: RunnableConfig, checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self.put, config, checkpoint, metadata, new_versions)

        async def aput_writes(self, config: RunnableConfig, writes: Any, task_id: Any, task_path: str = "") -> Any:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self.put_writes, config, writes, task_id, task_path)

        async def alist(self, config: RunnableConfig | None, *, filter: Any = None, before: Any = None, limit: Any = None) -> Any:
            loop = asyncio.get_running_loop()

            def _sync_list() -> list:
                return list(self.list(config, filter=filter, before=before, limit=limit))

            items = await loop.run_in_executor(None, _sync_list)
            for item in items:
                yield item

        @classmethod
        @contextmanager
        def from_conn_string(cls, conn_string: str) -> Any:
            import urllib.parse
            import pymysql

            parsed = urllib.parse.urlparse(conn_string)
            user = parsed.username
            password = parsed.password
            if password:
                password = urllib.parse.unquote(password)
            if user:
                user = urllib.parse.unquote(user)
            database = parsed.path.lstrip("/")
            if database:
                database = urllib.parse.unquote(database)

            valid_keys = {
                "host",
                "port",
                "user",
                "password",
                "database",
                "charset",
                "sql_mode",
                "read_default_file",
                "conv",
                "use_unicode",
                "client_flag",
                "cursorclass",
                "ssl",
                "read_timeout",
                "write_timeout",
                "connect_timeout",
                "autocommit",
                "ssl_disabled",
            }

            conn_args = {
                "host": parsed.hostname or "localhost",
                "port": parsed.port or 3306,
                "user": user,
                "password": password or "",
                "database": database,
                "connect_timeout": 5,
                "read_timeout": 15,
                "write_timeout": 15,
                "ssl_disabled": True,
                "autocommit": True,
                "init_command": "SET SESSION lock_wait_timeout = 5",
            }

            if parsed.query:
                params = urllib.parse.parse_qs(parsed.query)
                for k, v in params.items():
                    k_lower = k.lower()
                    if k_lower in valid_keys and k_lower != "init_command":
                        val = v[0]
                        if val.lower() == "true":
                            val = True
                        elif val.lower() == "false":
                            val = False
                        elif val.isdigit():
                            val = int(val)
                        conn_args[k_lower] = val

            print(
                f"[MySQL] Connecting to MySQL checkpointer at {conn_args['host']}:{conn_args['port']}/{database} (timeout: 5s)...",
                flush=True,
            )
            conn = pymysql.connect(**conn_args)
            try:
                saver = cls(conn=conn, serde=None, conn_args=conn_args)
                yield saver
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

except ImportError:
    ReconnectingPyMySQLSaver = None
