"""Local single-user HTTP API for AION — the backend the WinUI app talks to.

Wraps aion_engine so the desktop client gets the full feature set (session
memory, curated + semantic + ChatGPT-archive recall, /msg message search).
Bind to localhost only; there is no auth by design (personal, single-user).

Run:
    ./.venv/bin/uvicorn aion_api:app --host 127.0.0.1 --port 8770
"""
from __future__ import annotations

import json
import logging

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import aion_engine as engine
from config import CONFIG
from llm import stream_llm_chat
from ollama_endpoints import ollama_base_urls
from repair import handle_repair_command, propose_repair_for_failure
from tools import ToolRuntimeError, dispatch_safe_diagnostic_message

logger = logging.getLogger("aion.api")

app = FastAPI(title="AION local API", version="1.0")

# The WinUI client is a local desktop app (file:// / no origin); allow all —
# the server only listens on localhost so this isn't an exposure.
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# One shared Store for the process (connection pool underneath). Events are
# tagged source="api" so the CLI's "when did you last leave" stays CLI-only.
_store = engine.Store(source="api")


class ChatIn(BaseModel):
    message: str
    thread_id: str | None = None


class MsgIn(BaseModel):
    query: str
    thread_id: str | None = None


class ActionIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str = Field(default="desktop", min_length=1, max_length=128,
                            pattern=r"^[A-Za-z0-9:._-]+$")


def _ollama_runtime_status() -> dict:
    """Is Ollama up, and is the configured model already resident?

    Short timeout: this runs on every health poll and the client renders a
    status dot from it — a hung probe must not stall the UI.
    """
    configured_model = str(CONFIG.get("model") or "")
    for base_url in ollama_base_urls():
        try:
            response = requests.get(f"{base_url}/api/ps", timeout=0.75)
            if response.status_code >= 400:
                continue
            models = response.json().get("models") or []
            loaded_names = {
                str(item.get("name") or item.get("model") or "")
                for item in models
                if isinstance(item, dict)
            }
            return {"reachable": True, "model_loaded": configured_model in loaded_names}
        except (requests.RequestException, ValueError):
            continue
    return {"reachable": False, "model_loaded": False}


@app.get("/api/health")
def health():
    runtime = _ollama_runtime_status()
    return {
        "ok": True,
        "memory": _store.ok,
        "model": CONFIG.get("model"),
        "backend": CONFIG.get("backend", "ollama"),
        "ollama": runtime["reachable"],
        "model_loaded": runtime["model_loaded"],
        "keep_alive": CONFIG.get("llm_keep_alive", "30m"),
        "user": _store.username,
        "messages_archive": engine.messages_available(),
        "capabilities": {
            "streaming": True,
            "memory": _store.ok,
            "safe_diagnostics": True,
            "repair_proposals": True,
        },
    }


@app.get("/api/threads")
def threads():
    return {"threads": _store.list_threads()}


@app.get("/api/threads/{thread_id}")
def thread(thread_id: str):
    return {"thread_id": thread_id, "messages": _store.thread_history(thread_id)}


@app.post("/api/action")
def action(body: ActionIn):
    """Handle AION-only local diagnostics and explicit repair decisions.

    The diagnostic dispatcher exposes only the read-only safe subset. Repair
    proposals are read-only until the user returns the session-bound approval
    token emitted by the existing repair workflow. `handled: false` means the
    text was ordinary chat — the client falls through to streaming.
    """
    repair = handle_repair_command(body.message, username=_store.username,
                                   session_id=body.session_id)
    if repair:
        return {"handled": True, "kind": "repair", "reply": repair.response,
                "requires_confirmation": repair.staged}

    try:
        execution = dispatch_safe_diagnostic_message(body.message, "127.0.0.1")
    except ToolRuntimeError as exc:
        repair = propose_repair_for_failure(str(exc), tool_id=exc.tool_id,
                                            username=_store.username,
                                            session_id=body.session_id)
        if repair:
            return {"handled": True, "kind": "repair", "reply": repair.response,
                    "requires_confirmation": repair.staged}
        return {
            "handled": True,
            "kind": "diagnostic",
            "reply": (f"{exc.label} failed locally ({exc.error_type}). "
                      "No conservative repair recipe matched; no changes were made."),
            "requires_confirmation": False,
        }

    if execution:
        return {"handled": True, "kind": "diagnostic", "reply": execution.output,
                "requires_confirmation": False}
    return {"handled": False, "kind": None, "reply": None, "requires_confirmation": False}


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.post("/api/chat")
def chat(body: ChatIn):
    """Non-streaming turn (simple; handy for testing)."""
    thread_id = body.thread_id or engine.new_thread_id("app")
    is_new = body.thread_id is None
    try:
        reply = engine.chat(thread_id, body.message, store=_store, include_continuity=is_new)
    except Exception as exc:
        logger.warning("/api/chat failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))
    return {"thread_id": thread_id, "reply": reply}


@app.post("/api/chat/stream")
def chat_stream(body: ChatIn):
    """Streaming turn as Server-Sent Events. Emits `meta` (thread_id), then
    `token` events, then `done`. Persists the full turn on completion."""
    thread_id = body.thread_id or engine.new_thread_id("app")
    is_new = body.thread_id is None
    prior = _store.thread_history(thread_id)
    # Continuity only on the opening turn of a new chat, and measured across
    # threads — a fresh thread has no history of its own to measure against.
    last_seen = gap = None
    if is_new:
        last_seen, gap = engine.session_last_seen(_store)
    messages = engine.build_messages(prior, body.message, include_continuity=is_new,
                                     last_seen=last_seen, gap_seconds=gap)

    def gen():
        yield _sse("meta", {"thread_id": thread_id})
        chunks: list[str] = []
        try:
            for tok in stream_llm_chat(messages):
                chunks.append(tok)
                yield _sse("token", {"t": tok})
        except Exception as exc:
            logger.warning("stream failed on thread %s: %s", thread_id, exc)
            # Persist whatever arrived before the break so the thread isn't left
            # with a user turn and no reply.
            partial = "".join(chunks).strip()
            if partial:
                _store.save_turn(thread_id, body.message, partial)
            yield _sse("error", {"error": str(exc), "partial": bool(partial)})
            return
        reply = "".join(chunks).strip() or "(no response)"
        _store.save_turn(thread_id, body.message, reply)
        yield _sse("done", {"thread_id": thread_id})

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/msg")
def msg(body: MsgIn):
    """Search the Jenn/FB message archive and have AION walk the threads.

    With a `thread_id` the exchange joins that conversation and is persisted
    (as the bare `/msg <query>` marker, not the whole archive dump); without
    one it's a stateless lookup.
    """
    if not engine.messages_available():
        raise HTTPException(status_code=503, detail="Message archive (messages.db) not available.")
    blocks = engine.search_messages(body.query)
    if not blocks:
        return {"found": False, "threads": [],
                "reply": f"Nothing in the message archive matches '{body.query}'."}

    label = engine.msg_history_label(body.query)
    prior = _store.thread_history(body.thread_id) if body.thread_id else []
    messages = engine.build_messages(prior, label,
                                     augmented=engine.msg_context_turn(body.query, blocks))
    try:
        reply = (engine.ask_llm_chat(messages) or "").strip() or "(no response)"
    except Exception as exc:
        logger.warning("/api/msg failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))
    if body.thread_id:
        _store.save_turn(body.thread_id, label, reply)
    return {"found": True, "reply": reply, "threads": blocks, "thread_id": body.thread_id}
