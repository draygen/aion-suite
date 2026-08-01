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
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import aion_engine as engine
from config import CONFIG
from ollama_endpoints import ollama_base_urls

logger = logging.getLogger("aion.api")

app = FastAPI(title="AION local API", version="1.0")

# The WinUI client is a local desktop app (file:// / no origin); allow all —
# the server only listens on localhost so this isn't an exposure.
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# One shared Store for the process (connection pool underneath).
_store = engine.Store()


class ChatIn(BaseModel):
    message: str
    thread_id: str | None = None


class MsgIn(BaseModel):
    query: str


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "memory": _store.ok,
        "model": CONFIG.get("model"),
        "user": _store.username,
    }


@app.get("/api/threads")
def threads():
    return {"threads": _store.list_threads()}


@app.get("/api/threads/{thread_id}")
def thread(thread_id: str):
    return {"thread_id": thread_id, "messages": _store.thread_history(thread_id)}


@app.post("/api/chat")
def chat(body: ChatIn):
    """Non-streaming turn (simple; handy for testing)."""
    thread_id = body.thread_id or engine.new_thread_id("app")
    is_new = body.thread_id is None
    reply = engine.chat(thread_id, body.message, store=_store, include_continuity=is_new)
    return {"thread_id": thread_id, "reply": reply}


def _ollama_stream(messages: list[dict]):
    """Stream content tokens from Ollama for the given messages (think:false)."""
    payload = {
        "model": CONFIG.get("model"),
        "messages": messages,
        "stream": True,
        "keep_alive": CONFIG.get("llm_keep_alive", "30m"),
        "think": bool(CONFIG.get("llm_think", False)),
    }
    options = CONFIG.get("llm_options")
    if options:
        payload["options"] = options
    last_error = None
    for base_url in ollama_base_urls():
        try:
            with requests.post(f"{base_url}/api/chat", json=payload, stream=True, timeout=300) as resp:
                if resp.status_code >= 400:
                    raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                for line in resp.iter_lines():
                    if not line:
                        continue
                    data = json.loads(line)
                    tok = data.get("message", {}).get("content", "")
                    if tok:
                        yield tok
                    if data.get("done"):
                        return
            return
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_error = exc
            continue
    raise RuntimeError(f"Ollama unreachable. Last error: {last_error}")


@app.post("/api/chat/stream")
def chat_stream(body: ChatIn):
    """Streaming turn as Server-Sent Events. Emits `meta` (thread_id), then
    `token` events, then `done`. Persists the full turn on completion."""
    thread_id = body.thread_id or engine.new_thread_id("app")
    is_new = body.thread_id is None
    prior = _store.thread_history(thread_id)
    last_seen = gap = None
    if is_new and prior:
        last_seen = engine.parse_ts(prior[-1]["ts"])
        gap = (engine.utc_now() - last_seen).total_seconds() if last_seen else None
    messages = engine.build_messages(prior, body.message, include_continuity=is_new,
                                     last_seen=last_seen, gap_seconds=gap)

    def gen():
        yield f"event: meta\ndata: {json.dumps({'thread_id': thread_id})}\n\n"
        chunks: list[str] = []
        try:
            for tok in _ollama_stream(messages):
                chunks.append(tok)
                yield f"event: token\ndata: {json.dumps({'t': tok})}\n\n"
        except Exception as exc:
            yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"
            return
        reply = "".join(chunks).strip() or "(no response)"
        _store.save_turn(thread_id, body.message, reply)
        yield f"event: done\ndata: {json.dumps({'thread_id': thread_id})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/msg")
def msg(body: MsgIn):
    """Search the Jenn/FB message archive and have AION walk the threads."""
    blocks = engine.search_messages(body.query)
    if not blocks:
        return {"found": False, "reply": f"Nothing in the message archive matches '{body.query}'."}
    ctx = "\n\n".join(blocks)
    aug = ("(Message-archive threads matching the search — real logged messages; "
           f"use them to answer:\n{ctx}\n)\n\n"
           f"Brian: Walk me through these messages about \"{body.query}\".")
    reply = (engine.ask_llm_chat([{"role": "user", "content": aug}]) or "").strip()
    return {"found": True, "reply": reply or "(no response)", "threads": blocks}
