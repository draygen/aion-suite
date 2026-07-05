# MCPBuilder + AION Fleet QA Report

Date: 2026-07-05

## Scope

This QA pass covered:

- `mcpbuilder` MCP server build and tool registration.
- AION Core service health, service-token chat, and session-token endpoints.
- Fleet tools: `fleet_status`, `fleet_run` path via status probes, and `fleet_review`.
- Cross-server communication flow across WSL, Windows/Ollama, Postgres, draydev, and EC2.

Secrets were not printed in this report. Live token values and cookie values remain in local ignored config only.

## Current Result

Overall status: PASS with follow-up findings.

- `mcpbuilder` TypeScript build: PASS.
- MCP tool registration: PASS, 45 tools exposed across 12 tool groups.
- AION suite validation: PASS, 16/16 checks.
- AION session-token endpoints through MCP: PASS.
- AION service chat through MCP: PASS.
- Fleet status: PASS, 9/9 agent-machine combinations.
- Fleet review fan-out: PASS, codex + agy on `draydev`.

## Live Validation Evidence

### AION Suite

`./status.sh`:

```text
ollama       up @127.0.0.1:11434 (aion-producer:latest present)
postgres     up @127.0.0.1:5432 (aion_db)
aion-core    healthy @127.0.0.1:5000
mcpbuilder   built (stdio, on-demand)
```

`./tests/run.sh`:

```text
total 16 | passed 16 | critical failed 0 | informational failed 0
```

Important covered endpoints:

- `GET /api/health`
- `POST /api/service/chat`
- `GET /api/channels`
- `GET /api/activity`
- `GET /api/memory/browse`
- `GET /api/admin/users`

### MCPBuilder

MCP `listTools` returned 45 tools:

```text
aion: 14
conversation: 3
fleet: 3
image: 1
jenn: 2
kali: 6
memory: 5
ollama: 1
openai: 1
portal: 2
sonchat: 5
system: 2
```

Representative MCP tool calls:

```text
system_status: OK
aion_channels: OK
aion_activity: OK
aion_memory_browse: OK
aion_admin_users: OK
aion_api_chat: OK, returned exact numeric response
```

Fleet health:

```text
claude@wsl: PASS
codex@wsl: PASS
agy@wsl: PASS
claude@draydev: PASS
codex@draydev: PASS
agy@draydev: PASS
claude@ec2: PASS
codex@ec2: PASS
agy@ec2: PASS
```

Fleet review:

```text
codex@draydev: replied successfully
agy@draydev: replied successfully
```

## Communication Map

### Primary MCP Call Path

```text
MCP client
  -> starts mcpbuilder via stdio
  -> mcpbuilder dispatches tool by name
  -> specific tool module performs local HTTP, local shell, or SSH operation
  -> result returns as MCP text content
```

### AION Service Chat Path

```text
MCP client
  -> mcpbuilder stdio
  -> aion_api_chat
  -> HTTP POST http://127.0.0.1:5000/api/service/chat
     header: X-Aion-Service-Token from env
     body: { message, tts:false, optional channel }
  -> AION Core Flask
  -> Postgres for user, history, events, facts
  -> Ollama at 127.0.0.1:11434 from WSL
  -> Windows-host GPU model aion-producer:latest
  -> AION Core returns text response
  -> mcpbuilder returns MCP result
```

### AION Session/Admin Tool Path

```text
MCP client
  -> mcpbuilder stdio
  -> aion_channels / aion_activity / aion_memory_browse / aion_admin_users
  -> HTTP GET http://127.0.0.1:5000/...
     cookie: aion_token from AION_SESSION_TOKEN env
  -> AION Core validates token in Postgres
  -> AION Core returns channel, activity, memory, or admin data
  -> mcpbuilder returns MCP result
```

### Fleet Tool Path

```text
MCP client
  -> mcpbuilder stdio
  -> fleet_run / fleet_review / fleet_status
  -> local WSL command or SSH to target machine
  -> target shell runs one agent CLI:
       claude -p
       codex exec
       agy -p
  -> agent response captured
  -> mcpbuilder returns combined result
```

Machine routing:

```text
wsl     -> local shell
draydev -> SSH to dev VM
ec2     -> SSH to production host
```

Prompt handling:

```text
prompt text -> base64 -> remote shell decodes -> agent CLI receives prompt
```

This avoids most prompt quoting breakage.

## QA Findings

### High Priority

1. `fleet-tools.ts` contains a hardcoded fallback credential for the draydev SSH command.

