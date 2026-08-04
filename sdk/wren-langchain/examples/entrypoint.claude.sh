#!/bin/bash
#
# entrypoint.claude.sh — Container entrypoint for Claude CLI Automated MDL Initialization
#
# Features:
#   1. Detects /project (host-mounted volume), creates it if missing.
#   2. Sets WREN_HOME=/project/.wren for persistent configs.
#   3. Auto-generates connection profile from environment variables.
#   4. Checks for wren_project.yml; if missing, runs 'wren context init'.
#   5. Runs automated Claude CLI with model alias overrides and offline-wren-generate-mdl skill.
#   6. Builds MDL and indexes memory automatically.
#

set -euo pipefail

# Colours
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { printf "${GREEN}[entrypoint.claude]${NC} %s\n" "$*"; }
warn()  { printf "${YELLOW}[entrypoint.claude]${NC} %s\n" "$*"; }
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

# ── Step 2: WREN_HOME setup ────────────────────────────────────────────────
title "2 / WREN_HOME setup"

WREN_HOME="${PROJECT_DIR}/.wren"
export WREN_HOME
mkdir -p "$WREN_HOME"
chmod 700 "$WREN_HOME"
info "WREN_HOME set to $WREN_HOME"

# ── Step 3: Auto-generate connection profile ────────────────────────────────
title "3 / Connection profile"

PROFILES_FILE="${WREN_HOME}/profiles.yml"

if [ ! -f "$PROFILES_FILE" ] || [ ! -s "$PROFILES_FILE" ]; then
    info "Generating connection profile from environment..."
    profile_name="${ACTIVE_PROFILE:-default}"
    datasource="${DATASOURCE:-mysql}"

    python3 - <<PY
import os, sys, yaml

profile_name = "${profile_name}"
datasource = "${datasource}"

entry = {"datasource": datasource}
for yaml_key, env_key in (
    ("host", "DB_HOST"), ("port", "DB_PORT"), ("database", "DB_NAME"),
    ("user", "DB_USER"), ("password", "DB_PASSWORD"), ("ssl_mode", "SSL_MODE"),
):
    val = os.environ.get(env_key)
    if val:
        entry[yaml_key] = val

