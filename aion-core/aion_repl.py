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

The arrival/departure/gap facts and the recent-turn recap are injected into the
system prompt, so the model can naturally reference when Brian arrived, left,
and how long he was gone.

Run via the `aion` shell shortcut, or directly:
    ./.venv/bin/python aion_repl.py
"""
from __future__ import annotations

import os
import sys
import signal
import uuid
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - py<3.9
    ZoneInfo = None  # type: ignore

from aion_logging import get_logger
from config import CONFIG
from brain import get_facts

# Multi-turn chat entrypoint (routes to Ollama with think:false, OpenAI fallback).
from llm import ask_llm_chat

logger = get_logger("repl")

# Optional semantic memory — degrade gracefully if unavailable.
try:
    from memory_store import _search_memory
    _MEMORY_AVAILABLE = True
except ImportError:
    _MEMORY_AVAILABLE = False

# ChatGPT archive (auto-injected) and the Jenn/FB message archive (/msg command).
try:
    import chatgpt_store
except Exception:
    chatgpt_store = None
try:
    import messages_store
except Exception:
    messages_store = None

# How many prior history turns to replay into context on launch.
_REPLAY_TURNS = 16
# Hard cap on messages sent per request (system prompt excluded) to bound ctx.
_CONTEXT_WINDOW = 24


# ── time helpers ────────────────────────────────────────────────────────────

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _parse_ts(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _local_tz():
    name = CONFIG.get("USER_TIMEZONE", "America/New_York")
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return timezone.utc


def _fmt_local(dt: datetime) -> str:
    return dt.astimezone(_local_tz()).strftime("%A %b %d, %I:%M %p %Z")


def _humanize_gap(delta_seconds: float) -> str:
    secs = int(max(0, delta_seconds))
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    rem_m = mins % 60
    if hours < 24:
        return f"{hours}h {rem_m}m" if rem_m else f"{hours}h"
    days = hours // 24
    rem_h = hours % 24
    return f"{days}d {rem_h}h" if rem_h else f"{days}d"


# ── database access (best-effort; REPL still works stateless if DB is down) ──

class _Store:
    """Thin wrapper over the PostgreSQL history/events tables.

    Every method is defensive: if the DB is unreachable the REPL keeps working
    in a degraded (memory-less) mode rather than crashing.
    """

    def __init__(self):
        self.ok = False
        self.user_id: int | None = None
        self.username = str(CONFIG.get("primary_user", "brian"))
        self._log_event = None
        try:
            import auth
            from events import log_event
            auth.init_db()  # idempotent — CREATE TABLE IF NOT EXISTS
            self._get_db = auth.get_db
            self._log_event = log_event
            self.user_id = self._resolve_user_id()
            self.ok = self.user_id is not None
        except Exception as exc:
            logger.warning("Memory disabled — DB init failed: %s", exc)

    def _resolve_user_id(self) -> int | None:
        db = self._get_db()
        try:
            row = db.execute(
                "SELECT id FROM users WHERE username = ?", (self.username,)
            ).fetchone()
            return int(row["id"]) if row else None
        finally:
            db.close()

    def recent_turns(self, limit: int = _REPLAY_TURNS) -> list[dict]:
        """Return the last `limit` history turns (oldest first) for the user."""
        if not self.ok:
            return []
        db = self._get_db()
        try:
            rows = db.execute(
                """
                SELECT role, content, ts, session_id
                FROM history
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (self.user_id, limit),
            ).fetchall()
        except Exception as exc:
            logger.warning("history read failed: %s", exc)
            return []
        finally:
            db.close()
        return [dict(r) for r in reversed(rows)]

    def last_departure(self) -> datetime | None:
        """Timestamp of the most recent logged session_end event, if any."""
        if not self.ok:
            return None
        db = self._get_db()
        try:
            row = db.execute(
                """
                SELECT ts FROM events
                WHERE user_id = ? AND event_type = 'session_end' AND source = 'cli'
                ORDER BY id DESC LIMIT 1
                """,
                (self.user_id,),
            ).fetchone()
            return _parse_ts(row["ts"]) if row else None
        except Exception as exc:
            logger.warning("events read failed: %s", exc)
            return None
        finally:
            db.close()

    def save_turn(self, session_id: str, user_msg: str, assistant_msg: str) -> None:
        if not self.ok:
            return
        db = self._get_db()
        try:
            ts = _utc_now_iso()
            db.execute(
                """
                INSERT INTO history (user_id, role, content, ts, session_id, author_username)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (self.user_id, "user", user_msg, ts, session_id, self.username),
            )
            db.execute(
                """
                INSERT INTO history (user_id, role, content, ts, session_id, author_username)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (self.user_id, "assistant", assistant_msg, ts, session_id, "aion"),
            )
            db.commit()
        except Exception as exc:
            logger.warning("history write failed: %s", exc)
        finally:
            db.close()

    def log(self, event_type: str, session_id: str, content: str, payload=None) -> None:
        if not self.ok or self._log_event is None:
            return
        try:
            self._log_event(
                event_type=event_type,
                source="cli",
                user_id=self.user_id,
                session_id=session_id,
                content=content,
                payload=payload,
            )
        except Exception as exc:
            logger.warning("event log failed (%s): %s", event_type, exc)


# ── prompt construction ──────────────────────────────────────────────────────
#
# IMPORTANT: this model's embedded chat template rejects any request that
# contains a `system`-role message (Ollama 0.32 returns HTTP 400 while trying to
# auto-generate a tool parser from the template's system-message guard). So we
# never send a system message here. The persona lives in the Modelfile SYSTEM
# directive (Ollama injects it automatically), and dynamic context — session
# continuity + retrieved facts — is folded into the current user turn as a
# bracketed preamble that AION treats as context rather than as Brian's words.


