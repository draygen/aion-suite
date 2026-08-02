"""Shared AION conversation engine.

The single source of truth for how AION builds a turn — retrieval (curated
facts + semantic memory + ChatGPT-archive auto-recall), context augmentation,
and PostgreSQL-backed history/threads — used by BOTH the terminal REPL
(aion_repl.py) and the local HTTP API (aion_api.py).

Design rules:
  * No `system`-role message is ever produced — the aion-hauhau chat template
    400s on system messages, so persona lives in the Modelfile SYSTEM and
    dynamic context is folded into the user turn.
  * Every DB call is best-effort; if Postgres is down the engine still answers,
    just without memory.
  * A "thread" is a history `session_id`. The GUI shows one thread per session.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

from config import CONFIG
from brain import get_facts
from llm import ask_llm_chat

logger = logging.getLogger("aion.engine")

# Optional subsystems — degrade gracefully.
try:
    from memory_store import _search_memory
    _MEMORY_AVAILABLE = True
except Exception:
    _MEMORY_AVAILABLE = False
try:
    import chatgpt_store
except Exception:
    chatgpt_store = None
try:
    import messages_store
except Exception:
    messages_store = None

CONTEXT_WINDOW = 24  # max prior turns sent to the model


# ── time helpers ──────────────────────────────────────────────────────────────

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def parse_ts(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def local_tz():
    name = CONFIG.get("USER_TIMEZONE", "America/New_York")
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return timezone.utc


def fmt_local(dt: datetime) -> str:
    return dt.astimezone(local_tz()).strftime("%A %b %d, %I:%M %p %Z")


def humanize_gap(delta_seconds: float) -> str:
    secs = int(max(0, delta_seconds))
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m"
    hours, rem_m = divmod(mins, 60)
    if hours < 24:
        return f"{hours}h {rem_m}m" if rem_m else f"{hours}h"
    days, rem_h = divmod(hours, 24)
    return f"{days}d {rem_h}h" if rem_h else f"{days}d"


def new_thread_id(prefix: str = "cli") -> str:
    return f"{prefix}:{uuid.uuid4().hex[:12]}"


# ── PostgreSQL-backed store (history / events / threads) ────────────────────────

class Store:
    """History/events access. Every method is defensive; `.ok` is False if the
    DB never came up, in which case the engine runs statelessly.

    `source` tags the events this store writes ("cli", "api", …) and scopes
    `last_departure()` to the same front-end, so the terminal REPL reports when
    Brian last left the *terminal* rather than when he closed the desktop app.
    """

    def __init__(self, source: str = "cli"):
        self.ok = False
        self.user_id: int | None = None
        self.username = str(CONFIG.get("primary_user", "brian"))
        self.source = source
        self._log_event = None
        try:
            import auth
            from events import log_event
            auth.init_db()  # idempotent
            self._get_db = auth.get_db
            self._log_event = log_event
            self.user_id = self._resolve_user_id()
            self.ok = self.user_id is not None
        except Exception as exc:
            logger.warning("Memory disabled — DB init failed: %s", exc)

    def _resolve_user_id(self) -> int | None:
        db = self._get_db()
        try:
            row = db.execute("SELECT id FROM users WHERE username = ?", (self.username,)).fetchone()
            return int(row["id"]) if row else None
        finally:
            db.close()

    def recent_turns(self, limit: int = 16) -> list[dict]:
        """Last `limit` turns across all threads (oldest first)."""
        return self._turns("WHERE user_id = ?", (self.user_id,), limit)

    def thread_history(self, session_id: str, limit: int = 200) -> list[dict]:
        """All turns in one thread (oldest first)."""
        return self._turns("WHERE user_id = ? AND session_id = ?", (self.user_id, session_id), limit)

    def _turns(self, where: str, params: tuple, limit: int) -> list[dict]:
        if not self.ok:
            return []
        db = self._get_db()
        try:
            rows = db.execute(
                f"SELECT role, content, ts, session_id FROM history {where} "
                f"ORDER BY id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
            return [dict(r) for r in reversed(rows)]
        except Exception as exc:
            logger.warning("history read failed: %s", exc)
            return []
        finally:
            db.close()

    def list_threads(self, limit: int = 100) -> list[dict]:
        """Thread summaries newest-first: id, title (first user line), counts, times."""
        if not self.ok:
            return []
        db = self._get_db()
        try:
            rows = db.execute(
                """
                SELECT session_id,
                       COUNT(*)                          AS msg_count,
                       MIN(ts)                           AS started,
                       MAX(ts)                           AS last_ts,
                       MIN(id)                           AS first_id
                FROM history
                WHERE user_id = ? AND session_id IS NOT NULL
                GROUP BY session_id
                ORDER BY MAX(ts) DESC
                LIMIT ?
                """,
                (self.user_id, limit),
            ).fetchall()
            out = []
            for r in rows:
                title = self._thread_title(db, r["session_id"])
                out.append({
                    "thread_id": r["session_id"],
                    "title": title,
                    "msg_count": r["msg_count"],
                    "started": r["started"],
                    "last_ts": r["last_ts"],
                })
            return out
        except Exception as exc:
            logger.warning("thread list failed: %s", exc)
            return []
        finally:
            db.close()

    def _thread_title(self, db, session_id: str) -> str:
        try:
            row = db.execute(
                "SELECT content FROM history WHERE user_id = ? AND session_id = ? "
                "AND role = 'user' ORDER BY id ASC LIMIT 1",
                (self.user_id, session_id),
            ).fetchone()
            if row and row["content"]:
                t = " ".join(str(row["content"]).split())
                return (t[:60] + "…") if len(t) > 60 else t
        except Exception:
            pass
        return "New chat"

    def last_departure(self) -> datetime | None:
        if not self.ok:
            return None
        db = self._get_db()
        try:
            row = db.execute(
                "SELECT ts FROM events WHERE user_id = ? AND event_type = 'session_end' "
                "AND source = ? ORDER BY id DESC LIMIT 1",
                (self.user_id, self.source),
            ).fetchone()
            return parse_ts(row["ts"]) if row else None
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
            ts = utc_now_iso()
            for role, content, author in (
                ("user", user_msg, self.username),
                ("assistant", assistant_msg, "aion"),
            ):
                db.execute(
                    "INSERT INTO history (user_id, role, content, ts, session_id, author_username) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (self.user_id, role, content, ts, session_id, author),
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
            self._log_event(event_type=event_type, source=self.source, user_id=self.user_id,
                            session_id=session_id, content=content, payload=payload)
        except Exception as exc:
            logger.warning("event log failed (%s): %s", event_type, exc)


# ── retrieval + context augmentation ────────────────────────────────────────────

def session_last_seen(store: "Store", prior: list[dict] | None = None):
    """When Brian was last active *anywhere*, and how long ago, as
    (last_seen, gap_seconds).

    Prefers an explicit departure event, else the timestamp of his last turn.
    This deliberately looks across threads: a brand-new thread has no history of
    its own, so scoping to it would always report "no prior session" on exactly
    the turn where continuity matters. Pass `prior` (newest turn last) to reuse
    history the caller has already loaded instead of re-querying.
    """
    last_seen = store.last_departure()
    if last_seen is None:
        turns = prior if prior is not None else store.recent_turns(1)
        last_seen = parse_ts(turns[-1]["ts"]) if turns else None
    gap = (utc_now() - last_seen).total_seconds() if last_seen else None
    return last_seen, gap


def continuity_block(now: datetime, last_seen: datetime | None, gap_seconds: float | None) -> str:
    lines = [f"Current time is {fmt_local(now)}."]
    if last_seen is not None and gap_seconds is not None:
        lines.append(f"Brian was last active {humanize_gap(gap_seconds)} ago "
                     f"(at {fmt_local(last_seen)}); he has just returned this session.")
    else:
        lines.append("No prior session on record — this looks like a first conversation.")
    return "\n".join(f"- {l}" for l in lines)


def facts_block(user_text: str) -> str:
    """Curated facts + semantic memory + ChatGPT-archive auto-recall."""
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
    if chatgpt_store is not None and chatgpt_store.enabled():
        try:
            gpt = chatgpt_store.format_hits(chatgpt_store.search(user_text, final_limit=4))
            if gpt:
                blocks.append(gpt)
        except Exception:
            pass
    return "\n\n".join(blocks)


def augment_user_turn(user_text: str, now: datetime, last_seen, gap_seconds,
                      include_continuity: bool) -> str:
    ctx_parts = []
    if include_continuity:
        ctx_parts.append(continuity_block(now, last_seen, gap_seconds))
    fb = facts_block(user_text)
    if fb:
        ctx_parts.append(fb)
    if not ctx_parts:
        return user_text
    ctx = "\n\n".join(ctx_parts)
    return ("(Context for you, AION — background only, don't quote it back verbatim:\n"
            f"{ctx}\n)\n\n"
            f"Brian: {user_text}")


def build_messages(prior_turns: list[dict], user_text: str, *,
                   include_continuity: bool = False, last_seen=None, gap_seconds=None,
                   augmented: str | None = None) -> list[dict]:
    """Assemble the messages list to send to the model: bounded prior turns
    (user/assistant only, no leading assistant) + the augmented current turn.

    `prior_turns` must NOT already contain `user_text` — it is appended here.
    `augmented` overrides the default context-folding for the final turn (used
    by /msg, which folds message-archive threads in instead of facts).
    """
    convo = [{"role": t["role"], "content": t["content"]}
             for t in prior_turns
             if t.get("role") in ("user", "assistant") and t.get("content")]
    convo.append({"role": "user", "content": user_text})
    window = [dict(m) for m in convo[-CONTEXT_WINDOW:]]
    while window and window[0]["role"] == "assistant":
        window.pop(0)
    window[-1]["content"] = augmented if augmented is not None else augment_user_turn(
        user_text, utc_now(), last_seen, gap_seconds, include_continuity)
    persona = CONFIG.get("persona")
    if persona and CONFIG.get("persona_as_system_message"):
        # A real system message — the raw HF model accepts one, and it's where a
        # persona belongs. (aion-hauhau 400s on system role; that's what the
        # persona_as_system_message flag is for. See config.py.)
        window.insert(0, {"role": "system", "content": persona})
    return window


# Labels the base model sometimes prefixes despite the persona telling it not to.
# Handles bold either side of the colon: "Response:", "**Response:**", "**Reply:** x".
_LEAD_LABEL = re.compile(
    r"^\s*(?:\*\*|__)?\s*(?:response|answer|reply|aion|assistant)\s*(?:\*\*|__)?\s*:\s*(?:\*\*|__)?\s*\n*",
    re.IGNORECASE)


def clean_reply(text: str) -> str:
    """Strip a leading 'Response:' / 'AION:' style label the model occasionally
    emits. Belt-and-suspenders behind the persona's no-label instruction."""
    if not text:
        return text
    return _LEAD_LABEL.sub("", text, count=1).lstrip()


