#!/usr/bin/env bash
# VibeFairy — start daemon
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

# Activate venv if present
if [ -d ".venv" ]; then
    source .venv/bin/activate
fi

# Ensure data dirs exist
mkdir -p data/logs

echo "[VibeFairy] Starting daemon..."
python -m vibefairy run "$@"
