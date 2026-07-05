# aion-suite

![status](https://img.shields.io/badge/status-working-3fb950)
![tests](https://img.shields.io/badge/tests-16%2F16%20passing-3fb950)
![python](https://img.shields.io/badge/python-3.11-3776AB?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-API-000000?logo=flask&logoColor=white)
![MCP](https://img.shields.io/badge/MCP-aion--mcp-8957e5)

Clean, self-contained home for the **AION integration used by the `mcpbuilder` MCP server** — extracted from the sprawling drayhub trees so it can be run with one start/stop.

![aion-suite status and test run](assets/screenshot.svg)

## The chain

```
mcpbuilder (aion-mcp, stdio)  ──calls──▶  aion-core (Flask :5000)
                                              │
                        ┌─────────────────────┴─────────────────────┐
                   Ollama (Windows host, GPU)              Postgres (aion_db)
                   aion-producer:latest                    mft-server-db-1 :5432
                   127.0.0.1:11434
```

- **aion-core/** — the Flask API (`web.py`), a clean copy of `drayhub-platform/services/aion`, with a project-local `.venv` and host config in `aion-core/config_local.py`. This is the only service the suite owns.
- **Ollama** & **Postgres** are shared external dependencies — *ensured*, not owned. Ollama runs on the Windows host (GPU); Postgres reuses the existing `mft-server-db-1` container so `aion_db` data is preserved.
- **mcpbuilder** stays at `/mnt/c/projects/mcpbuilder` (its own project). It's stdio, spawned on demand by Claude Desktop / codex — nothing to daemonize. Its aion tools hit `aion-core` at `127.0.0.1:5000`.

## Fleet topology page

AION's chat UI now includes a **Fleet** view (`http://127.0.0.1:5000/fleet`, linked from the
chat header) that draws every machine and service in the stack, the connections between them,
and live per-node health. Click any node for details; machines expand to per-agent
(claude / codex / agy) status. It auto-refreshes every 12s.

![Aion Fleet topology](assets/fleet-topology.svg)

Machine/agent health comes from a small **read-only fleet gateway** — a localhost HTTP face
(`:5100`) over mcpbuilder's `fleet_status` probe, since mcpbuilder itself is stdio-only. The
gateway exposes health only (never remote execution) and is started/stopped with the suite. If
it's down, the page degrades gracefully and shows machines as *unknown*. Data sources:

- **infra** (Ollama, Postgres, aion-core) — direct HTTP/TCP probes from `aion-core`
- **fleet machines + agents** (wsl / draydev / ec2) — the fleet gateway
- **Kali container** — the existing `/api/kali` sensor health

## Usage

```bash
./start.sh      # ensure Ollama+Postgres, start aion-core + fleet-gateway, verify mcpbuilder, print status
./status.sh     # health line per component (incl. fleet-gw)
./stop.sh       # stop aion-core + fleet-gateway (leaves Ollama+Postgres up)
./stop.sh --deps  # also stop the Postgres container (Ollama untouched)
```

## Config

Everything host-specific is in `aion-core/config_local.py` (git-ignored):
`model=aion-producer:latest`, `OLLAMA_BASE_URL=http://127.0.0.1:11434`,
`DATABASE_URL=…@127.0.0.1:5432/aion_db`, `aion_host=192.168.0.114`.

## Manifest

`aion-suite.json` is the machine-readable source of truth (ids, paths, health checks, deps).

## Notes / deferred

Scope is deliberately minimal (the mcpbuilder AION integration only). Not included: drayhub portal / sonchat / share / sensors. The old drayhub AION trees are left intact — this suite is a clean parallel copy, not a move. A dedicated Postgres (instead of reusing `mft-server-db-1`) is a possible future step.
