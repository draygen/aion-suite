#!/usr/bin/env bash
# Run the AION contract + intelligence test suite against a live aion-core.
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$DIR/../aion-core/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

# Best-effort: pick up the session token from the live MCP config (mirrors mcpbuilder),
# so the session-authed contract tests run. Never echoed. Override by pre-setting the env.
LIVE_CFG="/mnt/c/Users/draygen/AppData/Roaming/Claude/claude_desktop_config.json"
if [ -f "$LIVE_CFG" ] && command -v node >/dev/null 2>&1; then
  if [ -z "${AION_SESSION_TOKEN:-}" ]; then
    AION_SESSION_TOKEN="$(node -e 'try{process.stdout.write(require(process.argv[1]).mcpServers["aion-mcp"].env.AION_SESSION_TOKEN||"")}catch(e){}' "$LIVE_CFG" 2>/dev/null)"
    export AION_SESSION_TOKEN
  fi
  if [ -z "${AION_SERVICE_TOKEN:-}" ]; then
    AION_SERVICE_TOKEN="$(node -e 'try{process.stdout.write(require(process.argv[1]).mcpServers["aion-mcp"].env.AION_SERVICE_TOKEN||"")}catch(e){}' "$LIVE_CFG" 2>/dev/null)"
    export AION_SERVICE_TOKEN
  fi
fi
exec "$PY" "$DIR/test_aion.py" "$@"