Impact: This is not safe for a repo that may ever be pushed or shared. Even if local-only, it normalizes secret-bearing source.

Recommended fix: require `FLEET_DRAYDEV_SSH` from env and fail with a clear configuration error if missing. Keep secrets only in ignored local config.

2. `fleet-tools.ts` interpolates `cwd` and `model` into a shell script without strong argument escaping.

Impact: A malicious or accidental `cwd` or `model` value can alter the shell command executed locally or remotely. Prompt text is base64-protected, but these fields are not.

Recommended fix: pass `cwd` and `model` through base64 or JSON env variables and shell-quote/decode them safely. Also validate `cwd` against allowed prefixes if fleet tools will be exposed broadly.

3. `fleet_status` probes EC2 with agent CLIs.

Impact: EC2 is production. The current probe is low-risk arithmetic, but it still runs commands on production.

Recommended fix: keep EC2 read-mostly. Add a config flag such as `FLEET_INCLUDE_PROD=false` by default, or make `fleet_status` default to WSL + draydev unless explicitly asked for EC2.

### Medium Priority

4. Ollama host configuration is inconsistent across tools.

Observed: AION suite and tests use `127.0.0.1:11434` from WSL. MCP `system_ollama_models` used the live env value and reported Ollama unavailable at the LAN host.

Impact: AION can work while MCP system health reports Ollama offline, which confuses operators.

Recommended fix: standardize `OLLAMA_HOST` per execution context. For WSL-run mcpbuilder, use `127.0.0.1`; for remote fleet machines, use the LAN IP.

5. `aion_query` in `chat-tools.ts` is legacy/confusing beside `aion_api_chat`.

Observed: `aion_query` targets a public API port and can fall back to OpenAI or raw Ollama without AION memory. `aion_api_chat` uses the current Flask service endpoint and service token.

Impact: Tool users may pick `aion_query` expecting current AION memory behavior, but get fallback behavior with no memory.

Recommended fix: mark `aion_query` as legacy in its description or retire it after confirming no client depends on it.

6. Dependency audit reports 7 transitive advisories through `@modelcontextprotocol/sdk`.

Observed: vulnerable packages are not imported directly by mcpbuilder source and mcpbuilder runs over stdio, lowering immediate exposure.

Recommended fix: run `npm audit fix`, rebuild, and rerun MCP smoke tests.

### Low Priority

7. Fleet output truncates at 8000 characters globally.

Impact: Good for chat safety, but code reviews or long logs can lose useful details.

Recommended fix: include a structured `truncated: true` marker and maybe a temp artifact path for long outputs.

8. MCP tool outputs are mostly JSON serialized as text.

Impact: Works, but downstream agents must parse strings instead of receiving structured MCP content.

Recommended fix: acceptable for now. Consider consistent schemas if building automated consumers.

## Benefits For Larger Development And AI Work

This architecture is valuable because it gives you three layers of leverage:

1. Persistent private intelligence.

AION can hold local memory, project context, personal preferences, and operational history. That lets your AI workflows accumulate continuity instead of restarting from scratch every session.

2. Agent specialization.

Claude, Codex, and Antigravity can be treated as callable workers. You can use one agent for implementation, another for critique, another for broad-context reasoning, and compare their outputs before acting.

3. Machine specialization.

WSL can orchestrate, draydev can absorb risky/heavy development work, Windows/Ollama can provide GPU inference, and EC2 can be probed carefully for production state. This separates concerns while keeping a single MCP interface.

For ambitious projects, this means you can build a real AI development operating system:

- fan out architecture reviews across models;
- run implementation on a disposable worker;
- keep private project memory in AION;
- use local GPU inference for fast private reasoning;
- validate production-adjacent changes with explicit tool calls;
- build repeatable QA pipelines around MCP tools instead of ad hoc manual checks.

For deep AI work, the important part is not just using more models. The important part is controlled routing: which model, which machine, which memory source, which permission level, and which verification loop. MCPBuilder is becoming the routing layer for that.

## Recommended Next Steps

1. Remove source-level SSH fallback secrets from `fleet-tools.ts`.
2. Harden shell argument handling for `cwd` and `model`.
3. Standardize Ollama host env for WSL MCP clients.
4. Run `npm audit fix`, rebuild, and rerun MCP smoke tests.
5. Add a `fleet_status` default that excludes EC2 unless explicitly requested.
6. Add a repeatable MCP QA script so this whole pass can be run with one command.
