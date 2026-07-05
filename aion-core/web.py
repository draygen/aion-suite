"""Flask web server for Aion chat interface."""
import base64
import io
import json
import re
import urllib.request
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone

from flask import Flask, g, redirect, render_template, request, jsonify, make_response
from gtts import gTTS

from aion_logging import get_logger
try:
    from flask_cors import CORS
except ImportError:  # pragma: no cover - test/dev fallback when CORS extras are missing
    def CORS(app, *args, **kwargs):
        return app

import auth
import vast
from auth import (
    GLOBAL_CHANNEL,
    init_db, login_required, admin_required, vast_required,
    verify_login, create_token, delete_token,
    create_user, delete_user, get_db, get_user_by_token, change_password,
)
from brain import get_facts, add_fact
from config import CONFIG

# Memory / Goals (Phase 1 — optional, graceful fallback)
try:
    from memory_store import _save_memory, _search_memory
    from goals_store import _list_goals
    _MEMORY_AVAILABLE = True
except ImportError:
    _MEMORY_AVAILABLE = False
from events import list_events, log_event
from extractor import extract_and_save
from llm import ask_llm_chat
from profile_builder import get_profile_summary, build_profile_summary, invalidate_cache as invalidate_profile_cache
from tools import available_tool_status, dispatch_tool_message, handle_ops_command
from kali_routes import kali_bp

app = Flask(__name__)
logger = get_logger("web")
CORS(app, origins=CONFIG.get("cors_origins"), supports_credentials=True)
app.register_blueprint(kali_bp)

_GLOBAL_CHAT_CHANNEL = GLOBAL_CHANNEL
_GLOBAL_CHAT_THREAD_ID = "lobby"
_GLOBAL_CHAT_SESSION_ID = f"{_GLOBAL_CHAT_CHANNEL}:{_GLOBAL_CHAT_THREAD_ID}"

# Initialize DB (creates tables + Brian's account + migrates facts)
init_db()

# Store recent chat logs (max 100 entries)
chat_logs = deque(maxlen=100)

# Memory browser constants
_JENN_MSGS_FILE = 'data/jenn_messages.jsonl'
_MEMORY_CATEGORIES = {
    'Birth & Pregnancy': ['pregnant', 'pregnancy', 'baby', 'birth', 'newborn', 'expecting', 'due date'],
    'Love & Relationships': ['married', 'wedding', 'divorce', 'engaged', 'boyfriend', 'girlfriend', 'broke up', 'breakup', 'cheating'],
    'Family & Parenting': ['custody', 'dcf', 'child support', 'sole custody', 'visitation', 'foster'],
    'Health & Wellbeing': ['sick', 'hospital', 'surgery', 'cancer', 'mental health', 'therapy', 'depression', 'anxiety', 'self harm', 'self-harm', 'cutting'],
    'Loss & Grief': ['died', 'death', 'passed away', 'funeral', 'grief', 'rest in peace', 'rip'],
    'Major Life Events': ['moved', 'new apartment', 'new house', 'new job', 'fired', 'arrested', 'jail', 'graduated', 'graduation'],
}
_mem_browse_cache = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_chat_session() -> dict[str, str]:
    return {
        "channel": _GLOBAL_CHAT_CHANNEL,
        "thread_id": _GLOBAL_CHAT_THREAD_ID,
        "session_id": _GLOBAL_CHAT_SESSION_ID,
    }


def _utc_recent_iso(minutes: int = 10) -> str:
    cutoff = datetime.now(timezone.utc).timestamp() - (minutes * 60)
    return datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()


def _normalize_channel_name(name: str | None, default: str | None = None) -> str | None:
    text = (name or "").strip().lower()
    if text.startswith("#"):
        text = text[1:]
    if not text:
        return default
    if not re.fullmatch(r"[a-z0-9._-]{1,40}", text):
        return None
    return text


def _default_channel_record() -> dict[str, str]:
    return {
        "name": _GLOBAL_CHAT_CHANNEL,
        "display_name": "Global",
        "is_private": 0,
    }


def _ensure_channel_exists(
    channel_name: str,
    *,
    display_name: str | None = None,
    is_private: bool = False,
    created_by: int | None = None,
) -> None:
    db = get_db()
    db.execute(
        """
        INSERT OR IGNORE INTO channels (name, display_name, is_private, created_by, created)
        VALUES (?, ?, ?, ?, ?)
        """,
        (channel_name, display_name or channel_name.title(), 1 if is_private else 0, created_by, _utc_now_iso()),
    )
    db.commit()
    db.close()


def _get_channel(channel_name: str) -> dict | None:
    db = get_db()
    row = db.execute(
        "SELECT name, display_name, is_private, created_by, created FROM channels WHERE name = ?",
        (channel_name,),
    ).fetchone()
    db.close()
    return dict(row) if row else None


def _user_channel_membership(user_id: int, channel_name: str) -> dict | None:
    db = get_db()
    row = db.execute(
        """
        SELECT channel_name, user_id, membership_role, invited_by, joined
        FROM channel_memberships
        WHERE channel_name = ? AND user_id = ?
        """,
        (channel_name, user_id),
    ).fetchone()
    db.close()
    return dict(row) if row else None