doc = {"active": profile_name, "profiles": {profile_name: entry}}
with open("${PROFILES_FILE}", "w") as f:
    yaml.safe_dump(doc, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
PY
    chmod 600 "$PROFILES_FILE"
    info "Profile written to $PROFILES_FILE"
fi

# ── Step 4: Wren Project Initialization & Claude Skill Execution ───────────
title "4 / Project initialisation & Claude Skill Execution"

PROJECT_FILE="${PROJECT_DIR}/wren_project.yml"

if [ ! -f "$PROJECT_FILE" ]; then
    info "No wren_project.yml found — initialising project at ${PROJECT_DIR}"
    wren context init --path "$PROJECT_DIR" 2>&1 | while IFS= read -r line; do
        info "  wren: $line"
    done
    info "Wren project initialised successfully"
else
    info "Existing wren_project.yml detected at ${PROJECT_DIR}"
fi

# Ensure .claude skills exist in project directory
mkdir -p "${PROJECT_DIR}/.claude/skills"
if [ -d "/app/sdk/wren-langchain/examples/.claude/skills" ]; then
    cp -r /app/sdk/wren-langchain/examples/.claude/skills/* "${PROJECT_DIR}/.claude/skills/" 2>/dev/null || true
fi

# Execute automated Claude CLI onboarding with proper Anthropic model routing environment variables
target_db="${DB_NAME:-prod-db}"
target_ds="${DATASOURCE:-mysql}"

anthropic_auth="${ANTHROPIC_AUTH_TOKEN:-${ANTHROPIC_API_KEY:-sk-dummy}}"
anthropic_url="${ANTHROPIC_BASE_URL:-http://192.168.110.209:8200/v1}"
anthropic_model="${ANTHROPIC_MODEL:-Qwen3.6-27B-AEON-Ultimate-Uncensored-BF16}"

sonnet_model="${ANTHROPIC_DEFAULT_SONNET_MODEL:-$anthropic_model}"
haiku_model="${ANTHROPIC_DEFAULT_HAIKU_MODEL:-$anthropic_model}"
opus_model="${ANTHROPIC_DEFAULT_OPUS_MODEL:-$anthropic_model}"
subagent_model="${CLAUDE_CODE_SUBAGENT_MODEL:-$anthropic_model}"
agent_teams="${CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS:-1}"

if command -v claude >/dev/null 2>&1; then
    info "Launching automated Claude CLI with offline-wren-generate-mdl skill..."
    info "Target Database: ${target_db} (${target_ds})"
    info "Target Model: ${anthropic_model} via ${anthropic_url}"
    
    # Ensure non-root user exists for running claude CLI without root privilege restrictions
    if ! id -u wrenuser >/dev/null 2>&1; then
        useradd -m -u 1000 -s /bin/bash wrenuser 2>/dev/null || true
    fi

    env_exports="export IS_SANDBOX=1; export IS_SANDBOXED=1; export ANTHROPIC_AUTH_TOKEN=\"${anthropic_auth}\"; export ANTHROPIC_API_KEY=\"${anthropic_auth}\"; export ANTHROPIC_BASE_URL=\"${anthropic_url}\"; export ANTHROPIC_MODEL=\"${anthropic_model}\"; export ANTHROPIC_DEFAULT_SONNET_MODEL=\"${sonnet_model}\"; export ANTHROPIC_DEFAULT_HAIKU_MODEL=\"${haiku_model}\"; export ANTHROPIC_DEFAULT_OPUS_MODEL=\"${opus_model}\"; export CLAUDE_CODE_SUBAGENT_MODEL=\"${subagent_model}\"; export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=\"${agent_teams}\";"

    if [ "$(id -u)" -eq 0 ]; then
        chown -R wrenuser:wrenuser "$PROJECT_DIR" /app 2>/dev/null || true
        su -s /bin/bash wrenuser -c "${env_exports} cd \"${PROJECT_DIR}\"; claude --dangerously-skip-permissions --verbose --output-format stream-json -p '当前目录为MDL数据源所在目录，使用offline-wren-generate-mdl 技能探索${target_db}数据库，并为所有表生成MDL,数据库为${target_ds}'" 2>&1 | while IFS= read -r line; do
            info "  claude: $line"
        done
    else
        eval "$env_exports"
        cd "$PROJECT_DIR"
        claude --dangerously-skip-permissions --verbose --output-format stream-json -p "当前目录为MDL数据源所在目录，使用offline-wren-generate-mdl 技能探索${target_db}数据库，并为所有表生成MDL,数据库为${target_ds}" 2>&1 | while IFS= read -r line; do
            info "  claude: $line"
        done
    fi
    info "Claude CLI MDL generation completed"
else
    warn "claude CLI command not found in container — skipping automated skill execution"
fi

# ── Step 5: Build MDL ───────────────────────────────────────────────────────
title "5 / MDL build"

MODEL_DIR="${PROJECT_DIR}/models"

if [ -d "$MODEL_DIR" ] && [ "$(ls -A "$MODEL_DIR" 2>/dev/null)" ]; then
    info "Building MDL from models/..."
    wren context build --path "$PROJECT_DIR" 2>&1 | while IFS= read -r line; do
        info "  wren: $line"
    done
    info "MDL build complete"
else
    warn "No models directory found or empty"
fi

# ── Step 6: Memory Index ────────────────────────────────────────────────────
title "6 / Memory index"

if [ -f "${PROJECT_DIR}/target/mdl.json" ]; then
    MEMORY_DIR="${PROJECT_DIR}/.wren/memory"
    if wren memory index --help >/dev/null 2>&1; then
        info "Indexing MDL schema into LanceDB memory..."
        wren memory index --path "$MEMORY_DIR" --mdl "${PROJECT_DIR}/target/mdl.json" --no-seed 2>&1 | while IFS= read -r line; do
            info "  wren: $line"
        done
        info "Memory indexing complete"
    fi
fi

info "Initialization sequence finished."

# Execute CMD if provided
if [ "$#" -gt 0 ]; then
    exec "$@"
fi
