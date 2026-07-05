#!/usr/bin/env bash
# aion-core launcher — self-contained (project-local .venv, no drayhub paths).
# Foreground by default; the suite's start.sh backgrounds it and records the pid.
set -euo pipefail

AION_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$AION_DIR/.venv"
SUITE_DIR="$(cd "$AION_DIR/.." && pwd)"

export PYTHONPATH="$AION_DIR"
export AION_LOG_FILE="${AION_LOG_FILE:-$SUITE_DIR/logs/aion-core.log}"
mkdir -p "$(dirname "$AION_LOG_FILE")"

if [ ! -x "$VENV/bin/python" ]; then
  echo "[aion-core] venv missing at $VENV — run: python3 -m venv .venv && .venv/bin/pip install -r requirements.runtime.txt psycopg2-binary" >&2
  exit 1
fi

cd "$AION_DIR"
echo "[aion-core] starting Flask app on http://0.0.0.0:5000 (model + Ollama/DB from config_local.py)"
exec "$VENV/bin/python" web.py
