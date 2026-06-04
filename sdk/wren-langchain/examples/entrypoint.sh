#!/bin/bash
#
# entrypoint.sh — WrenAI container entrypoint
#
# This script:
#   1. Detects /project (host-mounted volume), creates it if missing.
#   2. Sets WREN_HOME=/project/.wren so profiles/config are persisted on the host.
#   3. Auto-generates a connection profile from environment variables on first run.
#   4. Initialises a Wren AI project if /project/wren_project.yml does not exist.
#   5. Rebuilds MDL every restart if models/ exists (so model changes take effect).
#   6. Re-indexes LanceDB memory every restart if MDL is built (if wrenai[memory]
#      is installed).
#   7. Hands off to the container CMD via exec.
#
# Concurrency: uses flock(1) to ensure only one process runs the init
# logic at a time. The main process (exec "$@") runs outside the lock.
#
# Environment variables for auto-profile (all optional):
#   ACTIVE_PROFILE      Profile name (default: "default")
#   DATASOURCE          Data source type (mysql, postgres, bigquery, etc.)
#   DB_HOST             Database host
#   DB_PORT             Database port
#   DB_NAME             Database name
#   DB_USER             Database user
#   DB_PASSWORD         Database password
#   SSL_MODE            SSL mode (e.g. DISABLED, PREFERRED, REQUIRED)
#   EXTRA_PROFILE_KEYS  Additional YAML keys as a JSON object, e.g.
#                       '{"ssl_mode":"DISABLED","extra_field":"value"}'
#

set -euo pipefail

# ── Colours ─────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Colour

info()  { printf "${GREEN}[entrypoint]${NC} %s\n" "$*"; }
warn()  { printf "${YELLOW}[entrypoint]${NC} %s\n" "$*"; }
title() { printf "\n${CYAN}━━━ %s ━━━${NC}\n" "$*"; }

# ── Step 1: /project directory ──────────────────────────────────────────────
title "1 / Project directory"

PROJECT_DIR="${PROJECT_DIR:-/project}"
if [ ! -d "$PROJECT_DIR" ]; then
    warn "$PROJECT_DIR does not exist — creating it"
    mkdir -p "$PROJECT_DIR"
else
    info "$PROJECT_DIR already exists"
fi

# ── Step 2: WREN_HOME — persist configs on the host volume ────────────────
title "2 / WREN_HOME setup"

WREN_HOME="${PROJECT_DIR}/.wren"
export WREN_HOME
mkdir -p "$WREN_HOME"
chmod 700 "$WREN_HOME"
info "WREN_HOME set to $WREN_HOME"

# ── Concurrent lock ─────────────────────────────────────────────────────────
# Wrap the init logic in a flock so parallel container starts do not race.
# The main process (exec "$@") runs outside the lock.
LOCK_FILE="${WREN_HOME}/init.lock"
LOCK_FD=200

exec 200>"$LOCK_FILE"

# flock timeout: wait up to 60 seconds for the lock
if ! flock -w 60 "$LOCK_FD" 2>/dev/null; then
    warn "Could not acquire init lock after 60s — another process may be holding it"
    warn "Proceeding without lock (risky if multiple containers share /project)"
