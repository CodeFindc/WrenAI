"""Generate ~/.wren/profiles.yml from environment variables.

Used by ``start_server.bat`` (Windows). The bash entrypoint uses an inline
heredoc instead, but both produce the same shape. Building the YAML with
Python (rather than ``echo`` lines) means nested dict values — the MySQL/Doris
``kwargs`` block and any ``EXTRA_PROFILE_KEYS`` dict — serialise as real YAML
instead of Python ``repr`` (single-quoted, invalid).

Env vars (all optional):
    WREN_PROFILE_FILE   Output path (required; set by the caller).
    ACTIVE_PROFILE      Profile name (default: "default").
    DATASOURCE          Data source type (mysql, doris, postgres, ...).
    DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD/SSL_MODE
                        Standard connection fields.
    EXTRA_PROFILE_KEYS  JSON object of extra YAML keys; nested dicts OK,
                        e.g. '{"kwargs":{"connect_timeout":5},"ssl_mode":"DISABLED"}'.
                        A user-supplied "kwargs" merges over the default
                        MySQL/Doris timeout block (per-key, last wins).

MySQL / Doris timeout & pool defaults (driver-level). The library injects NO
default timeout for mysql (only postgres gets connect_timeout=120), so without
this block a slow query can hang the connection indefinitely:
    DB_CONNECT_TIMEOUT  MySQLdb connect_timeout seconds   (default: 5)
    DB_READ_TIMEOUT     MySQLdb read_timeout seconds      (default: 60)
    DB_WRITE_TIMEOUT    MySQLdb write_timeout seconds     (default: 30)
    DB_MAX_CONNECTIONS  connector connection-pool size    (default: 30)
    DB_PROFILE_TIMEOUTS Set to "0" to skip injecting the kwargs block
                        (e.g. for non-MySQL sources or to rely on the
                        connector/library defaults).
"""

from __future__ import annotations

import json
import os
import sys

import yaml


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def main() -> int:
    out_path = os.environ.get("WREN_PROFILE_FILE", "").strip()
    if not out_path:
        print("WREN_PROFILE_FILE is not set", file=sys.stderr)
        return 2

    profile_name = _env("ACTIVE_PROFILE", "default")
    datasource = _env("DATASOURCE")
    if not datasource:
        print("DATASOURCE is not set", file=sys.stderr)
        return 2

    entry: dict = {"datasource": datasource}
    for yaml_key, env_key in (
        ("host", "DB_HOST"),
        ("port", "DB_PORT"),
        ("database", "DB_NAME"),
        ("user", "DB_USER"),
        ("password", "DB_PASSWORD"),
        ("ssl_mode", "SSL_MODE"),
    ):
        val = os.environ.get(env_key)
        if val:
            entry[yaml_key] = val

    # Default MySQL/Doris timeout + pool kwargs. The connector reads
    # max_connections from connection_info.kwargs and pops it before
    # reaching MySQLdb.connect (mysql.py:148-150); the rest go straight
    # to MySQLdb (connect_timeout / read_timeout / write_timeout).
    #
    # NOTE: MySqlConnectionInfo.kwargs is typed dict[str, str] | None — the
    # pydantic schema rejects int values. MySQLdb and the pool both coerce
    # these strings to int at use time (mysql.py does int(...) on
    # max_connections), so we emit them as strings.
    skip_timeouts = _env("DB_PROFILE_TIMEOUTS", "1") == "0"
    if datasource in ("mysql", "doris") and not skip_timeouts:
        defaults = {
            "connect_timeout": _env("DB_CONNECT_TIMEOUT", "5"),
            "read_timeout": _env("DB_READ_TIMEOUT", "60"),
            "write_timeout": _env("DB_WRITE_TIMEOUT", "30"),
            "max_connections": _env("DB_MAX_CONNECTIONS", "30"),
        }
        merged = entry.get("kwargs", {}) or {}
        if not isinstance(merged, dict):
            merged = {}
        # User kwargs (from EXTRA_PROFILE_KEYS) win per key.
        for k, v in defaults.items():
            merged.setdefault(k, str(v))
        entry["kwargs"] = merged

    extra_raw = _env("EXTRA_PROFILE_KEYS")
    if extra_raw:
        try:
            extra = json.loads(extra_raw)
        except json.JSONDecodeError as exc:
            print(
                f"EXTRA_PROFILE_KEYS is not valid JSON ({exc}) — skipping",
                file=sys.stderr,
            )
        else:
            if not isinstance(extra, dict):
                print(
                    "EXTRA_PROFILE_KEYS is not a JSON object — skipping",
                    file=sys.stderr,
                )
            else:
                # Merge user extras; nested dicts merge into entry["kwargs"]
                # per-key so a partial kwargs override still keeps our
                # defaults for the other keys.
                for k, v in extra.items():
                    if (
                        k == "kwargs"
                        and isinstance(v, dict)
                        and isinstance(entry.get("kwargs"), dict)
                    ):
                        merged = dict(entry["kwargs"])
                        # Stringify user-provided kwargs values too, since
                        # MySqlConnectionInfo.kwargs is dict[str, str].
                        merged.update({kk: str(vv) for kk, vv in v.items()})
                        entry["kwargs"] = merged
                    else:
                        entry[k] = v

    doc = {"active": profile_name, "profiles": {profile_name: entry}}
    text = yaml.safe_dump(
        doc, default_flow_style=False, sort_keys=False, allow_unicode=True
    )
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
