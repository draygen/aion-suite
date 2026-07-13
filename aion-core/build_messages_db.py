#!/usr/bin/env python3
"""Build data/messages.db — a queryable, FTS5-indexed store of the verbatim
Facebook message archives.

The JSONL files remain the source of truth; this DB is a rebuildable index.
Run it whenever the JSONL exports change:

    ./.venv/bin/python build_messages_db.py

Two sources with different schemas are normalized into one `messages` table
(one row per message):

  data/jenn_messages.jsonl   source='jenn'      imported_fact thread chunks
                             (multi-message `output` with `[YYYY-MM-DD HH:MM UTC]
                             sender: body` lines; chunks overlap, so we dedupe).
                             Timestamps verified UTC (epoch matches the embedded
                             `UTC` label); converted to US/Eastern for display.

  raw FB HTML export         source='brian_fb'  both sides of every thread,
                             parsed by fb_html_parser.py. Timestamps are already
                             in local US/Eastern time in the export.

The old one-sided data/fb_messages_parsed.jsonl is intentionally NOT ingested
here (it only held Brian's outgoing text); it remains for voice-training use.
"""

import json
import os
import re
import sqlite3
from datetime import datetime, timezone

import fb_html_parser
from fb_html_parser import eastern_parts as _eastern_parts

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DATA_DIR, "messages.db")
JENN_FILE = os.path.join(DATA_DIR, "jenn_messages.jsonl")

# A message header line inside a jenn thread `output` blob. The remainder after
# the timestamp comes in two forms, handled by _split_jenn_header():
#   [2013-08-19 03:51 UTC] Jennifer Frotten: inline body...
#   [2013-07-05 15:43 UTC] From:  → To: Jennifer Frotten   (body on next lines)
_JENN_HEADER_RE = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}) UTC\] (.*)$",
    re.MULTILINE,
)
_JENN_FROMTO_RE = re.compile(r"^From:\s*(.*?)\s*→\s*To:\s*(.*?)\s*$")


def _split_jenn_header(rest: str, body_after: str):
    """Given a header's remainder and the text following the header line,
    return (sender, recipient_or_None, body)."""
    m = _JENN_FROMTO_RE.match(rest.strip())
    if m:
        # From/To style: sender + recipient on the header line, body follows.
        sender, recipient = m.group(1).strip(), m.group(2).strip()
        body = body_after.strip().strip('"').strip()
        return sender, recipient, body
    if ": " in rest:
        # Inline style: "Sender: first line of body" then continuation lines.
        sender, first = rest.split(": ", 1)
        body = first.strip()
        if body_after.strip():
            body = (body + "\n" + body_after.strip()).strip()
        return sender.strip(), None, body.strip('"').strip()
    # Fallback: no recognizable sender.
    body = (rest + "\n" + body_after).strip().strip('"').strip()
    return "", None, body


def _iter_jenn_messages():
    """Yield normalized message dicts from the jenn thread chunks (deduped)."""
    if not os.path.exists(JENN_FILE):
        return
    seen = set()
    with open(JENN_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            thread_id = rec.get("thread_id") or ""
            participants = rec.get("participants") or []
            thread_display = (rec.get("thread") or "").strip()
            if not thread_display:
                thread_display = " ↔ ".join(p for p in participants if p) or thread_id
            output = rec.get("output") or ""

            headers = list(_JENN_HEADER_RE.finditer(output))
            for i, h in enumerate(headers):
                date_s, time_s, rest = h.group(1), h.group(2), h.group(3)
                body_start = h.end()
                body_end = headers[i + 1].start() if i + 1 < len(headers) else len(output)
                body_after = output[body_start:body_end]
                sender, recipient, body = _split_jenn_header(rest, body_after)
                if not body or fb_html_parser.is_system_message(sender, body):
                    continue
                try:
                    dt = datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M").replace(
                        tzinfo=timezone.utc
                    )
                except ValueError:
                    continue
                # Never guess the sender: an empty sender means the unnamed party
                # in the thread. Substituting a participant name here risks
                # crediting Jenn with messages she actually *received*.
                # Recipient is best-effort (used only for context, not attribution).
                if recipient is None:
                    recipient = next(
                        (p for p in participants if p and p != sender), ""
                    )
                # Dedupe across overlapping chunks and the two header formats
                # (same ts + body regardless of representation or stray
                # whitespace differences between the two encodings).
                norm = re.sub(r"\s+", " ", body).strip()
                key = (thread_id, int(dt.timestamp()), norm[:120])
                if key in seen:
                    continue
                seen.add(key)
                ts_ms, date_est, time_est = _eastern_parts(dt)
                yield {
                    "source": "jenn",
                    "thread_id": thread_id,
                    "thread_display": thread_display,
                    "sender": sender or "(unknown)",
                    "recipient": recipient or "(unknown)",
                    "ts_utc": ts_ms,
                    "date_est": date_est,
                    "time_est": time_est,
                    "body": body,
                    "post_death": 1 if rec.get("post_death") else 0,
                    "participants": json.dumps(participants, ensure_ascii=False),
                }