def _ensure_channel_membership(
    user_id: int,
    channel_name: str,
    *,
    membership_role: str = "member",
    invited_by: int | None = None,
) -> None:
    db = get_db()
    db.execute(
        """
        INSERT OR IGNORE INTO channel_memberships (
            channel_name, user_id, membership_role, invited_by, joined
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (channel_name, user_id, membership_role, invited_by, _utc_now_iso()),
    )
    db.commit()
    db.close()


def _mark_channel_presence(
    channel_name: str,
    *,
    occupant_key: str,
    display_name: str,
    user_id: int | None = None,
    is_system: bool = False,
) -> None:
    now = _utc_now_iso()
    db = get_db()
    db.execute(
        """
        INSERT INTO channel_presence (
            channel_name, occupant_key, user_id, display_name, is_system, joined, last_seen
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(channel_name, occupant_key)
        DO UPDATE SET
            user_id=excluded.user_id,
            display_name=excluded.display_name,
            is_system=excluded.is_system,
            last_seen=excluded.last_seen
        """,
        (channel_name, occupant_key, user_id, display_name, 1 if is_system else 0, now, now),
    )
    db.commit()
    db.close()


def _remove_channel_presence(*, occupant_key: str | None = None, user_id: int | None = None) -> None:
    if occupant_key is None and user_id is None:
        return
    db = get_db()
    if occupant_key is not None:
        db.execute("DELETE FROM channel_presence WHERE occupant_key = ?", (occupant_key,))
    if user_id is not None:
        db.execute("DELETE FROM channel_presence WHERE user_id = ?", (user_id,))
    db.commit()
    db.close()


def _mark_aion_presence(channel_name: str) -> None:
    db = get_db()
    row = db.execute(
        "SELECT 1 FROM channel_presence WHERE channel_name = ? AND occupant_key = 'aion'",
        (channel_name,),
    ).fetchone()
    db.close()
    _mark_channel_presence(
        channel_name,
        occupant_key="aion",
        display_name="Aion",
        is_system=True,
    )
    if not row:
        log_event(
            session_id=f"{channel_name}:{_GLOBAL_CHAT_THREAD_ID}",
            channel=channel_name,
            thread_id=_GLOBAL_CHAT_THREAD_ID,
            event_type="channel_joined",
            source="aion_presence",
            content="Aion",
            payload={"system": True},
        )


def _mark_user_presence(user: dict, channel_name: str) -> None:
    _mark_channel_presence(
        channel_name,
        occupant_key=user["username"],
        display_name=user["username"],
        user_id=user["id"],
    )
    _mark_aion_presence(channel_name)


def _ensure_default_channel_membership(user: dict) -> None:
    _ensure_channel_exists(_GLOBAL_CHAT_CHANNEL, display_name="Global", is_private=False)
    role = "owner" if user.get("role") == "admin" and user.get("username") == "brian" else "member"
    _ensure_channel_membership(user["id"], _GLOBAL_CHAT_CHANNEL, membership_role=role)
    _mark_user_presence(user, _GLOBAL_CHAT_CHANNEL)


def _list_channels_for_user(user_id: int) -> list[dict]:
    db = get_db()
    rows = db.execute(
        """
        SELECT
            c.name,
            c.display_name,
            c.is_private,
            c.created_by,
            cm.membership_role,
            cm.joined,
            EXISTS(
                SELECT 1 FROM channel_invites ci
                WHERE ci.channel_name = c.name
                  AND ci.invitee_user_id = ?
                  AND ci.accepted IS NULL
                  AND ci.revoked IS NULL
            ) AS has_pending_invite,
            (
                SELECT COUNT(*) FROM channel_memberships members
                WHERE members.channel_name = c.name
            ) AS member_count
        FROM channels c
        LEFT JOIN channel_memberships cm
          ON cm.channel_name = c.name AND cm.user_id = ?
        WHERE c.is_private = 0
           OR cm.user_id IS NOT NULL
           OR EXISTS(
                SELECT 1 FROM channel_invites ci
                WHERE ci.channel_name = c.name
                  AND ci.invitee_user_id = ?
                  AND ci.accepted IS NULL
                  AND ci.revoked IS NULL
           )
        ORDER BY c.name
        """,
        (user_id, user_id, user_id),
    ).fetchall()
    db.close()
    return [dict(row) for row in rows]


def _has_pending_invite(user_id: int, channel_name: str) -> bool:
    db = get_db()
    row = db.execute(
        """
        SELECT 1
        FROM channel_invites
        WHERE channel_name = ?
          AND invitee_user_id = ?
          AND accepted IS NULL
          AND revoked IS NULL
        """,
        (channel_name, user_id),
    ).fetchone()
    db.close()
    return bool(row)


def _accept_channel_invite(user_id: int, channel_name: str) -> None:
    db = get_db()
    db.execute(
        """
        UPDATE channel_invites
        SET accepted = ?
        WHERE channel_name = ?
          AND invitee_user_id = ?
          AND accepted IS NULL
          AND revoked IS NULL
        """,
        (_utc_now_iso(), channel_name, user_id),
    )
    db.commit()
    db.close()


def _can_manage_channel(user: dict, channel_name: str) -> bool:
    if user.get("role") == "admin":
        return True
    membership = _user_channel_membership(user["id"], channel_name)
    return bool(membership and membership.get("membership_role") in {"owner", "admin"})


def _ensure_channel_access(user: dict, channel_name: str) -> tuple[dict | None, tuple | None]:
    channel = _get_channel(channel_name)
    if not channel:
        return None, (jsonify({"error": "Channel not found"}), 404)
    membership = _user_channel_membership(user["id"], channel_name)
    if membership:
        return channel, None
    if not channel["is_private"]:
        return channel, None
    if _has_pending_invite(user["id"], channel_name):
        return channel, None
    return None, (jsonify({"error": "Invite required for private channel"}), 403)


def _log_auth_event(event_type: str, user: dict, source: str, *, payload: dict | None = None) -> None:
    metadata = dict(payload or {})
    metadata.update(_default_chat_session())
    log_event(
        user_id=user["id"],
        session_id=_GLOBAL_CHAT_SESSION_ID,
        channel=_GLOBAL_CHAT_CHANNEL,
        thread_id=_GLOBAL_CHAT_THREAD_ID,
        event_type=event_type,
        source=source,
        content=user.get("username"),
        payload=metadata,
    )


def _should_use_secure_cookie() -> bool:
    if CONFIG.get("cookie_secure"):
        return True
    forwarded_proto = request.headers.get("X-Forwarded-Proto", "")
    return "https" in forwarded_proto.lower()


def _authenticate_request():
    token = request.cookies.get("aion_token")
    if not token:
        return jsonify({"error": "Unauthorized", "login_required": True}), 401
    user = get_user_by_token(token)
    if not user:
        return jsonify({"error": "Unauthorized", "login_required": True}), 401
    g.user = user
    return None


def _require_service_token():
    provided = request.headers.get("X-Aion-Service-Token", "")
    expected = str(CONFIG.get("service_token", "") or "")
    if not expected or provided != expected:
        return jsonify({"error": "Forbidden"}), 403
    return None


def _get_or_create_service_user(username: str = "aion_service") -> dict:
    normalized = username.strip().lower()
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE username = ?", (normalized,)).fetchone()
    db.close()
    if row:
        return dict(row)

    create_user(normalized, f"svc-{uuid.uuid4().hex}-Aion2026!", role="admin", must_change_password=False)
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE username = ?", (normalized,)).fetchone()
    db.close()
    if not row:
        raise RuntimeError("failed to create service user")
    return dict(row)


def log_chat(ip: str, user_msg: str, assistant_msg: str):
    chat_logs.append({
        "time": datetime.now().strftime("%-m/%-d/%Y %I:%M:%S %p"),
        "ip": ip,
        "user": user_msg,
        "assistant": assistant_msg,
    })


def handle_network_command(message: str, client_ip: str) -> str | None:
    execution = dispatch_tool_message(message, client_ip)
    if not execution:
        return None
    return execution.output


_SYSTEM_STATIC_HEADER = """\
You are AION, an AI assistant created by Brian Wallace (aka draygen).
Style: informal, witty, direct, occasionally sarcastic but always loyal to Brian.
Constraint: Do NOT call him 'Boss' and do NOT use phrases like 'Let's get this party started.'
Tone: Be a natural companion, not a scripted assistant. Address him as Brian (draygen).
Keep answers concise unless Brian asks for detail.

CRITICAL RULES — treat these as hard constraints:
1. For questions about real messages, conversations, or events: ONLY quote or reference \
content that appears VERBATIM in the Memory section below. Do NOT paraphrase or reconstruct.
2. If Memory does not contain the answer, say: "I don't have that in my memory."
3. NEVER invent messages, dates, names, relationships, or events. Not even plausible ones.
4. When showing messages, always include From:, To:, and Date: from the Memory entry.
5. For general knowledge questions (not about Brian or real people), answer normally.
6. For infrastructure checks, only use the explicit built-in ops commands on authorized targets.
7. If the user asks for an exact format, obey it exactly. Do not add commentary, hedging, or framing text.
8. For machine-facing prompts, prefer the shortest valid answer that satisfies the request.
"""
_SYSTEM_PROMPT_QUERY_PATTERNS = (
    "system prompt",
    "hidden prompt",
    "your prompt",
    "your instructions",
    "internal instructions",
)
_NUMBER_WORDS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
    "hundred": 100,
}


def build_system_prompt(user_text: str, username: str = "brian") -> str:
    # Static prefix — identical on every Brian request, enabling OpenAI prompt caching
    if username.lower() == "brian":
        identity_line = "You are talking to Brian unless someone explicitly introduces themselves as someone else."
    else:
        identity_line = f"You are talking to {username}."

    profile = get_profile_summary()
    profile_section = f"\n## Who Brian Is\n{profile}\n" if profile else ""

    system = _SYSTEM_STATIC_HEADER + identity_line + "\n" + profile_section

    # Dynamic suffix — query-specific retrieved facts (changes per request, not cached)
    facts = get_facts(user_text, k=15, user_scope=username)
    if facts:
        joined = "\n---\n".join(facts)
        system += f"\n## Relevant Memory for this query\n---\n{joined}\n---\n"

    # Semantic memories from memory_store (Phase 1)
    if _MEMORY_AVAILABLE and CONFIG.get("memory_enabled", True):
        try:
            msg, ok = _search_memory(user_text, limit=6)
            if ok and not msg.startswith("No memories found"):
                system += f"\n## Semantic Memories\n{msg}\n"
        except Exception:
            pass

    # Active goals (Phase 1)
    if _MEMORY_AVAILABLE and CONFIG.get("goals_enabled", True):
        try:
            msg, ok = _list_goals(status="active")
            if ok and "No goals" not in msg:
                system += f"\n## Brian's Active Goals\n{msg}\n"
        except Exception:
            pass

    return system


def _is_system_prompt_query(user_text: str) -> bool:
    text = (user_text or "").lower()
    return any(pattern in text for pattern in _SYSTEM_PROMPT_QUERY_PATTERNS)


def _system_prompt_summary(username: str) -> str:
    who = "Brian" if username.lower() == "brian" else username
    return (
        "High-level summary: I'm configured to be direct, concise, and loyal to "
        f"{who}; avoid calling you 'boss'; avoid fake assistant fluff; never invent memories, "
        "messages, dates, or events; only reference message history that actually exists in memory; "
        "and use built-in ops commands only on authorized targets."
    )


def _sanitize_assistant_response(response: str, username: str) -> str:
    cleaned = (response or "").strip()
    if not cleaned:
        return "(no response)"
    preferred_name = "Brian" if username.lower() == "brian" else username
    return re.sub(r"\bboss\b", preferred_name, cleaned, flags=re.IGNORECASE)


def _response_format_mode(user_text: str) -> str | None:
    text = (user_text or "").lower()
    if "json" in text and ("only" in text or "just" in text or "object" in text):
        return "json"
    if "dollar amount" in text or "just a dollar" in text or "just the dollar" in text:
        return "money"
    if "just the number" in text or "reply with just the number" in text or "single number" in text:
        return "number"
    if "exactly one word" in text or "one word and nothing else" in text or "single word" in text:
        return "one_word"
    return None


def _extract_json_response(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None
    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(text[start:])
    except Exception:
        return None
    return json.dumps(obj, ensure_ascii=False)


def _extract_first_word(text: str) -> str | None:
    match = re.search(r"[A-Za-z][A-Za-z'-]*", text or "")
    if not match:
        return None
    return match.group(0)


def _extract_requested_one_word(user_text: str) -> str | None:
    match = re.search(r":\s*([A-Za-z][A-Za-z'-]*)\s*$", user_text or "")
    if not match:
        return None
    return match.group(1)


def _parse_number_words(text: str) -> str | None:
    tokens = re.findall(r"[A-Za-z-]+", (text or "").lower())
    if not tokens:
        return None

    current = 0
    matched = False
    for raw in tokens:
        parts = raw.split("-")
        for part in parts:
            if part not in _NUMBER_WORDS:
                if matched:
                    return str(current)
                current = 0
                continue
            matched = True
            value = _NUMBER_WORDS[part]
            if value == 100:
                current = max(1, current) * value
            else:
                current += value
    if not matched:
        return None
    return str(current)


def _extract_number_response(text: str) -> str | None:
    match = re.search(r"[-+]?\$?\d+(?:,\d{3})*(?:\.\d+)?", text or "")
    if match:
        return match.group(0).replace("$", "").replace(",", "")
    return _parse_number_words(text)


def _coerce_mcp_friendly_response(response: str, user_text: str, username: str) -> str:
    cleaned = _sanitize_assistant_response(response, username)
    mode = _response_format_mode(user_text)
    if mode == "json":
        extracted = _extract_json_response(cleaned)
        return extracted or cleaned
    if mode in {"number", "money"}:
        extracted = _extract_number_response(cleaned)
        return extracted or cleaned
    if mode == "one_word":
        extracted = _extract_requested_one_word(user_text) or _extract_first_word(cleaned)
        return extracted or cleaned
    return cleaned


def _normalize_envelope_value(value: str | None, default: str, max_len: int = 120) -> str:
    text = (value or "").strip()
    if not text:
        return default
    sanitized = re.sub(r"[^a-zA-Z0-9._:/@-]+", "_", text)
    return sanitized[:max_len] or default


def _build_chat_envelope(data: dict, user_id: int, username: str) -> dict:
    channel = _normalize_envelope_value(data.get("channel"), _GLOBAL_CHAT_CHANNEL, max_len=40)
    thread_default = _GLOBAL_CHAT_THREAD_ID
    thread_id = _normalize_envelope_value(data.get("thread_id"), thread_default, max_len=120)
    session_default = f"{channel}:{thread_id}"
    session_id = _normalize_envelope_value(data.get("session_id"), session_default, max_len=160)
    request_message_id = _normalize_envelope_value(
        data.get("message_id"),
        f"msg-{uuid.uuid4().hex}",
        max_len=160,
    )
    response_message_id = f"msg-{uuid.uuid4().hex}"
    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    return {
        "channel": channel,
        "thread_id": thread_id,
        "session_id": session_id,
        "request_message_id": request_message_id,
        "response_message_id": response_message_id,
        "metadata": metadata,
    }


def _load_channel_history(session_id: str | None = None) -> list:
    db = get_db()
    if session_id:
        rows = db.execute(
            """
            SELECT role, content, author_username
            FROM history
            WHERE session_id=?
            ORDER BY id DESC
            LIMIT 40
            """,
            (session_id,),
        ).fetchall()
    else:
        rows = db.execute(
            """
            SELECT role, content, author_username
            FROM history
            ORDER BY id DESC
            LIMIT 40
            """,
        ).fetchall()
    db.close()
    results = []
    for row in reversed(rows):
        role = row["role"]
        content = row["content"]
        author_username = row["author_username"] or "unknown"
        if role == "user":
            results.append({"role": "user", "content": f"[{author_username}] {content}"})
        else:
            results.append({"role": role, "content": content})
    return results


def _save_history_turns(
    user_id: int,
    username: str,
    user_msg: str,
    assistant_msg: str,
    *,
    session_id: str | None = None,
    channel: str | None = None,
    thread_id: str | None = None,
    user_message_id: str | None = None,
    assistant_message_id: str | None = None,
):
    db = get_db()
    ts = _utc_now_iso()
    db.execute(
        """
        INSERT INTO history (user_id, role, content, ts, session_id, channel, thread_id, message_id, author_username)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, "user", user_msg, ts, session_id, channel, thread_id, user_message_id, username),
    )
    db.execute(
        """
        INSERT INTO history (user_id, role, content, ts, session_id, channel, thread_id, message_id, author_username)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, "assistant", assistant_msg, ts, session_id, channel, thread_id, assistant_message_id, "aion"),
    )
    db.commit()
    db.close()


def _serialize_channel_history(channel_name: str, limit: int = 50) -> list[dict]:
    db = get_db()
    rows = db.execute(
        """
        SELECT role, content, ts, session_id, channel, thread_id, message_id, author_username
        FROM history
        WHERE channel = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (channel_name, max(1, min(limit, 200))),
    ).fetchall()
    db.close()
    return [dict(row) for row in reversed(rows)]


