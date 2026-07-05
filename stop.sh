#!/usr/bin/env bash
# aion-suite global stop — stops aion-core. Shared deps (Ollama, Postgres) are left up
# unless --deps is passed (then the Postgres container is stopped too; Ollama is never
# touched since it's a Windows-host service).
set -uo pipefail

SUITE="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$SUITE/pids/aion-core.pid"
GW_PID_FILE="$SUITE/pids/fleet-gateway.pid"
PG_CONTAINER="mft-server-db-1"
STOP_DEPS="no"; [ "${1:-}" = "--deps" ] && STOP_DEPS="yes"

ok(){ echo "  [+] $*"; }; warn(){ echo "  [!] $*"; }

echo "== aion-suite stop =="

# fleet-gateway (read-only status HTTP backing the /fleet page)
if [ -f "$GW_PID_FILE" ]; then
  GPID="$(cat "$GW_PID_FILE" 2>/dev/null || true)"
  if [ -n "$GPID" ] && kill -0 "$GPID" 2>/dev/null; then kill "$GPID" 2>/dev/null && ok "fleet-gateway stopped"; fi
  rm -f "$GW_PID_FILE"
fi
pkill -f "dist/fleet-gateway.js" 2>/dev/null || true

# aion-core: prefer pidfile, then fall back to matching the web.py process in this project
stopped="no"
if [ -f "$PID_FILE" ]; then
  PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    # kill the launcher and its python child
    pkill -P "$PID" 2>/dev/null || true; kill "$PID" 2>/dev/null || true; stopped="yes"
  fi
  rm -f "$PID_FILE"
fi
# belt-and-suspenders: any python web.py running from this project dir
pkill -f "$SUITE/aion-core/.venv/bin/python web.py" 2>/dev/null && stopped="yes" || true
pkill -f "aion-suite/aion-core.*web.py" 2>/dev/null && stopped="yes" || true
sleep 1
if curl -sf --max-time 3 http://127.0.0.1:5000/api/health >/dev/null 2>&1; then
  warn "something is still answering on :5000 (may be a different AION instance, e.g. the old drayhub launch)"
else
  [ "$stopped" = "yes" ] && ok "aion-core stopped" || warn "aion-core was not running"
fi

if [ "$STOP_DEPS" = "yes" ] && command -v docker >/dev/null 2>&1; then
  docker stop "$PG_CONTAINER" >/dev/null 2>&1 && ok "stopped Postgres ($PG_CONTAINER)" || warn "could not stop $PG_CONTAINER"
  echo "  [i] Ollama left running (Windows-host service)"
fi
echo "== done =="