else
    # Lock acquired — ensure it is released before exec below
    # (The lock release runs on the EXIT trap or when we explicitly release it)
    # We release it right before exec by closing fd 200.
    info "Init lock acquired (fd 200)"

    # ── Step 3: Auto-generate profiles.yml from environment ────────────────────
    title "3 / Connection profile"

    PROFILES_FILE="${WREN_HOME}/profiles.yml"

    auto_generate_profile() {
        local profile_name="${ACTIVE_PROFILE:-default}"
        local datasource="${DATASOURCE:-}"

        if [ -z "$datasource" ]; then
            warn "DATASOURCE not set — skipping auto-profile generation"
            return 0
        fi

        info "Generating profile '${profile_name}' (datasource: ${datasource}) from environment variables"

        # Start YAML output
        {
            echo "active: ${profile_name}"
            echo "profiles:"
            echo "  ${profile_name}:"
            echo "    datasource: ${datasource}"

            # Standard connection fields (only if set)
            [ -n "${DB_HOST:-}" ]     && echo "    host: ${DB_HOST}"
            [ -n "${DB_PORT:-}" ]     && echo "    port: ${DB_PORT}"
            [ -n "${DB_NAME:-}" ]     && echo "    database: ${DB_NAME}"
            [ -n "${DB_USER:-}" ]     && echo "    user: ${DB_USER}"
            [ -n "${DB_PASSWORD:-}" ] && echo "    password: ${DB_PASSWORD}"
            [ -n "${SSL_MODE:-}" ]    && echo "    ssl_mode: ${SSL_MODE}"

            # Additional keys from JSON env var
            if [ -n "${EXTRA_PROFILE_KEYS:-}" ]; then
                echo "$EXTRA_PROFILE_KEYS" | python3 -c "
        import sys, json
        extra = json.load(sys.stdin)
        for k, v in extra.items():
            print(f'    {k}: {v}')
        " 2>/dev/null || warn "EXTRA_PROFILE_KEYS is not valid JSON — skipping extra keys"
            fi
        } > "$PROFILES_FILE"

        chmod 600 "$PROFILES_FILE"
        info "Profile written to $PROFILES_FILE"
    }

    # Only auto-generate if profiles.yml doesn't exist OR is empty
    if [ ! -f "$PROFILES_FILE" ] || [ ! -s "$PROFILES_FILE" ]; then
        auto_generate_profile
    elif [ -n "${ACTIVE_PROFILE:-}" ]; then
        # File exists — check if the active profile is already present
        if ! grep -q "^active:" "$PROFILES_FILE" 2>/dev/null; then
            warn "profiles.yml exists but has no active profile — checking env..."
            # If the file exists but doesn't have the requested profile, add it
            if [ -n "${DATASOURCE:-}" ]; then
                # Quick check: does the profile already exist in the file?
                if ! grep -q "^  ${ACTIVE_PROFILE}:" "$PROFILES_FILE" 2>/dev/null; then
                    info "Adding profile '${ACTIVE_PROFILE}' to existing profiles.yml"
                    # Append new profile; update active if needed
                    {
                        echo "  ${ACTIVE_PROFILE}:"
                        echo "    datasource: ${DATASOURCE}"
                        [ -n "${DB_HOST:-}" ]     && echo "    host: ${DB_HOST}"
                        [ -n "${DB_PORT:-}" ]     && echo "    port: ${DB_PORT}"
                        [ -n "${DB_NAME:-}" ]     && echo "    database: ${DB_NAME}"
                        [ -n "${DB_USER:-}" ]     && echo "    user: ${DB_USER}"
                        [ -n "${DB_PASSWORD:-}" ] && echo "    password: ${DB_PASSWORD}"
                        [ -n "${SSL_MODE:-}" ]    && echo "    ssl_mode: ${SSL_MODE}"
                    } >> "$PROFILES_FILE"
                fi
                # Set active profile
                python3 -c "
        import yaml
        path = '${PROFILES_FILE}'
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        data['active'] = '${ACTIVE_PROFILE}'
        with open(path, 'w') as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        " 2>/dev/null || true
            fi
        fi
    fi

    # Ensure correct permissions
    if [ -f "$PROFILES_FILE" ]; then
        chmod 600 "$PROFILES_FILE"
    fi

    # ── Step 4: Initialise Wren project ─────────────────────────────────────────
    title "4 / Project initialisation"

    PROJECT_FILE="${PROJECT_DIR}/wren_project.yml"
    if [ ! -f "$PROJECT_FILE" ]; then
        info "No wren_project.yml found — initialising empty project at ${PROJECT_DIR}"
        wren context init --empty --path "$PROJECT_DIR" 2>&1 | while IFS= read -r line; do
            info "  wren: $line"
        done
        info "Project initialised successfully"

        # Bind profile to project if we have one
        if [ -f "$PROFILES_FILE" ] && [ -s "$PROFILES_FILE" ]; then
            ACTIVE=$(python3 -c "
        import yaml
        with open('${PROFILES_FILE}') as f:
            data = yaml.safe_load(f) or {}
        print(data.get('active', '') or '')
        " 2>/dev/null || echo "")
            if [ -n "$ACTIVE" ]; then
                info "Binding profile '${ACTIVE}' to project"
                # Write profile and data_source into wren_project.yml
                DS=$(python3 -c "
        import yaml
        with open('${PROFILES_FILE}') as f:
            data = yaml.safe_load(f) or {}
        p = data.get('profiles', {}).get('${ACTIVE}', {})
        print(p.get('datasource', ''))
        " 2>/dev/null || echo "")
                python3 -c "
        import yaml
        from pathlib import Path
        path = Path('${PROJECT_FILE}')
        data = yaml.safe_load(path.read_text()) or {}
        data['profile'] = '${ACTIVE}'
        if '${DS}':
            data['data_source'] = '${DS}'
        path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
        " 2>/dev/null || true
                info "Profile '${ACTIVE}' bound to project"
            fi
        fi
    else
        info "Project already exists at ${PROJECT_DIR}"
    fi

    # ── Step 5: Build MDL (always on restart) ────────────────────────────────────
    title "5 / MDL build"

    MODEL_DIR="${PROJECT_DIR}/models"

    if [ -d "$MODEL_DIR" ] && [ "$(ls -A "$MODEL_DIR" 2>/dev/null)" ]; then
        info "Rebuilding MDL from models/..."
        wren context build --path "$PROJECT_DIR" 2>&1 | while IFS= read -r line; do
            info "  wren: $line"
        done
        info "MDL build complete"
    else
        info "No models directory or empty — skipping MDL build"
    fi

    # ── Step 6: Memory index (always on restart) ─────────────────────────────────
    title "6 / Memory index"

    if [ -f "${PROJECT_DIR}/target/mdl.json" ]; then
        MEMORY_DIR="${PROJECT_DIR}/.wren/memory"

        # Check if the `memory` extra is available
        if wren memory index --help >/dev/null 2>&1; then
            info "Indexing MDL schema into LanceDB memory..."
            wren memory index --path "$MEMORY_DIR" --mdl "${PROJECT_DIR}/target/mdl.json" --no-seed 2>&1 | while IFS= read -r line; do
                info "  wren: $line"
            done
            info "Memory indexing complete"
        else
            info "Memory extra not installed — skipping memory index"
            info "  Install with: pip install \"wrenai[memory]\""
            info "  Then run:     wren memory index --path \"${PROJECT_DIR}/.wren/memory\" --mdl \"${PROJECT_DIR}/target/mdl.json\""
        fi
    else
        info "No MDL build found — skipping memory index"
    fi

    # ── Step 7: Configuration summary ───────────────────────────────────────────
    title "7 / Configuration summary"

    info "Project directory : ${PROJECT_DIR}"
    info "WREN_HOME         : ${WREN_HOME}"
    info "Profiles file     : ${PROFILES_FILE}"
    if [ -f "$PROFILES_FILE" ] && [ -s "$PROFILES_FILE" ]; then
        ACTIVE_NAME=$(python3 -c "
        import yaml
        with open('${PROFILES_FILE}') as f:
            data = yaml.safe_load(f) or {}
        print(data.get('active', '(none)') or '(none)')
        " 2>/dev/null || echo "(unknown)")
        info "Active profile    : ${ACTIVE_NAME}"
    else
        warn "No profiles configured — use 'wren profile add' and 'wren profile switch'"
    fi
    if [ -f "$PROJECT_FILE" ]; then
        info "Project file      : ${PROJECT_FILE}"
        info "To add a model     : wren context import ..."
        info "To validate        : wren context validate --path ${PROJECT_DIR}"
        info "To build           : wren context build --path ${PROJECT_DIR}"
        info "To query           : wren --sql 'SELECT ...'"
    else
        warn "No project initialised yet — run 'wren context init --path ${PROJECT_DIR}'"
    fi

    info "WrenAI container entrypoint completed"

    # ── Release the lock before exec ────────────────────────────────
    # Close fd 200 so the lock is released. The long-running main process
    # must not hold the lock.
    exec 200>&-
    info "Init lock released"
fi

# ── Step 8: Hand off to CMD ────────────────────────────────────────────────
title "Starting main process"
exec "$@"