def _continuity_block(now: datetime, last_seen: datetime | None, gap_seconds: float | None) -> str:
    lines = [f"Current time is {_fmt_local(now)}."]
    if last_seen is not None and gap_seconds is not None:
        lines.append(
            f"Brian was last active {_humanize_gap(gap_seconds)} ago "
            f"(at {_fmt_local(last_seen)}); he has just returned this session."
        )
    else:
        lines.append("No prior session on record — this looks like a first conversation.")
    return "\n".join(f"- {l}" for l in lines)


def _facts_block(user_text: str) -> str:
    try:
        facts = get_facts(user_text, k=8) or []
    except Exception:
        facts = []
    blocks = []
    if facts:
        blocks.append("Relevant facts:\n- " + "\n- ".join(facts))
    if _MEMORY_AVAILABLE and CONFIG.get("memory_enabled", True):
        try:
            msg, ok = _search_memory(user_text, limit=4)
            if ok and not msg.startswith("No memories found"):
                blocks.append(f"Remembered:\n{msg}")
        except Exception:
            pass
    # Auto-recall from the ChatGPT archive (PersonaBuilder). Best-effort.
    if chatgpt_store is not None and chatgpt_store.enabled():
        try:
            gpt_block = chatgpt_store.format_hits(chatgpt_store.search(user_text, final_limit=4))
            if gpt_block:
                blocks.append(gpt_block)
        except Exception:
            pass
    return "\n\n".join(blocks)


def _augment_user_turn(user_text: str, now: datetime, last_seen, gap_seconds,
                       include_continuity: bool) -> str:
    """Fold dynamic context into the current user message (no system role)."""
    ctx_parts = []
    if include_continuity:
        ctx_parts.append(_continuity_block(now, last_seen, gap_seconds))
    facts = _facts_block(user_text)
    if facts:
        ctx_parts.append(facts)
    if not ctx_parts:
        return user_text
    ctx = "\n\n".join(ctx_parts)
    return (
        "(Context for you, AION — background only, don't quote it back verbatim:\n"
        f"{ctx}\n)\n\n"
        f"Brian: {user_text}"
    )


# ── main REPL ─────────────────────────────────────────────────────────────────

def main() -> int:
    store = _Store()
    session_id = f"cli:{uuid.uuid4().hex[:12]}"
    started = _utc_now()

    # Figure out how long Brian was gone: prefer an explicit departure event,
    # else fall back to the timestamp of the last history turn.
    prior = store.recent_turns()
    last_departure = store.last_departure()
    last_turn_ts = _parse_ts(prior[-1]["ts"]) if prior else None
    last_seen = last_departure or last_turn_ts
    gap_seconds = (started - last_seen).total_seconds() if last_seen else None

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
            print(f"Welcome back — last active {_humanize_gap(gap_seconds)} ago.")
        else:
            print("First session on record.")
    else:
        print("(memory offline — running stateless)")
    print("Type /exit to leave, /help for commands.\n")

    def _depart(*_):
        duration = (_utc_now() - started).total_seconds()
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
                print(f"AION: last saw you {_humanize_gap(gap_seconds)} ago, at {_fmt_local(last_seen)}.\n")
            else:
                print("AION: no prior session on record.\n")
            continue
        if low.startswith("/msg"):
            q = user_input[4:].strip()
            if not q:
                print("Usage: /msg <search terms>  (searches the Jenn/FB message archive)\n")
                continue
            if messages_store is None or not messages_store.db_exists():
                print("AION: the message archive (messages.db) isn't available here.\n")
                continue
            blocks = messages_store.search_threads(query=q, max_threads=4, max_per_thread=8)
            if not blocks:
                print(f"AION: nothing in the message archive matches '{q}'.\n")
                continue
            # Feed the real threads to AION and let it answer, folded into a user
            # turn (no system role). Store a clean marker in history.
            ctx = "\n\n".join(blocks)
            aug = (
                "(Message-archive threads matching the search — these are real logged "
                f"messages; use them to answer:\n{ctx}\n)\n\n"
                f"Brian: Walk me through these messages about \"{q}\"."
            )
            conversation.append({"role": "user", "content": f"/msg {q}"})
            send = [dict(m) for m in conversation[-_CONTEXT_WINDOW:]]
            while send and send[0]["role"] == "assistant":
                send.pop(0)
            send[-1]["content"] = aug
            try:
                answer = (ask_llm_chat(send) or "").strip() or "(no response)"
            except Exception as exc:
                print(f"AION (error): {exc}\n")
                conversation.pop()
                continue
            print(f"AION: {answer}\n")
            conversation.append({"role": "assistant", "content": answer})
            store.save_turn(session_id, f"/msg {q}", answer)
            first_turn = False
            continue

        # Store the raw turn (clean history), but send an augmented copy with
        # continuity/facts folded into the user message. No system role — this
        # model 400s on system messages (see prompt-construction note above).
        conversation.append({"role": "user", "content": user_input})
        window = [dict(m) for m in conversation[-_CONTEXT_WINDOW:]]
        while window and window[0]["role"] == "assistant":
            window.pop(0)
        window[-1]["content"] = _augment_user_turn(
            user_input, _utc_now(), last_seen, gap_seconds, include_continuity=first_turn
        )

        try:
            answer = (ask_llm_chat(window) or "").strip() or "(no response)"
        except Exception as exc:
            print(f"AION (error): {exc}\n")
            # Drop the dangling user turn so it isn't double-counted next time.
            conversation.pop()
            continue
        first_turn = False

        print(f"AION: {answer}\n")
        conversation.append({"role": "assistant", "content": answer})
        store.save_turn(session_id, user_input, answer)


if __name__ == "__main__":
    sys.exit(main())
