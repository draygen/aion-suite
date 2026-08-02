"""Session- and presence-aware AION CLI.

Unlike app.py (stateless, single-turn), this REPL wires the terminal chat into
the PostgreSQL `history` and `events` tables so AION has continuity:

  * On launch it resolves the primary user, opens a session, and logs an
    `session_start` (arrival) event with a UTC timestamp.
  * It reads the tail of `history` to know when Brian was last seen, computes
    how long he was gone, and replays recent turns so AION remembers what was
    said between sessions.
  * Every turn is persisted to `history` (user + assistant, with ts +
    session_id) and kept in an in-memory context window for coherent
    multi-turn conversation within the session.
  * On exit (command / EOF / Ctrl-C / SIGTERM) it logs a `session_end`
    (departure) event, so the *next* launch can report how long he was away.

The arrival/departure/gap facts and retrieved memory are folded into the current
user turn (never a `system` message — the aion-hauhau chat template 400s on
those), so the model can naturally reference when Brian arrived, left, and how
long he was gone. All of that turn-building lives in aion_engine.

Run via the `aion` shell shortcut, or directly:
    ./.venv/bin/python aion_repl.py
"""
from __future__ import annotations

import sys
import signal

from aion_logging import get_logger

# Turn-building, retrieval and history all live in the shared engine, which the
# local HTTP API (aion_api.py) drives too — keep behaviour changes in there.
import aion_engine as engine
from aion_engine import fmt_local, humanize_gap, parse_ts, utc_now

# Multi-turn chat entrypoint (routes to Ollama with think:false, OpenAI fallback).
from llm import ask_llm_chat

logger = get_logger("repl")

# How many prior history turns to replay into context on launch.
_REPLAY_TURNS = 16


# ── main REPL ─────────────────────────────────────────────────────────────────

def main() -> int:
    store = engine.Store(source="cli")
    session_id = engine.new_thread_id("cli")
    started = utc_now()

    # Figure out how long Brian was gone: prefer an explicit departure event,
    # else fall back to the timestamp of the last history turn.
    prior = store.recent_turns(_REPLAY_TURNS)
    last_seen, gap_seconds = engine.session_last_seen(store, prior=prior)

    # Seed the rolling context with the tail of prior conversation so AION
    # actually remembers what was said between sessions. Drop any leading
    # assistant turns so the replay starts on a user message.
    conversation: list[dict] = [
        {"role": r["role"], "content": r["content"]}
        for r in prior
        if r.get("role") in ("user", "assistant") and r.get("content")
    ]
    while conversation and conversation[0]["role"] == "assistant":
        conversation.pop(0)

    first_turn = True

    store.log("session_start", session_id, "Brian arrived (CLI)",
              payload={"gap_seconds": gap_seconds, "started": started.isoformat()})

    # Greeting
    print("AION online.", end=" ")
    if store.ok:
        if last_seen is not None:
            print(f"Welcome back — last active {humanize_gap(gap_seconds)} ago.")
        else:
            print("First session on record.")
    else:
        print("(memory offline — running stateless)")
    print("Type /exit to leave, /help for commands.\n")

    def _depart(*_):
        duration = (utc_now() - started).total_seconds()
        store.log("session_end", session_id, "Brian left (CLI)",
                  payload={"duration_seconds": duration})
        print("\nAION: later, Brian.")
        sys.exit(0)

    # Log departure on kill/terminate too, not just clean exit.
    try:
        signal.signal(signal.SIGTERM, _depart)
    except (ValueError, OSError):
        pass

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            _depart()

        if not user_input:
            continue
        low = user_input.lower()
        if low in ("/exit", "exit", "/quit", "quit"):
            _depart()
        if low == "/help":
            print("Commands:\n"
                  "  /exit                leave (logs your departure)\n"
                  "  /whenlast            when you were last here\n"
                  "  /msg <search terms>  search your Jenn/FB message archive\n"
                  "  /help                this list\n")
            continue
        if low == "/whenlast":
            if last_seen:
                print(f"AION: last saw you {humanize_gap(gap_seconds)} ago, at {fmt_local(last_seen)}.\n")
            else:
                print("AION: no prior session on record.\n")
            continue
        if low.startswith("/msg"):
            q = user_input[4:].strip()
            if not q:
                print("Usage: /msg <search terms>  (searches the Jenn/FB message archive)\n")
                continue
            if not engine.messages_available():
                print("AION: the message archive (messages.db) isn't available here.\n")
                continue
            blocks = engine.search_messages(q)
            if not blocks:
                print(f"AION: nothing in the message archive matches '{q}'.\n")
                continue
            # Feed the real threads to AION and let it answer, folded into a user
            # turn (no system role). Store a clean marker in history.
            label = engine.msg_history_label(q)
            send = engine.build_messages(conversation, label,
                                         augmented=engine.msg_context_turn(q, blocks))
            try:
                answer = engine.clean_reply((ask_llm_chat(send) or "").strip()) or "(no response)"
            except Exception as exc:
                print(f"AION (error): {exc}\n")
                continue
            print(f"AION: {answer}\n")
            conversation.append({"role": "user", "content": label})
            conversation.append({"role": "assistant", "content": answer})
            store.save_turn(session_id, label, answer)
            first_turn = False
            continue

        # "Do it on my machine" requests go to the Hermes worker instead of the
        # LLM, so AION performs the action rather than explaining it.
        delegated = engine.maybe_delegate_action(user_input)
        if delegated is not None:
            print(f"AION: {delegated}\n")
            conversation.append({"role": "user", "content": user_input})
            conversation.append({"role": "assistant", "content": delegated})
            store.save_turn(session_id, user_input, delegated)
            first_turn = False
            continue

        # Send an augmented copy with continuity/facts folded into the user
        # message, but keep the raw turn in history and in the rolling context.
        window = engine.build_messages(
            conversation, user_input, include_continuity=first_turn,
            last_seen=last_seen, gap_seconds=gap_seconds,
        )

        try:
            answer = engine.clean_reply((ask_llm_chat(window) or "").strip()) or "(no response)"
        except Exception as exc:
            print(f"AION (error): {exc}\n")
            # Nothing was appended yet, so the failed turn leaves no trace.
            continue
        first_turn = False

        print(f"AION: {answer}\n")
        conversation.append({"role": "user", "content": user_input})
        conversation.append({"role": "assistant", "content": answer})
        store.save_turn(session_id, user_input, answer)


if __name__ == "__main__":
    sys.exit(main())
