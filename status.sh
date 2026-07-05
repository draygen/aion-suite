#!/usr/bin/env bash
# aion-suite status — one health line per component.
set -uo pipefail
SUITE="$(cd "$(dirname "$0")" && pwd)"
MCPBUILDER="/mnt/c/projects/mcpbuilder"

line(){ printf "  %-12s %s\n" "$1" "$2"; }
up="✓"; down="✗"

echo "== aion-suite status =="

# Ollama
if curl -sf --max-time 4 http://127.0.0.1:11434/api/tags >/tmp/aion_st_oll.json 2>/dev/null; then
  grep -q "aion-producer:latest" /tmp/aion_st_oll.json && m="(aion-producer:latest present)" || m="(model missing)"
  line "ollama" "$up up @127.0.0.1:11434 $m"
else line "ollama" "$down down (Windows-host service)"; fi

# Postgres
if (exec 3<>/dev/tcp/127.0.0.1/5432) 2>/dev/null; then line "postgres" "$up up @127.0.0.1:5432 (aion_db)"; else line "postgres" "$down down"; fi

# aion-core
if curl -sf --max-time 4 http://127.0.0.1:5000/api/health >/dev/null 2>&1; then
  pid="$(cat "$SUITE/pids/aion-core.pid" 2>/dev/null || echo '?')"
  line "aion-core" "$up healthy @127.0.0.1:5000 (pid $pid)"
else line "aion-core" "$down down"; fi

# mcpbuilder
if [ -f "$MCPBUILDER/dist/index.js" ]; then line "mcpbuilder" "$up built (stdio, on-demand)"; else line "mcpbuilder" "$down dist/ not built"; fi

# fleet-gateway (read-only status HTTP that backs the /fleet topology page)
if curl -sf --max-time 4 http://127.0.0.1:5100/health >/dev/null 2>&1; then
  line "fleet-gw" "$up up @127.0.0.1:5100 (topology: :5000/fleet)"
else line "fleet-gw" "$down down (npm run gateway in mcpbuilder — /fleet shows machines as unknown)"; fi
