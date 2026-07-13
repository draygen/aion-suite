#!/usr/bin/env python3
"""Parse the raw Facebook "your_facebook_activity/messages" HTML export into
normalized per-message rows (both sides of every thread).

Facebook's current HTML export wraps each message in:

    <section class="_a6-g">
      <h2 class="_a6-h">Brian Wallace</h2>            # sender
      <div class="_a6-p"> ... message text ... </div>  # body
      <footer><div class="_a72d">Sep 19, 2025 11:48:49 pm</div></footer>  # timestamp
    </section>

Timestamps in the HTML export are rendered in the account's **local timezone**
(US/Eastern for Brian), so they are parsed as Eastern local time — no UTC
conversion guesswork required.

The export root defaults to the known dump location but can be overridden with
the AION_FB_EXPORT_DIR environment variable.
"""

import glob
import json
import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

DEFAULT_EXPORT_DIR = (
    "/mnt/c/Users/drayg/projects/fb_ai_brian/your_facebook_activity/messages"
)
# Thread folders within the export we ingest (skip settings/photos/etc.).
THREAD_FOLDERS = ("inbox", "e2ee_cutover", "filtered_threads")

EASTERN = ZoneInfo("America/New_York")

# "Sep 19, 2025 11:48:49 pm"
_TS_RE = re.compile(r"^([A-Z][a-z]{2} \d{1,2}, \d{4}) (\d{1,2}:\d{2}:\d{2}) ([ap]m)$")

# Facebook system/event lines that aren't real conversation content. Matched
# against the message body; skipped at ingest so they don't pollute search.
_SYSTEM_PATTERNS = re.compile(
    r"("
    r"left the (group|conversation)|joined the (group|call|video chat|conversation)|"
    r"(was )?(added|removed) (to|from|by)|added .+ to the group|removed .+ from the group|"
    r"named the (group|conversation)|changed the (group|chat|theme|nickname|emoji|photo|color)|"
    r"set (the|your|his|her|their) nickname|set the emoji|cleared the nickname|"
    r"created (the|a) (group|poll|plan)|started (a|an|sharing) (call|video|audio|plan)|"
    r"missed (a|your|the) (call|video)|(pinned|unpinned) a message|"
    r"reacted .+ to your message|responded to the poll|waved at|sent a wave|"
    r"turned (on|off) |as the word effect|"
    r"sent an attachment|sent a (photo|sticker|gif|voice|file|video|link|location)"
    r")",
    re.IGNORECASE,
)


# Standalone event lines whose entire body is the event (actor may be "A
# contact", "Facebook user", or a name that isn't the h2 sender).
_SYSTEM_STANDALONE = re.compile(
    r"^(.{0,80}?(left|joined) the (group|conversation)\.?"
    r"|a contact (left|joined|added|removed|was added|was removed|changed|named|set|missed|called|sent|is now|started|turned|pinned|unpinned).*"
    r"|.{0,80}?(added|removed) .+ (to|from) the (group|conversation).*"
    r"|.{0,80}? (named the group|created the group|changed the group name).*)$",
    re.IGNORECASE,
)


def is_system_message(sender: str, body: str) -> bool:
    """True for Facebook event lines (joins/leaves/renames/attachments/etc.)."""
    b = (body or "").strip()
    if not b:
        return True
    # System lines are typically "<Actor> <verb>…" — require the actor prefix to
    # avoid nuking normal speech that happens to contain a trigger word.
    if sender and b.startswith(sender) and _SYSTEM_PATTERNS.search(b):
        return True
    if _SYSTEM_STANDALONE.match(b):
        return True
    # A few actor-less variants worth catching regardless of prefix.
    return bool(re.match(r"^(You (are now|missed|can now)|Say hi|You waved)", b))


def export_dir() -> str:
    return os.environ.get("AION_FB_EXPORT_DIR", DEFAULT_EXPORT_DIR)


def eastern_parts(dt_aware: datetime):
    """Given any tz-aware datetime, return (ts_ms, date_est, time_est).

    date_est: 'YYYY-MM-DD' in US/Eastern
    time_est: 'HH:MM AM/PM EST' (or EDT) precomputed for display
    """
    ts_ms = int(dt_aware.timestamp() * 1000)
    local = dt_aware.astimezone(EASTERN)
    return ts_ms, local.strftime("%Y-%m-%d"), local.strftime("%I:%M %p %Z")


