#!/usr/bin/env bash
set -e

echo "===================================================================="
echo "  WrenAI Data Source Auto-Initialization via Containerized Claude CLI"
echo "===================================================================="
echo ""
echo "Launching Claude CLI container loaded with offline_wren_generate-mdl skill..."
echo ""

docker-compose -f docker-compose.claude.yaml run --rm wren-claude-init /bin/bash -c "claude --print 'Please discover current database schema and run offline-wren-generate-mdl skill to initialize Wren MDL project.'"
