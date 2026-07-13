"""Small autonomous Bob participant for the local AION lobby."""

from __future__ import annotations

import argparse
import json
import random
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from auth import get_db
from config import CONFIG


CHANNEL = "global"
THREAD_ID = "lobby"
SESSION_ID = "global:lobby"
STATE_FILE = Path("data/bob_bot_state.json")
SERVICE_URL = "http://127.0.0.1:5000/api/service/chat"


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _latest_id() -> int:
    db = get_db()
    row = db.execute(
        """
        SELECT COALESCE(MAX(id), 0) AS max_id
        FROM history
        WHERE channel = ? AND thread_id = ?
        """,
        (CHANNEL, THREAD_ID),
    ).fetchone()
    db.close()
    return int(row["max_id"] or 0)


def _new_rows(after_id: int) -> list[dict]:
    db = get_db()
    rows = db.execute(
        """
        SELECT id, role, author_username, content
        FROM history
        WHERE channel = ? AND thread_id = ? AND id > ?
        ORDER BY id ASC
        """,
        (CHANNEL, THREAD_ID, after_id),
    ).fetchall()
    db.close()
    return [dict(row) for row in rows]


def _post_as_bob(message: str, timeout: int = 30) -> bool:
    payload = {
        "username": "Bob",
        "message": message,
        "channel": CHANNEL,
        "thread_id": THREAD_ID,
        "session_id": SESSION_ID,
        "metadata": {"source": "bob_bot"},
    }
    request = urllib.request.Request(
        SERVICE_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Aion-Service-Token": CONFIG.get("service_token", ""),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
        return True
    except (TimeoutError, urllib.error.URLError):
        return False


def _wants_bob_to_leave(text: str) -> bool:
    lower = text.lower()
    return bool(re.search(r"\bbob\b.*\b(leave|go away|exit|stop|shut down|bounce)\b", lower)) or bool(
        re.search(r"\b(leave|go away|exit|stop|shut down|bounce)\b.*\bbob\b", lower)
    )


def _should_answer(row: dict) -> bool:
    author = (row.get("author_username") or row.get("role") or "").lower()
    if author in {"bob", "aion"}:
        return False
    if row.get("role") != "user":
        return False
    return True


def _bob_reply(text: str) -> str:
    lower = text.lower()
    if "hello" in lower or "hi" in lower or "hey" in lower:
        return "Brian, hey. Bob reporting for questionable experimental friendship duty. AION, try not to turn this into a twelve-part documentary."
    if "calendar" in lower or "appointment" in lower:
        return "Brian, make AION earn its keep on that calendar thing. AION, dates, reminders, notes, Google devices. No dramatic fog machine."
    if "test" in lower:
        return "Brian, I am actively testing. I have no clipboard, but I do have judgment, which is frankly worse for everyone."
    if "aion" in lower:
        return "Brian, AION heard you. Whether it behaves is the exciting product-risk portion of today's program."
    choices = [
        "Brian, I’m here. Saw that. AION, keep one hand on the wheel and no speeches unless somebody asks for the deluxe edition.",
        "Brian, yep, I’m still in the room. This is already more organized than half the neighborhood group texts.",
        "Brian, got it. AION, translate that into useful action and maybe try doing it before the heat death of the universe.",
        "Brian, I’m following. Also, for the record, this is a weird way to hang out, but not the weirdest thing on this street.",
    ]
    return random.choice(choices)


def run(poll_seconds: float, announce: bool) -> None:
    state = _load_state()
    last_id = int(state.get("last_id") or _latest_id())
    if announce:
        _post_as_bob(
            "Brian, Bob is autonomous now. I’ll answer in here until you tell me to leave. AION, congratulations, you have a neighbor problem."
        )
        last_id = _latest_id()
    _save_state({"last_id": last_id, "running": True})

    while True:
        rows = _new_rows(last_id)
        for row in rows:
            last_id = max(last_id, int(row["id"]))
            content = row.get("content") or ""
            if _wants_bob_to_leave(content):
                _post_as_bob("Brian, got it. Bob is leaving the lobby. AION, try not to miss me too loudly.")
                _save_state({"last_id": last_id, "running": False})
                return
            if _should_answer(row):
                if _post_as_bob(_bob_reply(content)):
                    last_id = _latest_id()
        _save_state({"last_id": last_id, "running": True})
        time.sleep(poll_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Bob as an autonomous AION lobby participant.")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--announce", action="store_true")
    args = parser.parse_args()
    run(max(0.5, args.poll_seconds), args.announce)


if __name__ == "__main__":
    main()
