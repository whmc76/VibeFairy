#!/usr/bin/env bash
# VibeFairy V2 — start daemon
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

# 确保依赖已安装
uv sync --quiet

# Ensure data dirs exist
mkdir -p data/logs

echo "[VibeFairy] Starting daemon..."
.venv/bin/python -m vibefairy run "$@"
