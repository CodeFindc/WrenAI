#!/usr/bin/env bash
set -e

echo "=================================================="
echo "  Building WrenAI Pre-packaged Base Image (CN)"
echo "=================================================="
echo ""

docker build -t wrenai-base:cn -f Dockerfile.cn.base .

echo ""
echo "Successfully built base image 'wrenai-base:cn'!"
echo "Now you can run fast application builds in ~2 seconds:"
echo "  docker-compose -f docker-compose.fast.yaml up -d --build"