def chat(session_id: str, user_text: str, *, store: Store | None = None,
         include_continuity: bool = False) -> str:
    """Full non-streaming turn: load thread history → build → ask → persist."""
    store = store or Store()
    prior = store.thread_history(session_id)
    last_seen = gap = None
    if include_continuity:
        last_seen, gap = session_last_seen(store)
    messages = build_messages(prior, user_text, include_continuity=include_continuity,
                              last_seen=last_seen, gap_seconds=gap)
    reply = clean_reply((ask_llm_chat(messages) or "").strip()) or "(no response)"
    store.save_turn(session_id, user_text, reply)
    return reply


def messages_available() -> bool:
    """True if the Jenn/FB message archive (messages.db) is present here."""
    return messages_store is not None and messages_store.db_exists()


def search_messages(query: str, max_threads: int = 4, max_per_thread: int = 8) -> list[str]:
    """Jenn/FB message-archive search (messages.db). Returns formatted blocks."""
    if not messages_available():
        return []
    return messages_store.search_threads(query=query, max_threads=max_threads,
                                         max_per_thread=max_per_thread)


def msg_context_turn(query: str, blocks: list[str]) -> str:
    """Fold matched archive threads into a user turn (no system role)."""
    ctx = "\n\n".join(blocks)
    return ("(Message-archive threads matching the search — these are real logged "
            f"messages; use them to answer:\n{ctx}\n)\n\n"
            f"Brian: Walk me through these messages about \"{query}\".")


def msg_history_label(query: str) -> str:
    """What gets written to history for a /msg turn — the command, not the
    thousands of characters of archive context we sent the model."""
    return f"/msg {query}"
