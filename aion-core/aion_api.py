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

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import aion_engine as engine
from config import CONFIG
from llm import stream_llm_chat

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


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "memory": _store.ok,
        "model": CONFIG.get("model"),
        "user": _store.username,
        "messages_archive": engine.messages_available(),
    }


@app.get("/api/threads")
def threads():
    return {"threads": _store.list_threads()}


@app.get("/api/threads/{thread_id}")
def thread(thread_id: str):
    return {"thread_id": thread_id, "messages": _store.thread_history(thread_id)}


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