def _parse_ts(text: str):
    """Parse an export timestamp string into a tz-aware Eastern datetime."""
    m = _TS_RE.match(text.strip())
    if not m:
        return None
    date_s, time_s, ampm = m.group(1), m.group(2), m.group(3).upper()
    try:
        naive = datetime.strptime(f"{date_s} {time_s} {ampm}", "%b %d, %Y %I:%M:%S %p")
    except ValueError:
        return None
    # Attach Eastern wall-clock tz (DST resolved by zoneinfo).
    return naive.replace(tzinfo=EASTERN)


def _thread_id_from_slug(slug: str) -> str:
    m = re.match(r"^(.*)_(\d+)$", slug)
    return m.group(2) if m else slug


def _parse_thread_file(path: str):
    """Yield (sender, dt_eastern, body) tuples from one message_N.html file."""
    from bs4 import BeautifulSoup

    with open(path, encoding="utf-8") as f:
        soup = BeautifulSoup(f, "html.parser")

    for sec in soup.select("section._a6-g"):
        h = sec.find("h2")
        sender = h.get_text(strip=True) if h else ""
        content = sec.select_one("._a6-p")
        body = content.get_text(" ", strip=True) if content else ""
        ts_div = sec.select_one("._a72d")
        dt = _parse_ts(ts_div.get_text(strip=True)) if ts_div else None
        if not body or dt is None or is_system_message(sender, body):
            continue
        yield sender, dt, body


def iter_thread_records(root: str = None):
    """Yield one dict per thread: {thread_id, thread_display, messages:[...]}.

    Each message is (sender, dt_eastern, body). Messages are sorted oldest-first.
    """
    root = root or export_dir()
    for folder in THREAD_FOLDERS:
        base = os.path.join(root, folder)
        if not os.path.isdir(base):
            continue
        for slug in sorted(os.listdir(base)):
            tdir = os.path.join(base, slug)
            if not os.path.isdir(tdir):
                continue
            files = sorted(glob.glob(os.path.join(tdir, "message_*.html")))
            if not files:
                continue
            thread_display = slug
            messages = []
            for i, fp in enumerate(files):
                if i == 0:
                    # message_1 carries the human-readable <title>.
                    try:
                        from bs4 import BeautifulSoup

                        with open(fp, encoding="utf-8") as f:
                            title = BeautifulSoup(f, "html.parser").find("title")
                        if title and title.get_text(strip=True):
                            thread_display = title.get_text(strip=True)
                    except Exception:
                        pass
                messages.extend(_parse_thread_file(fp))
            if not messages:
                continue
            messages.sort(key=lambda t: t[1])
            yield {
                "thread_id": _thread_id_from_slug(slug),
                "thread_display": thread_display,
                "messages": messages,
            }


def iter_messages(root: str = None):
    """Yield normalized message dicts ready for messages.db insertion."""
    for thread in iter_thread_records(root):
        thread_id = thread["thread_id"]
        thread_display = thread["thread_display"]
        msgs = thread["messages"]
        participants = sorted({s for s, _, _ in msgs if s})
        two_party = len(participants) == 2
        seen = set()
        for sender, dt, body in msgs:
            key = (thread_id, int(dt.timestamp()), sender, body[:80])
            if key in seen:
                continue
            seen.add(key)
            if two_party:
                recipient = next((p for p in participants if p != sender), thread_display)
            else:
                recipient = thread_display
            ts_ms, date_est, time_est = eastern_parts(dt)
            yield {
                "source": "brian_fb",
                "thread_id": thread_id,
                "thread_display": thread_display,
                "sender": sender or "(unknown)",
                "recipient": recipient or "(unknown)",
                "ts_utc": ts_ms,
                "date_est": date_est,
                "time_est": time_est,
                "body": body,
                "post_death": 0,
                "participants": json.dumps(participants, ensure_ascii=False),
            }


if __name__ == "__main__":
    n = 0
    threads = set()
    for m in iter_messages():
        n += 1
        threads.add(m["thread_id"])
    print(f"parsed {n} messages across {len(threads)} threads from {export_dir()}")