def _iter_brian_messages():
    """Yield normalized message dicts from the raw FB HTML export (both sides)."""
    yield from fb_html_parser.iter_messages()


SCHEMA = """
DROP TABLE IF EXISTS messages_fts;
DROP TABLE IF EXISTS messages;

CREATE TABLE messages (
    id             INTEGER PRIMARY KEY,
    source         TEXT NOT NULL,
    thread_id      TEXT NOT NULL,
    thread_display TEXT,
    sender         TEXT,
    recipient      TEXT,
    ts_utc         INTEGER NOT NULL,   -- epoch ms, UTC
    date_est       TEXT NOT NULL,      -- YYYY-MM-DD in US/Eastern
    time_est       TEXT,               -- "HH:MM AM/PM EST" precomputed for display
    body           TEXT NOT NULL,
    post_death     INTEGER DEFAULT 0,
    participants   TEXT                -- JSON array
);

CREATE INDEX idx_msg_thread ON messages(thread_id, ts_utc);
CREATE INDEX idx_msg_date   ON messages(date_est);
CREATE INDEX idx_msg_ts     ON messages(ts_utc);
CREATE INDEX idx_msg_sender ON messages(sender);

-- External-content FTS5 over the searchable columns. Rebuilt from `messages`
-- after load; no triggers, since this DB is regenerated wholesale from JSONL.
CREATE VIRTUAL TABLE messages_fts USING fts5(
    body, sender, thread_display,
    content='messages', content_rowid='id'
);
"""


COLUMNS = (
    "source", "thread_id", "thread_display", "sender", "recipient",
    "ts_utc", "date_est", "time_est", "body", "post_death", "participants",
)


def create_db(path: str, rows) -> None:
    """(Re)create a messages.db at `path` from an iterable of normalized row
    dicts, populating the FTS index. Reused by build() and by tests."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        placeholders = ", ".join(["?"] * len(COLUMNS))
        insert_sql = f"INSERT INTO messages ({', '.join(COLUMNS)}) VALUES ({placeholders})"
        batch = []
        for row in rows:
            batch.append(tuple(row.get(c) for c in COLUMNS))
            if len(batch) >= 1000:
                conn.executemany(insert_sql, batch)
                batch.clear()
        if batch:
            conn.executemany(insert_sql, batch)
        conn.execute(
            "INSERT INTO messages_fts(rowid, body, sender, thread_display) "
            "SELECT id, body, sender, thread_display FROM messages"
        )
        conn.commit()
    finally:
        conn.close()


def build():
    os.makedirs(DATA_DIR, exist_ok=True)
    counts = {"jenn": 0, "brian_fb": 0}

    def _rows():
        for row in list(_iter_jenn_messages()) + list(_iter_brian_messages()):
            counts[row["source"]] += 1
            yield row

    create_db(DB_PATH, _rows())

    conn = sqlite3.connect(DB_PATH)
    try:
        total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        threads = conn.execute(
            "SELECT COUNT(DISTINCT thread_id) FROM messages"
        ).fetchone()[0]
    finally:
        conn.close()
    print(f"[✔] messages.db built: {total} messages across {threads} threads")
    print(f"    jenn={counts['jenn']}  brian_fb={counts['brian_fb']}")
    print(f"    -> {DB_PATH}")


if __name__ == "__main__":
    build()
