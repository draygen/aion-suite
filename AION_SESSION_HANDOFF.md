# AION — Session Handoff (2026-08-01)

Complete record of the changes made this session, for continuing with Codex.
Spans **three repos**. Everything in `aion-suite` is committed to branch
`feat/aion-hauhau-uncensored-model` and pushed to `origin`.

---

## 0. The three repos and how they relate

| Repo | Path | Role | Git |
|---|---|---|---|
| **aion-suite** | `/mnt/c/projects/aion-suite` (canonical) | AION itself — CLI, local API, engine, tools, RAG | branch `feat/aion-hauhau-uncensored-model`, pushed |
| **aion-chat** | `/mnt/c/projects/aion-chat` | WinUI 3 desktop client (C#/.NET 8) that talks to AION's local API | `main`, local only (no remote yet) |
| **hermes-aion** | `/mnt/c/projects/hermes-aion` | Hermes agent worker + FastAPI adapter AION delegates tasks to | `master` + branch `feat/desktop-action-repair`, pushed |

> ⚠️ **`/projects/aion-suite` (ext4) is a STALE second clone.** It has a
> `STALE.md`. Do NOT edit code there. Canonical is `/mnt/c/projects/aion-suite`.
> The `aion` shell function in `~/.bashrc` and the desktop launcher both point
> at `/mnt/c` now.

---

## 1. Runtime topology (how a message flows)

```
WinUI desktop (aion-chat, AionChat.exe)
   │  http://127.0.0.1:8770   (NOT localhost — Windows resolves ::1 first; WSL binds IPv4)
   ▼
aion_api.py  (FastAPI, loopback only, no auth by design)
   ├─ POST /api/action     ← desktop calls this FIRST every message
   │     repair → safe-diagnostics → WEB SEARCH → HERMES DELEGATION → handled:false
   ├─ POST /api/chat/stream ← falls through here (pure LLM) when action handled:false
   ├─ POST /api/chat, /api/msg, GET /api/health, /api/threads
   ▼
aion_engine.py  (shared turn logic — used by BOTH aion_api.py and aion_repl.py)
   ├─ maybe_web_search()      → tools.run_firecrawl_search() (DIRECT, reliable)
   ├─ maybe_delegate_action() → tools.run_hermes_delegate()  → Hermes adapter :8722
   ├─ build_messages()        → persona system msg + context-folded user turn
   └─ ask_llm_chat() / stream_llm_chat()  → Ollama :11434
   ▼
Ollama :11434
   main model: hf.co/mradermacher/Qwen3.5-9B-Claude-4.6-...HERETIC-UNCENSORED:Q8_0
              (set in config_local.py, overrides config.py "aion-hauhau")

Hermes adapter (hermes-aion/integrations/aion-hermes, :8722)
   └─ hermes -z -m hermes-aion-llama:latest -t terminal,file,firecrawl
        worker model: hermes-aion-llama (Llama 3.1 8B) → its own Ollama call
```

**Start commands**
```bash
# AION API (WSL):
cd /mnt/c/projects/aion-suite/aion-core
./.venv/bin/uvicorn aion_api:app --host 127.0.0.1 --port 8770

# Hermes adapter:
cd /mnt/c/projects/hermes-aion && bash scripts/start-hermes.sh   # guards on port 8722

# Terminal REPL:  aion   (bash function) or ./.venv/bin/python aion_repl.py
# Desktop:        C:\projects\aion-chat\artifacts\AionChat-win-x64\start-aion.cmd
```

---

## 2. This session's commits (newest first)

### aion-suite  (branch `feat/aion-hauhau-uncensored-model`, all pushed)
```
eebf559  feat: reliable web search via AION's own firecrawl_search (not the worker)
0234c7b  fix: give the Hermes worker the firecrawl toolset (correct name)
048983a  feat: AION auto-delegates "do it on my machine" requests to Hermes
c3ca84f  fix: kill trailing helpdesk filler in AION replies
fe061cc  feat: wire AION → Hermes worker delegation (the missing client side)
e4a67c7  feat: retune AION persona — funny and likeable, never insulting; protect Jenn
507b3df  feat: give AION a real persona; fix greeting misrouting and "Response:" headers
f091240  feat: merge desktop action/repair layer forward onto the engine refactor
dae171f  feat: finish engine refactor, wire REPL to it, make the API runnable
a2613ee  feat: extract shared AION conversation engine + local FastAPI backend
```
### hermes-aion
```
9a82ca1  tune: clamp hermes-aion-llama sampling + add no-fabricate directive   (master)
76bb8c2  feat: desktop /api/action — safe diagnostics + repairs (branch feat/desktop-action-repair, pushed)
```
### aion-chat
```
556f4f6  chore: version the client at 1.0.1
c92b278  chore: track the WinUI client in git
```

---

## 3. Key files & what changed

### `aion-core/aion_engine.py`  (the heart — shared by REPL + API)
- `Store(source="cli"|"api")` — Postgres history/threads/events; `source` scopes
  events + `last_departure()` so CLI vs desktop don't clobber each other's "last seen".
- `session_last_seen(store, prior=None)` — cross-thread "when was Brian last here";
  fixes a bug where a NEW thread always said "no prior session".
- `build_messages(...)` — assembles the turn. **Prepends persona as a `system`
  message** when `CONFIG["persona_as_system_message"]` is True. No system role is
  sent when False (the ChatML `aion-hauhau` model 400s on system role; the raw
  HF model in use accepts it).
- `clean_reply(text)` — strips leading `Response:`/`AION:`/`**Response:**` labels
  and a stray leading `---`. Applied on every reply path.
- `maybe_web_search(user_text)` — if `tools.detect_web_search` matches, runs
  `tools.run_firecrawl_search` (DIRECT) and grounds the model in the real hits.
- `maybe_delegate_action(user_text)` — if `tools.looks_like_machine_action`, sends
  a directive objective to the Hermes worker; returns None if Hermes down (chat answers).
- `chat()` order: **web search → hermes delegation → normal LLM.**

### `aion-core/aion_api.py`  (FastAPI, loopback :8722… no, :8770)
- `/api/health` returns `ollama`, `model_loaded` (via /api/ps, 0.75s), `backend`,
  `keep_alive`, `capabilities{}`, `messages_archive` — the fields `BackendHealth`
  in the C# client reads.
- `/api/action` pipeline: repair → `dispatch_safe_diagnostic_message` → `maybe_web_search`
  → `maybe_delegate_action` → `handled:false`. Returns `{handled,kind,reply,requires_confirmation}`.
  `kind` ∈ `repair|diagnostic|web_search|delegated|None`.
- `/api/chat/stream` SSE: buffers first ~24 chars to strip a leaked label before first paint.
- `stream_llm_chat` shared from `llm.py` (no OpenAI fallback mid-stream by design).

### `aion-core/aion_repl.py`
- Now imports `aion_engine` instead of its own copies (was 456 lines → 173).
- Same web-search / hermes-delegation short-circuits before the LLM.

### `aion-core/llm.py`
- `_ollama_payload()` shared by `_ollama_chat` + new `stream_llm_chat()`.
- `OllamaResponseError` surfaces 4xx instead of masking as unreachable.

### `aion-core/tools.py`  (the tool registry + new capabilities)
- **Hermes client:** `run_hermes_delegate(objective, timeout)`, `hermes_available()`,
  `_hermes_url()`, `build_hermes_objective()` (directive wrap + C:\→/mnt/c note).
  Registered tool `hermes_delegate` (explicit "delegate:/hermes:/worker:/have hermes to").
- **Machine-action intent:** `looks_like_machine_action(text)` — action VERB +
  SYSTEM target, minus how-to/explain. Feeds auto-delegation.
- **Web-search intent:** `detect_web_search(text)` — "search the web for X",
  "google X", "look up X online", "what's the latest on X". Excludes /msg & chat.
- **Firecrawl direct (pre-existing, 977f38c):** `run_firecrawl_search`,
  `run_firecrawl_scrape`, `_firecrawl_key()`, `_firecrawl_post()`.
- **Safe diagnostics (from f091240 merge):** `SAFE_LOCAL_DIAGNOSTIC_TOOL_IDS`
  {dig,nmap_ping_sweep,nslookup,ping,traceroute}, `dispatch_safe_diagnostic_message`,
  `ToolRuntimeError`.

### `aion-core/config.py`  (config keys added this session)
```python
AION_PERSONA = """...funny, likeable, opinionated, NEVER insulting;
                  dedicated Jenn-protection section..."""
"persona": AION_PERSONA,
"persona_as_system_message": True,     # False if switching to aion-hauhau model
"hermes_enabled": True,                # env HERMES_ENABLED
"hermes_adapter_url": "http://127.0.0.1:8722",
"hermes_default_timeout": 600,
"hermes_toolsets": "terminal,file,firecrawl",   # NOTE: firecrawl, NOT mcp-firecrawl
# (firecrawl_* keys pre-existed: firecrawl_enabled, firecrawl_api_key, firecrawl_search_limit)
```
- Real model is set in `config_local.py` (gitignored): `"model": "hf.co/mradermacher/Qwen3.5-9B-Claude-4.6-...Q8_0"`.

### `aion-core/repair.py` + `test_repair.py`  (from f091240)
- Approval-gated repair proposals for failed tools; used by `/api/action`.

### hermes-aion `ollama/Modelfile.hermes-aion-llama`  (9a82ca1)
- Added `temperature 0.2 / top_p 0.9 / top_k 20 / repeat_penalty 1.1` + no-fabricate
  SYSTEM. Rebuild: `ollama create hermes-aion-llama:latest -f ollama/Modelfile.hermes-aion-llama`.

### aion-chat (C#)
- `MainWindow.xaml.cs` `EnsureBackend()` + all 4 `start-aion.cmd` copies now start
  `/mnt/c/...` on `--host 127.0.0.1` (was `/projects/...` on `0.0.0.0` — a LAN
  exposure of the unauthenticated API).
- `AionChat.csproj` has `<Version>1.0.1</Version>` (drives filename + assembly).
- Now a git repo; `bin/ obj/ artifacts/` gitignored.
- Rebuild: `dotnet publish AionChat.csproj -c Release -r win-x64 -p:Platform=x64 --self-contained true`
  then repackage into `artifacts/`. Latest artifact: `artifacts/AionChat-win-x64-v1.0.1.zip`.

---

## 4. Persona (config.py `AION_PERSONA`) — behaviour contract

- Funny, likeable, opinionated, direct — **never insulting/mean** (an earlier
  "blunt to the point of insulting" version made a cruel joke about Jenn; removed).
- **Jenn (late wife) section is a hard rule:** never a joke/punchline, never raised
  to explain his mood, never speculated about unprompted; if he brings her up, warmth
  over cleverness. **Do not weaken this.**
- No opening filler ("Great question"), **no trailing helpdesk offers** in any form —
  including the question form ("Want me to…?", "Anything else?"). End on substance.
- Never prefix a reply with a label. Addresses him Brian / draygen.
- Knows his world (Lowell MA; security/pentest, Linux/Unix/Windows/DOS, cooking/chef,
  demoscene, music, art) — for register, not to recite back.
- Delivered as a **system message** because the running raw HF model accepts one.

---

## 5. Intent routing (how a turn is classified, in order)

1. **Web search** — `detect_web_search()` → AION's own firecrawl (reliable, grounded). ✅ works
2. **Machine action** — `looks_like_machine_action()` → Hermes worker. ✅ single-command works
3. **Explicit delegate** — `hermes_delegate` tool ("delegate: …"). ✅
4. **Safe diagnostics** — ping/nmap/dig/etc via `/api/action`. ✅
5. Otherwise → normal LLM chat.

Detectors are **heuristics** (verb+target regex, testable), not an LLM classifier.
They pass all current tests but a wildly novel phrasing could slip. Upgrade path:
a small LLM confirm on ambiguous messages (deliberately deferred).

---

## 6. Known issues / open items  (candidates for Codex)

1. **Hermes worker fabricates on multi-step / MCP-tool tasks.** Root cause is
   NOT temperature (already tuned) — the worker (Llama 3.1 8B) emits tool calls
   as **text JSON in content** instead of the API `tool_calls` field, and Hermes
   strips those. Single-command terminal tasks work; firecrawl/web via the worker
   does not. **This is why web search routes to AION's OWN firecrawl, not the worker.**
   - Next lever: try **`qwen3:14b`** as the worker (already in Ollama, native
     tool+thinking). Tradeoff: 9.3GB vs the main AION Q8 (~10GB) on a 12GB card —
     they evict each other → reload latency. Brian's call.
   - Or investigate how Hermes presents MCP tool schemas over its OpenAI `/v1` path.
2. **Doc bug in hermes-aion:** `AION-INTEGRATION.md` / `.env.example` say the
   firecrawl toolset is `mcp-firecrawl`; Hermes actually ignores that name
   ("unknown --toolsets entries"). Correct name is **`firecrawl`** (`hermes tools list`).
   The docs still say the wrong thing — worth fixing in that repo.
3. **21 pre-existing test failures** in `test_web_chat.py` + `test_web_security.py`
   — all `AttributeError: module 'auth' has no attribute 'DB_PATH'` (Postgres auth
   migration fallout, predates this session). `aion-core/CLAUDE.md` still says "two
   pre-existing failures" — it's 21 now; update it.
4. **Worker reply voice:** the Hermes worker has its own SYSTEM prompt (task agent),
   so delegated replies don't match AION's persona and can add helpdesk-y tails.
   Fine for a task worker; if it matters, post-process delegated output through
   `clean_reply` or an AION rephrase.
5. **`/api/chat/stream` doesn't run tool routing** (only `/api/action` does). The
   desktop hits `/api/action` first so it's covered, but a stream-only client
   would miss web-search/delegation. Consider unifying if you add other clients.
6. **aion-chat has no git remote** — local only. Push it somewhere if you want it backed up.

---

## 7. How to run the tests

```bash
cd /mnt/c/projects/aion-suite/aion-core
./.venv/bin/python -m unittest discover -s . -p 'test_*.py'
# Expect: 173 tests, 21 pre-existing errors (test_web_chat / test_web_security).
# This session's suites (all green): test_aion_engine, test_aion_api, test_tools, test_repair
```

New test files/classes this session:
- `test_aion_engine.py`: TestBuildMessages, TestSessionLastSeen, TestPersona,
  TestCleanReply, TestActionDelegation, TestWebSearch, TestStoreSourceScoping.
- `test_tools.py`: TestHermesDelegation, TestMachineActionDetection, TestWebSearchDetection.
- `test_aion_api.py`, `test_repair.py` (from the f091240 merge).

---

## 8. Live-verified this session (so you know what actually works)

- Persona: "Hi there" → warm/funny, no Jenn reference, no "Response:" header, no filler.
- Continuity: new thread correctly reports cross-thread gap.
- Web search (desktop `/api/action`): "search the web for latest python" → **real 3.14.6, python.org cited**, ~17s.
- Machine action (desktop `/api/action`): "check my disk space on C:\" → Hermes ran `df`, real numbers, ~20s.
- Disk units gotcha: `df -h` "272G" = 272 **GiB** = ~291.5 **GB** (Windows Explorer shows the decimal 291).
- Hermes web via worker: **fabricated** 3× (stale versions) — do not trust it; use AION's direct firecrawl.

---

## 9. Gotchas that will bite you

- Use **`127.0.0.1`**, never `localhost`, for WSL↔Windows (IPv6 ::1 vs IPv4 bind).
- Keep the API **loopback-only** — no auth by design; `0.0.0.0` leaks chat history + FB archive to the LAN.
- Hermes firecrawl toolset name is **`firecrawl`** not `mcp-firecrawl`.
- `persona_as_system_message` must be **False** if you switch `model` back to `aion-hauhau` (it 400s on system role).
- The real model is in **`config_local.py`** (gitignored), not `config.py`.
- `df -h` is base-1024 (GiB labeled "G"); Windows Explorer is base-1000 (GB).
- Background `pkill -f uvicorn` can match its own shell — use `pgrep`/PID or a bracket pattern.