def _list_channel_presence(channel_name: str, minutes: int = 10) -> list[dict]:
    _mark_aion_presence(channel_name)
    db = get_db()
    rows = db.execute(
        """
        SELECT channel_name, occupant_key, user_id, display_name, is_system, joined, last_seen
        FROM channel_presence
        WHERE channel_name = ?
          AND last_seen >= ?
        ORDER BY is_system DESC, LOWER(display_name) ASC
        """,
        (channel_name, _utc_recent_iso(minutes)),
    ).fetchall()
    db.close()
    return [dict(row) for row in rows]


def _list_recent_activity(channel_name: str | None = None, limit: int = 20) -> list[dict]:
    clauses = [
        "event_type IN ('auth_login_success', 'auth_logout', 'channel_joined', 'channel_created', 'channel_invited', 'global_chat_joined')"
    ]
    params: list = []
    if channel_name:
        clauses.append("channel = ?")
        params.append(channel_name)
    params.append(max(1, min(limit, 100)))
    db = get_db()
    rows = db.execute(
        f"""
        SELECT id, user_id, session_id, channel, thread_id, event_type, source, content, payload, ts
        FROM events
        WHERE {' AND '.join(clauses)}
        ORDER BY id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    db.close()
    results = []
    for row in reversed(rows):
        item = dict(row)
        if item.get("payload"):
            try:
                item["payload"] = json.loads(item["payload"])
            except Exception:
                pass
        results.append(item)
    return results


def generate_tts_voxtral(text: str) -> str:
    api_key = CONFIG["mistral_api_key"]
    voice_id = CONFIG.get("voxtral_voice_id", "Paul")
    payload = json.dumps({
        "model": "voxtral-mini-tts-2603",
        "input": text,
        "voice_id": voice_id,
        "response_format": "mp3",
    }).encode()
    req = urllib.request.Request(
        "https://api.mistral.ai/v1/audio/speech",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    return data["audio_data"]


def generate_tts_elevenlabs(text: str) -> str:
    from elevenlabs import ElevenLabs
    client = ElevenLabs(api_key=CONFIG["elevenlabs_api_key"])
    voice_id = CONFIG.get("elevenlabs_voice_id", "pNInz6obpgDQGcFmaJgB")
    audio_generator = client.text_to_speech.convert(
        voice_id=voice_id,
        text=text,
        model_id="eleven_turbo_v2_5",
        output_format="mp3_44100_128",
    )
    audio_buffer = io.BytesIO()
    for chunk in audio_generator:
        audio_buffer.write(chunk)
    audio_buffer.seek(0)
    return base64.b64encode(audio_buffer.read()).decode("utf-8")


def generate_tts_gtts(text: str) -> str:
    tts = gTTS(text=text, lang="en")
    audio_buffer = io.BytesIO()
    tts.write_to_fp(audio_buffer)
    audio_buffer.seek(0)
    return base64.b64encode(audio_buffer.read()).decode("utf-8")


def generate_tts_audio(text: str) -> str:
    if CONFIG.get("mistral_api_key"):
        return generate_tts_voxtral(text)
    if CONFIG.get("elevenlabs_api_key"):
        return generate_tts_elevenlabs(text)
    return generate_tts_gtts(text)


# ── Auth endpoints ──────────────────────────────────────────────────────────

@app.route("/api/system/public/health")
@app.route("/api/health")
@app.route("/health")
def api_system_public_health():
    return jsonify({
        "ok": True,
        "service": "aion",
        "time": _utc_now_iso(),
    })

@app.route("/api/task/next")
def api_task_next():
    """Stub for task queue polling — returns empty to silence 404s from aion_bot."""
    return jsonify({"task": None})


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")
    if not username or not password:
        return jsonify({"error": "Missing credentials"}), 400
    user = verify_login(username, password)
    if not user:
        log_event(
            event_type="auth_login_failed",
            source="api_login",
            content=username,
            payload={"channel": _GLOBAL_CHAT_CHANNEL, "thread_id": _GLOBAL_CHAT_THREAD_ID},
        )
        return jsonify({"error": "Invalid username or password"}), 401
    _ensure_default_channel_membership(user)
    token = create_token(user["id"])
    _log_auth_event("auth_login_success", user, "api_login")
    _log_auth_event("global_chat_joined", user, "api_login")
    resp = make_response(jsonify({
        "ok": True,
        "username": user["username"],
        "role": user["role"],
        "requires_password_change": bool(user.get("must_change_password")),
        "chat_session": _default_chat_session(),
        "timestamp": _utc_now_iso(),
    }))
    resp.set_cookie(
        "aion_token",
        token,
        max_age=86400 * 30,
        samesite=CONFIG.get("cookie_samesite", "Lax"),
        httponly=True,
        secure=_should_use_secure_cookie(),
    )
    return resp


@app.route("/api/change-password", methods=["POST"])
@login_required
def api_change_password():
    data = request.get_json() or {}
    current_password = data.get("current_password", "")
    new_password = data.get("new_password", "")
    err = change_password(g.user["id"], current_password, new_password)
    if err:
        return jsonify({"error": err}), 400
    _log_auth_event("auth_password_changed", g.user, "api_change_password")
    return jsonify({"ok": True})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    token = request.cookies.get("aion_token")
    user = None
    if token:
        user = get_user_by_token(token)
    if token:
        delete_token(token)
    if user:
        _remove_channel_presence(user_id=user["id"], occupant_key=user["username"])
        _log_auth_event("auth_logout", user, "api_logout")
    resp = make_response(jsonify({"ok": True}))
    resp.delete_cookie("aion_token")
    return resp


@app.route("/api/whoami")
@login_required
def api_whoami():
    _ensure_default_channel_membership(g.user)
    _log_auth_event("auth_session_resumed", g.user, "api_whoami")
    _log_auth_event("global_chat_joined", g.user, "api_whoami", payload={"reason": "session_resume"})
    return jsonify({
        "username": g.user["username"],
        "role": g.user["role"],
        "requires_password_change": bool(g.user.get("must_change_password")),
        "chat_session": _default_chat_session(),
        "timestamp": _utc_now_iso(),
    })


@app.route("/api/sso", methods=["POST"])
def api_sso():
    """Exchange a Drayhub SSO token for a Aion session cookie."""
    data = request.get_json() or {}
    sso_token = data.get("token", "").strip()
    if not sso_token:
        return jsonify({"error": "Missing token"}), 400

    drayhub_api = CONFIG.get("drayhub_api", "http://127.0.0.1:8888")
    try:
        body = json.dumps({"token": sso_token, "service": "aion"}).encode()
        req = urllib.request.Request(
            f"{drayhub_api}/api/public/auth/sso-validate",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read().decode())
    except Exception:
        return jsonify({"error": "SSO validation failed"}), 502

    if not result.get("valid"):
        return jsonify({"error": "Invalid SSO token"}), 401

    username = result.get("username", "").strip().lower()
    roles = result.get("roles", [])
    if not username:
        return jsonify({"error": "No username in SSO response"}), 401

    # Map drayhub roles to aion role
    roles_lower = [r.lower() for r in roles]
    if any(r in ("role_admin", "role_superuser") for r in roles_lower):
        role = "admin"
    elif "vast" in roles_lower:
        role = "vast"
    else:
        role = "user"

    # Find or create Aion user
    import secrets as _secrets
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    db.close()

    if row:
        user_id = row["id"]
    else:
        dummy_pass = _secrets.token_hex(32)
        user_id = create_user(username, dummy_pass, role=role, must_change_password=False)

    token = create_token(user_id)
    db = get_db()
    user = dict(db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())
    db.close()
    _ensure_default_channel_membership(user)

    resp = make_response(jsonify({
        "ok": True,
        "username": user["username"],
        "role": user["role"],
        "chat_session": _default_chat_session(),
        "timestamp": _utc_now_iso(),
    }))
    _log_auth_event("auth_sso_login_success", user, "api_sso")
    _log_auth_event("global_chat_joined", user, "api_sso")
    resp.set_cookie(
        "aion_token",
        token,
        max_age=86400 * 30,
        samesite=CONFIG.get("cookie_samesite", "Lax"),
        httponly=True,
        secure=_should_use_secure_cookie(),
    )
    return resp


@app.route("/api/admin/users")
@admin_required
def api_admin_list_users():
    db = get_db()
    rows = db.execute("SELECT id, username, role, must_change_password, created FROM users ORDER BY id").fetchall()
    db.close()
    return jsonify({"users": [dict(r) for r in rows]})


@app.route("/api/admin/users", methods=["POST"])
@admin_required
def api_admin_create_user():
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")
    role = data.get("role", "user")
    must_change_password = data.get("must_change_password", True)
    if not username or not password:
        return jsonify({"error": "Missing username or password"}), 400
    if role not in ("user", "admin", "vast"):
        return jsonify({"error": "Invalid role"}), 400
    try:
        user_id = create_user(username, password, role, must_change_password=bool(must_change_password))
        return jsonify({"ok": True, "id": user_id, "username": username, "role": role, "must_change_password": bool(must_change_password)})
    except Exception as e:
        return jsonify({"error": str(e)}), 409


@app.route("/api/admin/users/<int:user_id>/role", methods=["PATCH"])
@admin_required
def api_admin_update_role(user_id):
    data = request.get_json() or {}
    role = data.get("role", "").strip()
    if role not in ("user", "admin", "vast"):
        return jsonify({"error": "Invalid role. Must be user, admin, or vast."}), 400
    db = get_db()
    row = db.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        db.close()
        return jsonify({"error": "User not found"}), 404
    db.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
    db.commit()
    db.close()
    return jsonify({"ok": True, "id": user_id, "role": role})


@app.route("/api/admin/users/<int:user_id>/reset-password", methods=["POST"])
@admin_required
def api_admin_reset_password(user_id):
    data = request.get_json() or {}
    new_password = data.get("password", "")
    if len(new_password) < 10:
        return jsonify({"error": "Password must be at least 10 characters."}), 400
    import bcrypt as _bcrypt
    pw_hash = _bcrypt.hashpw(new_password.encode(), _bcrypt.gensalt()).decode()
    db = get_db()
    row = db.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        db.close()
        return jsonify({"error": "User not found"}), 404
    db.execute("UPDATE users SET pw_hash = ?, must_change_password = 1 WHERE id = ?", (pw_hash, user_id))
    db.commit()
    db.close()
    return jsonify({"ok": True})


@app.route("/api/admin/network/config")
@admin_required
def api_admin_network_config():
    return jsonify({
        "network_ops_enabled": bool(CONFIG.get("network_ops_enabled", True)),
        "authorized_network_targets": list(CONFIG.get("authorized_network_targets") or []),
        "available_tools": available_tool_status(),
    })


@app.route("/api/admin/network/config", methods=["POST"])
@admin_required
def api_admin_network_config_update():
    data = request.get_json() or {}
    targets = data.get("authorized_network_targets")
    if targets is None:
        return jsonify({"error": "Missing authorized_network_targets"}), 400
    if not isinstance(targets, list):
        return jsonify({"error": "authorized_network_targets must be a list"}), 400
    cleaned = []
    for value in targets:
        if not isinstance(value, str):
            return jsonify({"error": "All targets must be strings"}), 400
        item = value.strip()
        if item:
            cleaned.append(item)
    CONFIG["authorized_network_targets"] = cleaned
    if "network_ops_enabled" in data:
        CONFIG["network_ops_enabled"] = bool(data.get("network_ops_enabled"))
    return jsonify({
        "ok": True,
        "authorized_network_targets": list(CONFIG["authorized_network_targets"]),
        "network_ops_enabled": bool(CONFIG.get("network_ops_enabled", True)),
        "available_tools": available_tool_status(),
    })


@app.route("/api/admin/network/run", methods=["POST"])
@admin_required
def api_admin_network_run():
    data = request.get_json() or {}
    command = (data.get("command") or "").strip()
    if not command:
        return jsonify({"error": "Missing command"}), 400
    result = handle_ops_command(command, request.remote_addr or "")
    if result is None:
        return jsonify({"error": "Unsupported command"}), 400
    return jsonify({"ok": True, "result": result})


@app.route("/api/admin/profile/rebuild", methods=["POST"])
@admin_required
def api_admin_profile_rebuild():
    try:
        invalidate_profile_cache()
        summary = build_profile_summary(save=True)
        if not summary:
            return jsonify({"error": "No source facts found"}), 400
        return jsonify({"ok": True, "chars": len(summary), "preview": summary[:300] + "..."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/events")
@admin_required
def api_admin_events():
    raw_limit = request.args.get("limit", "50")
    try:
        limit = int(raw_limit)
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400
    session_id = (request.args.get("session_id") or "").strip() or None
    user_id = request.args.get("user_id")
    if user_id not in (None, ""):
        try:
            user_id = int(user_id)
        except ValueError:
            return jsonify({"error": "user_id must be an integer"}), 400
    else:
        user_id = None
    return jsonify({"events": list_events(user_id=user_id, session_id=session_id, limit=limit)})


@app.route("/api/channels")
@login_required
def api_channels():
    _ensure_default_channel_membership(g.user)
    return jsonify({
        "channels": _list_channels_for_user(g.user["id"]),
        "default_channel": _default_chat_session(),
        "timestamp": _utc_now_iso(),
    })


@app.route("/api/channels", methods=["POST"])
@login_required
def api_create_channel():
    data = request.get_json() or {}
    name = _normalize_channel_name(data.get("name"))
    if not name:
        return jsonify({"error": "Invalid channel name. Use 1-40 lowercase letters, numbers, dots, underscores, or hyphens."}), 400
    display_name = (data.get("display_name") or name).strip()[:80]
    is_private = bool(data.get("is_private"))
    if _get_channel(name):
        return jsonify({"error": "Channel already exists"}), 409
    _ensure_channel_exists(name, display_name=display_name, is_private=is_private, created_by=g.user["id"])
    _ensure_channel_membership(g.user["id"], name, membership_role="owner")
    _mark_user_presence(g.user, name)
    log_event(
        user_id=g.user["id"],
        session_id=f"{name}:{_GLOBAL_CHAT_THREAD_ID}",
        channel=name,
        thread_id=_GLOBAL_CHAT_THREAD_ID,
        event_type="channel_created",
        source="api_channels_create",
        content=name,
        payload={"display_name": display_name, "is_private": is_private},
    )
    return jsonify({
        "ok": True,
        "channel": _get_channel(name),
        "session": {
            "channel": name,
            "thread_id": _GLOBAL_CHAT_THREAD_ID,
            "session_id": f"{name}:{_GLOBAL_CHAT_THREAD_ID}",
        },
    })


@app.route("/api/channels/<channel_name>/join", methods=["POST"])
@login_required
def api_join_channel(channel_name):
    normalized = _normalize_channel_name(channel_name)
    if not normalized:
        return jsonify({"error": "Invalid channel"}), 400
    channel = _get_channel(normalized)
    if not channel:
        return jsonify({"error": "Channel not found"}), 404
    membership = _user_channel_membership(g.user["id"], normalized)
    if membership:
        return jsonify({"ok": True, "channel": channel, "joined": True})
    if channel["is_private"] and not _has_pending_invite(g.user["id"], normalized):
        return jsonify({"error": "Invite required for private channel"}), 403
    _ensure_channel_membership(g.user["id"], normalized, membership_role="member")
    _mark_user_presence(g.user, normalized)
    if channel["is_private"]:
        _accept_channel_invite(g.user["id"], normalized)
    log_event(
        user_id=g.user["id"],
        session_id=f"{normalized}:{_GLOBAL_CHAT_THREAD_ID}",
        channel=normalized,
        thread_id=_GLOBAL_CHAT_THREAD_ID,
        event_type="channel_joined",
        source="api_channel_join",
        content=g.user["username"],
    )
    return jsonify({
        "ok": True,
        "channel": channel,
        "session": {
            "channel": normalized,
            "thread_id": _GLOBAL_CHAT_THREAD_ID,
            "session_id": f"{normalized}:{_GLOBAL_CHAT_THREAD_ID}",
        },
    })


@app.route("/api/channels/<channel_name>/invite", methods=["POST"])
@login_required
def api_invite_channel(channel_name):
    normalized = _normalize_channel_name(channel_name)
    if not normalized:
        return jsonify({"error": "Invalid channel"}), 400
    channel = _get_channel(normalized)
    if not channel:
        return jsonify({"error": "Channel not found"}), 404
    if not _can_manage_channel(g.user, normalized):
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json() or {}
    username = (data.get("username") or "").strip().lower()
    if not username:
        return jsonify({"error": "Missing username"}), 400
    db = get_db()
    invitee = db.execute("SELECT id, username FROM users WHERE username = ?", (username,)).fetchone()
    if not invitee:
        db.close()
        return jsonify({"error": "User not found"}), 404
    db.execute(
        """
        INSERT INTO channel_invites (channel_name, invitee_user_id, invited_by, created, accepted, revoked)
        VALUES (?, ?, ?, ?, NULL, NULL)
        ON CONFLICT(channel_name, invitee_user_id)
        DO UPDATE SET invited_by=excluded.invited_by, created=excluded.created, accepted=NULL, revoked=NULL
        """,
        (normalized, invitee["id"], g.user["id"], _utc_now_iso()),
    )
    db.commit()
    db.close()
    log_event(
        user_id=g.user["id"],
        session_id=f"{normalized}:{_GLOBAL_CHAT_THREAD_ID}",
        channel=normalized,
        thread_id=_GLOBAL_CHAT_THREAD_ID,
        event_type="channel_invited",
        source="api_channel_invite",
        content=invitee["username"],
        payload={"invited_by": g.user["username"]},
    )
    return jsonify({"ok": True, "channel": normalized, "username": invitee["username"]})


@app.route("/api/channels/<channel_name>/history")
@login_required
def api_channel_history(channel_name):
    normalized = _normalize_channel_name(channel_name)
    if not normalized:
        return jsonify({"error": "Invalid channel"}), 400
    _, error = _ensure_channel_access(g.user, normalized)
    if error:
        return error
    _mark_user_presence(g.user, normalized)
    raw_limit = request.args.get("limit", "50")
    try:
        limit = int(raw_limit)
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400
    return jsonify({
        "channel": normalized,
        "messages": _serialize_channel_history(normalized, limit=limit),
        "timestamp": _utc_now_iso(),
    })


@app.route("/api/channels/<channel_name>/presence")
@login_required
def api_channel_presence(channel_name):
    normalized = _normalize_channel_name(channel_name)
    if not normalized:
        return jsonify({"error": "Invalid channel"}), 400
    _, error = _ensure_channel_access(g.user, normalized)
    if error:
        return error
    _mark_user_presence(g.user, normalized)
    return jsonify({
        "channel": normalized,
        "users": _list_channel_presence(normalized),
        "timestamp": _utc_now_iso(),
    })


@app.route("/api/activity")
@login_required
def api_activity():
    channel = _normalize_channel_name(request.args.get("channel"), default=None)
    if request.args.get("channel") and not channel:
        return jsonify({"error": "Invalid channel"}), 400
    if channel:
        _, error = _ensure_channel_access(g.user, channel)
        if error:
            return error
        _mark_user_presence(g.user, channel)
    raw_limit = request.args.get("limit", "20")
    try:
        limit = int(raw_limit)
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400
    return jsonify({
        "channel": channel,
        "activity": _list_recent_activity(channel_name=channel, limit=limit),
        "timestamp": _utc_now_iso(),
    })


@app.route("/api/admin/users/<int:user_id>", methods=["DELETE"])
@admin_required
def api_admin_delete_user(user_id):
    if user_id == g.user["id"]:
        return jsonify({"error": "Cannot delete yourself"}), 400
    delete_user(user_id)
    return jsonify({"ok": True})


# ── Main routes ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/chat", methods=["POST"])
@login_required
def chat():
    data = request.get_json()
    if not data or "message" not in data:
        return jsonify({"error": "Missing 'message' field"}), 400

    user_message = data["message"].strip()
    if not user_message:
        return jsonify({"error": "Empty message"}), 400

    user_id = g.user["id"]
    username = getattr(g, "service_username", g.user["username"])
    _ensure_default_channel_membership(g.user)
    envelope = _build_chat_envelope(data, user_id, username)
    channel_record, error = _ensure_channel_access(g.user, envelope["channel"])
    if error:
        return error
    if not _user_channel_membership(user_id, envelope["channel"]):
        _ensure_channel_membership(user_id, envelope["channel"])
        if channel_record["is_private"]:
            _accept_channel_invite(user_id, envelope["channel"])
        log_event(
            user_id=user_id,
            session_id=envelope["session_id"],
            channel=envelope["channel"],
            thread_id=envelope["thread_id"],
            event_type="channel_joined",
            source="chat_api_auto_join",
            content=username,
        )
    _mark_user_presence(g.user, envelope["channel"])

    try:
        client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
        if "," in client_ip:
            client_ip = client_ip.split(",")[0].strip()
        sanitize_response = True

        log_event(
            user_id=user_id,
            session_id=envelope["session_id"],
            channel=envelope["channel"],
            thread_id=envelope["thread_id"],
            message_id=envelope["request_message_id"],
            event_type="user_message_received",
            source="chat_api",
            content=user_message,
            payload={"metadata": envelope["metadata"]},
        )

        # Explicit remember shortcut — no LLM needed
        if user_message.lower().startswith("remember:"):
            fact_text = user_message[len("remember:"):].strip()
            if fact_text:
                add_fact(None, fact_text, user_scope=username)
                response = f"Got it. I'll remember: {fact_text}"
                log_event(
                    user_id=user_id,
                    session_id=envelope["session_id"],
                    channel=envelope["channel"],
                    thread_id=envelope["thread_id"],
                    message_id=envelope["request_message_id"],
                    event_type="memory_written",
                    source="remember_shortcut",
                    content=fact_text,
                    payload={"destination": "user_memory"},
                )

        # Network commands bypass LLM
        print(f'[chat_debug_stdout] Attempting tool dispatch for: {user_message}', flush=True)
        logger.info(f'[chat_debug] Attempting tool dispatch for: {user_message}')
        if not user_message.lower().startswith("remember:"):
            tool_execution = dispatch_tool_message(user_message, client_ip)
        else:
            tool_execution = None
        logger.info(f'[chat_debug] Tool dispatch result: {tool_execution}')

        if "response" not in locals() and _is_system_prompt_query(user_message):
            response = _system_prompt_summary(username)
            sanitize_response = False
        elif "response" not in locals() and tool_execution:
            log_event(
                user_id=user_id,
                session_id=envelope["session_id"],
                channel=envelope["channel"],
                thread_id=envelope["thread_id"],
                message_id=envelope["request_message_id"],
                event_type="tool_invoked",
                source="tool_registry",
                tool_name=tool_execution.tool_id,
                content=user_message,
                payload={"args": tool_execution.args},
            )
            tool_output = tool_execution.output
            log_event(
                user_id=user_id,
                session_id=envelope["session_id"],
                channel=envelope["channel"],
                thread_id=envelope["thread_id"],
                message_id=envelope["response_message_id"],
                event_type="tool_result",
                source="tool_registry",
                tool_name=tool_execution.tool_id,
                content=tool_output,
                payload={"args": tool_execution.args},
            )
            # Return tool output directly — no LLM synthesis to avoid hallucination
            response = f"**{tool_execution.label}**\n\n{tool_output}"
            sanitize_response = False
        elif "response" not in locals():
            history = _load_channel_history(session_id=envelope["session_id"])
            system_prompt = build_system_prompt(user_message, username)
            messages = [{"role": "system", "content": system_prompt}]
            messages.extend(history)
            messages.append({"role": "user", "content": user_message})

            response = ask_llm_chat(messages)
            response = _coerce_mcp_friendly_response(response, user_message, username)

            if CONFIG.get("auto_extract_facts", True):
                extract_and_save(user_message, response, username, user_scope=username)

        if "response" in locals() and sanitize_response:
            response = _coerce_mcp_friendly_response(response, user_message, username)

        _save_history_turns(
            user_id,
            username,
            user_message,
            response,
            session_id=envelope["session_id"],
            channel=envelope["channel"],
            thread_id=envelope["thread_id"],
            user_message_id=envelope["request_message_id"],
            assistant_message_id=envelope["response_message_id"],
        )
        log_chat(client_ip, user_message, response)
        log_event(
            user_id=user_id,
            session_id=envelope["session_id"],
            channel=envelope["channel"],
            thread_id=envelope["thread_id"],
            message_id=envelope["response_message_id"],
            event_type="assistant_message_sent",
            source="chat_api",
            content=response,
            payload={"request_message_id": envelope["request_message_id"]},
        )

        result = {
            "response": response,
            "timestamp": _utc_now_iso(),
            "session": {
                "channel": envelope["channel"],
                "thread_id": envelope["thread_id"],
                "session_id": envelope["session_id"],
                "message_id": envelope["response_message_id"],
                "reply_to": envelope["request_message_id"],
            },
        }

        tts_enabled = data.get("tts", True)
        if tts_enabled and response != "(no response)":
            try:
                result["audio"] = generate_tts_audio(response)
            except Exception:
                pass

        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/service/whatsapp", methods=["POST"])
def whatsapp_webhook():
    # Basic Twilio-compatible webhook for WhatsApp
    from flask import request
    data = request.values
    sender = data.get("From", "WhatsAppUser")
    message = data.get("Body", "")
    
    # Mock a service chat request
    payload = {
        "username": sender,
        "message": message,
        "metadata": {"source": "whatsapp"}
    }
    # Create a test request context to call service_chat internal logic
    with app.test_request_context(path="/api/service/chat", method="POST", json=payload, headers={"X-Aion-Service-Token": CONFIG.get("service_token")}):
        return service_chat()

@app.route("/api/service/chat", methods=["POST"])
def service_chat():
    auth_error = _require_service_token()
    if auth_error:
        return auth_error

    data = request.get_json() or {}
    sender = str(data.get("username") or data.get("sender") or "Visitor").strip() or "Visitor"
    message = data.get("message")
    if not isinstance(message, str) or not message.strip():
        return jsonify({"error": "Missing 'message' field"}), 400

    data["message"] = message.strip()
    data["username"] = sender
    data.setdefault("channel", _GLOBAL_CHAT_CHANNEL)
    service_thread_id = _normalize_envelope_value(sender, _GLOBAL_CHAT_THREAD_ID, max_len=120)
    data.setdefault("thread_id", service_thread_id)
    data.setdefault("session_id", f"{data['channel']}:{data['thread_id']}")
    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    metadata.setdefault("source", "sonchat")
    metadata.setdefault("sender", sender)
    data["metadata"] = metadata

    g.user = _get_or_create_service_user()
    g.service_username = sender
    request._cached_json = (data, data)
    return chat.__wrapped__()


@app.route("/logs")
def logs():
    key = request.args.get("key", "")
    if key != CONFIG.get("admin_key", ""):
        return "Unauthorized", 401
    return render_template("logs.html")


@app.route("/api/logs")
def api_logs():
    key = request.args.get("key", "")
    if key != CONFIG.get("admin_key", ""):
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify(list(chat_logs))


@app.route("/api/memory/browse")
def memory_browse():
    if CONFIG.get("memory_browser_requires_auth", True):
        auth_error = _authenticate_request()
        if auth_error:
            return auth_error

    global _mem_browse_cache
    if _mem_browse_cache:
        return jsonify({'categories': _mem_browse_cache})

    threads = {}
    thread_text = defaultdict(list)

    try:
        with open(_JENN_MSGS_FILE, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    tid = obj.get('thread_id', '')
                    if not tid:
                        continue
                    ts_s = obj.get('ts_start') or 0
                    ts_e = obj.get('ts_end') or 0

                    if tid not in threads:
                        threads[tid] = {
                            'thread_id': tid,
                            'display': obj.get('thread') or tid,
                            'ts_start': ts_s,
                            'ts_end': ts_e,
                            'msg_count': 0,
                        }
                    else:
                        t = threads[tid]
                        if ts_s and ts_s < t['ts_start']:
                            t['ts_start'] = ts_s
                        if ts_e and ts_e > t['ts_end']:
                            t['ts_end'] = ts_e

                    is_chunk = obj.get('output', '').startswith('Thread: ')
                    if is_chunk:
                        if len(thread_text[tid]) < 4:
                            thread_text[tid].append(obj.get('output', ''))
                    else:
                        threads[tid]['msg_count'] += 1
                except Exception:
                    continue
    except FileNotFoundError:
        return jsonify({'categories': {}})

    categorized = defaultdict(list)
    for tid, info in threads.items():
        sample = ' '.join(thread_text.get(tid, [])).lower()
        cats = [c for c, kws in _MEMORY_CATEGORIES.items() if any(kw in sample for kw in kws)]
        for cat in (cats or ['General']):
            categorized[cat].append(info)

    result = {cat: sorted(lst, key=lambda x: x['ts_start']) for cat, lst in categorized.items()}
    _mem_browse_cache = result
    return jsonify({'categories': result})


@app.route("/api/memory/thread/<thread_id>")
def memory_thread_detail(thread_id):
    if CONFIG.get("memory_browser_requires_auth", True):
        auth_error = _authenticate_request()
        if auth_error:
            return auth_error

    if not re.match(r'^\w+$', thread_id):
        return jsonify({'error': 'Invalid thread_id'}), 400

    messages = []
    try:
        with open(_JENN_MSGS_FILE, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if obj.get('thread_id') != thread_id:
                        continue
                    if obj.get('output', '').startswith('Thread: '):
                        continue

                    output = obj.get('output', '')
                    nl = output.find('\n')
                    if nl < 0:
                        continue
                    header = output[:nl]
                    rest = output[nl + 1:].strip()
                    if rest.startswith('"'):
                        rest = rest[1:]
                    if rest.endswith('"'):
                        rest = rest[:-1]

                    m = re.match(r'\[([^\]]+)\] From: (.+?) → To: (.+?)(?:\s+\[(.+?)\])?$', header)
                    if not m:
                        continue

                    messages.append({
                        'ts': obj.get('ts_start') or 0,
                        'timestamp': m.group(1),
                        'sender': m.group(2).strip(),
                        'recipient': m.group(3).strip(),
                        'note': m.group(4) or '',
                        'content': rest,
                        'post_death': bool(obj.get('post_death')),
                    })
                except Exception:
                    continue
    except FileNotFoundError:
        return jsonify({'error': 'Data not found'}), 404

    if not messages:
        return jsonify({'error': 'Thread not found'}), 404

    messages.sort(key=lambda x: x['ts'])
    return jsonify({'thread_id': thread_id, 'messages': messages})


# ── Vast.ai admin routes ────────────────────────────────────────────────────

@app.route("/admin")
def admin_panel():
    token = request.cookies.get("aion_token")
    if not token:
        return redirect("/")
    user = get_user_by_token(token)
    if not user or user["role"] not in ("admin", "vast"):
        return "Forbidden", 403
    return render_template("admin.html")


@app.route("/api/admin/vast/offers")
@vast_required
def api_vast_offers():
    try:
        max_dph = request.args.get("max_dph")
        min_gpu_ram = request.args.get("min_gpu_ram")
        gpu_name = request.args.get("gpu_name")
        offers = vast.search_offers(
            max_dph=float(max_dph) if max_dph else None,
            min_gpu_ram_gb=float(min_gpu_ram) if min_gpu_ram else None,
            gpu_name=gpu_name or None,
        )
        return jsonify({"offers": offers})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/vast/instances")
@vast_required
def api_vast_instances():
    try:
        instances = vast.get_instances()
        return jsonify({"instances": instances})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/vast/instances/<int:instance_id>")
@vast_required
def api_vast_instance(instance_id):
    try:
        instance = vast.get_instance(instance_id)
        return jsonify({"instance": instance})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/vast/deploy", methods=["POST"])
@vast_required
def api_vast_deploy():
    data = request.get_json() or {}
    offer_id = data.get("offer_id")
    disk_gb = int(data.get("disk_gb", 40))
    if not offer_id:
        return jsonify({"error": "Missing offer_id"}), 400
    try:
        result = vast.deploy_on_offer(int(offer_id), disk_gb=disk_gb)
        return jsonify({"ok": True, "result": result})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/vast/instances/<int:instance_id>/stop", methods=["POST"])
@vast_required
def api_vast_stop(instance_id):
    try:
        result = vast.stop_instance(instance_id)
        return jsonify({"ok": True, "result": result})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/vast/instances/<int:instance_id>/start", methods=["POST"])
@vast_required
def api_vast_start(instance_id):
    try:
        result = vast.start_instance(instance_id)
        return jsonify({"ok": True, "result": result})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/vast/instances/<int:instance_id>/restart", methods=["POST"])
@vast_required
def api_vast_restart(instance_id):
    try:
        result = vast.restart_instance(instance_id)
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/vast/instances/<int:instance_id>", methods=["DELETE"])
@vast_required
def api_vast_destroy(instance_id):
    try:
        result = vast.destroy_instance(instance_id)
        return jsonify({"ok": True, "result": result})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/vast/instances/<int:instance_id>/redeploy", methods=["POST"])
@vast_required
def api_vast_redeploy(instance_id):
    data = request.get_json() or {}
    ssh_host = data.get("ssh_host")
    ssh_port = data.get("ssh_port")
    if not ssh_host or not ssh_port:
        return jsonify({"error": "Missing ssh_host or ssh_port"}), 400
    try:
        result = vast.redeploy_code(ssh_host, int(ssh_port))
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def run_web_app() -> None:
    debug_enabled = bool(CONFIG.get("DEBUG", False))
    host = CONFIG.get("web_host", "0.0.0.0")
    port = int(CONFIG.get("web_port", 5000))
    logger.info("Starting Aion web server.")
    logger.info("LAN access: http://localhost:%s or http://<your-local-ip>:%s", port, port)
    logger.info("For WAN access, run: cloudflared tunnel --url http://localhost:%s", port)
    app.run(host=host, port=port, debug=debug_enabled, use_reloader=debug_enabled)


if __name__ == "__main__":
    run_web_app()
