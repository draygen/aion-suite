#!/usr/bin/env bash
# aion-suite global start — dependency-ordered: ollama -> postgres -> aion-core -> verify mcpbuilder.
# Idempotent: safe to re-run. Ollama & Postgres are shared deps (ensured, not owned).
set -uo pipefail

SUITE="$(cd "$(dirname "$0")" && pwd)"
CORE="$SUITE/aion-core"
MCPBUILDER="/mnt/c/projects/mcpbuilder"
PID_DIR="$SUITE/pids"; LOG_DIR="$SUITE/logs"
mkdir -p "$PID_DIR" "$LOG_DIR"

OLLAMA_URL="http://127.0.0.1:11434"
OLLAMA_MODEL="aion-producer:latest"
PG_CONTAINER="mft-server-db-1"
AION_HEALTH="http://127.0.0.1:5000/api/health"

ok(){ echo "  [+] $*"; }; warn(){ echo "  [!] $*"; }; err(){ echo "  [-] $*"; }

echo "== aion-suite start =="

# 1) Ollama (external, Windows-host GPU) — verify only
echo "[1/5] Ollama"
if curl -sf --max-time 5 "$OLLAMA_URL/api/tags" >/tmp/aion_ollama.json 2>/dev/null; then
  if grep -q "$OLLAMA_MODEL" /tmp/aion_ollama.json; then ok "Ollama up, $OLLAMA_MODEL present"; else warn "Ollama up but $OLLAMA_MODEL not found (run: ollama pull/create it)"; fi
else
  warn "Ollama not reachable at $OLLAMA_URL — it runs on the Windows host; AION chat will fail until it's up"
fi

# 2) Postgres (ensure the shared container is running)
echo "[2/5] Postgres ($PG_CONTAINER)"
if (exec 3<>/dev/tcp/127.0.0.1/5432) 2>/dev/null; then ok "Postgres :5432 reachable"
elif command -v docker >/dev/null 2>&1; then
  if docker start "$PG_CONTAINER" >/dev/null 2>&1; then
    for i in $(seq 1 15); do (exec 3<>/dev/tcp/127.0.0.1/5432) 2>/dev/null && break; sleep 1; done
    (exec 3<>/dev/tcp/127.0.0.1/5432) 2>/dev/null && ok "started $PG_CONTAINER, :5432 up" || err "$PG_CONTAINER started but :5432 not answering"
  else err "could not start $PG_CONTAINER (does it exist?)"; fi
else err "Postgres down and docker unavailable"; fi

# 3) aion-core
echo "[3/5] aion-core (:5000)"
PID_FILE="$PID_DIR/aion-core.pid"
if curl -sf --max-time 3 "$AION_HEALTH" >/dev/null 2>&1; then
  ok "aion-core already healthy"
else
  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null; then
    warn "pid $(cat "$PID_FILE") alive but not healthy yet; waiting"
  else
    nohup bash "$CORE/start-aion-core.sh" >"$LOG_DIR/aion-core-launch.log" 2>&1 &
    echo $! > "$PID_FILE"
    ok "launched aion-core (pid $(cat "$PID_FILE"))"
  fi
  for i in $(seq 1 30); do curl -sf --max-time 3 "$AION_HEALTH" >/dev/null 2>&1 && break; sleep 2; done
  if curl -sf --max-time 3 "$AION_HEALTH" >/dev/null 2>&1; then ok "aion-core healthy"; else err "aion-core did not become healthy — see $LOG_DIR/aion-core.log"; fi
fi

# 4) mcpbuilder (stdio, on-demand) — verify built
echo "[4/5] mcpbuilder (reference)"
if [ -f "$MCPBUILDER/dist/index.js" ]; then ok "dist/ present (spawned on demand by Claude Desktop/codex)"
elif [ -d "$MCPBUILDER" ]; then warn "dist/ missing — run: (cd $MCPBUILDER && npm run build)"
else warn "mcpbuilder not found at $MCPBUILDER"; fi

# 5) fleet-gateway (read-only HTTP over mcpbuilder's fleet status; backs the /fleet page)
echo "[5/5] fleet-gateway (:5100)"
GW_PID_FILE="$PID_DIR/fleet-gateway.pid"
GW_HEALTH="http://127.0.0.1:5100/health"
if curl -sf --max-time 3 "$GW_HEALTH" >/dev/null 2>&1; then
  ok "fleet-gateway already up"
elif [ -f "$MCPBUILDER/dist/fleet-gateway.js" ] && command -v node >/dev/null 2>&1; then
  # Load mcpbuilder's git-ignored .env so the gateway inherits the fleet/SSH
  # secrets (KALI_/FLEET_* — draydev, ec2). Claude Desktop injects these when it
  # spawns the MCP server; the standalone gateway needs them loaded here.
  if [ -f "$MCPBUILDER/.env" ]; then set -a; . "$MCPBUILDER/.env"; set +a; fi
  nohup node "$MCPBUILDER/dist/fleet-gateway.js" >"$LOG_DIR/fleet-gateway.log" 2>&1 &
  echo $! > "$GW_PID_FILE"
  for i in $(seq 1 10); do curl -sf --max-time 2 "$GW_HEALTH" >/dev/null 2>&1 && break; sleep 1; done
  if curl -sf --max-time 3 "$GW_HEALTH" >/dev/null 2>&1; then ok "launched fleet-gateway (pid $(cat "$GW_PID_FILE"))"; else warn "fleet-gateway launched but not answering — see $LOG_DIR/fleet-gateway.log"; fi
else warn "fleet-gateway not built — run: (cd $MCPBUILDER && npm run build); the /fleet page will show machines as 'unknown' until it's up"; fi

echo "== done =="; echo "  [i] Fleet topology page: http://127.0.0.1:5000/fleet"; exec "$SUITE/status.sh"
