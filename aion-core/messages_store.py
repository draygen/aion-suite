#!/usr/bin/env python3
"""Query layer over data/messages.db (built by build_messages_db.py).

Provides body full-text search (FTS5), date / date-range lookups, thread
retrieval, and recent-thread listing — all returning results grouped into
threads and rendered with human timestamps ("Sep 19 2025 10:53 PM EDT",
from → to). Used by brain.py for RAG retrieval and by web.py for the memory
browser.
"""

import os
import re
import sqlite3
from typing import Any, Dict, List, Optional

DB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "messages.db"
)

# Words we strip before building an FTS query — control/verbiage, not content.
_STOPWORDS = {
    "the", "and", "for", "you", "your", "that", "this", "with", "from", "was",
    "were", "are", "our", "have", "has", "had", "what", "when", "who", "did",
    "does", "about", "show", "find", "search", "get", "give", "tell", "retrieve",
    "display", "list", "please", "can", "could", "would", "message", "messages",
    "messenger", "chat", "chats", "text", "texts", "thread", "threads",
    "conversation", "conversations", "verbatim", "said", "say", "wrote", "write",
    "facebook", "fb", "between", "all", "any", "some", "me", "my", "mine", "him",
    "her", "them", "they", "she", "his", "hers",
}

# Names that should not be treated as body-search terms (they're routing hints,
# handled by thread/sender filters instead).
_NAME_NOISE = {"jenn", "jennifer", "brian"}


def db_exists() -> bool:
    return os.path.exists(DB_PATH)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _fts_query(text: str) -> Optional[str]:
    """Build a safe FTS5 MATCH string: significant word tokens OR-joined.

    OR-joining maximizes recall; bm25 `rank` then surfaces the best matches,
    which is a decent lexical proxy for "infer on context and meaning".
    """
    tokens = re.findall(r"[a-zA-Z0-9']{2,}", (text or "").lower())
    terms = [t for t in tokens if t not in _STOPWORDS and t not in _NAME_NOISE]
    if not terms:
        return None
    # Quote each term so FTS treats it as a literal, not an operator/column.
    return " OR ".join(f'"{t}"' for t in terms)


def search(
    query: Optional[str] = None,
    on_date: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    thread: Optional[str] = None,
    sender: Optional[str] = None,
    name: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = 300,
) -> List[Dict[str, Any]]:
    """Return matching message rows (as dicts).

    query      -> FTS body/sender/thread match (ranked)
    on_date    -> exact date_est 'YYYY-MM-DD'
    start/end  -> inclusive date_est range
    thread     -> substring match on thread_display / thread_id
    sender     -> substring match on sender
    name       -> routing hint: substring match on thread OR sender
    source     -> 'jenn' | 'brian_fb'
    """
    if not db_exists():
        return []

    where = []
    params: List[Any] = []
    joins = ""
    # Non-FTS routing (by name/date/thread) surfaces most-recent activity first;
    # group_into_threads re-sorts each thread's messages chronologically after.
    order = "m.ts_utc DESC"

    fts = _fts_query(query) if query else None
    if fts:
        joins = "JOIN messages_fts f ON m.id = f.rowid"
        where.append("messages_fts MATCH ?")
        params.append(fts)
        order = "rank"  # bm25 relevance

    if on_date:
        where.append("m.date_est = ?")
        params.append(on_date)
    if start_date:
        where.append("m.date_est >= ?")
        params.append(start_date)
    if end_date:
        where.append("m.date_est <= ?")
        params.append(end_date)
    if thread:
        where.append("(m.thread_display LIKE ? OR m.thread_id LIKE ?)")
        params.extend([f"%{thread}%", f"%{thread}%"])
    if sender:
        where.append("m.sender LIKE ?")
        params.append(f"%{sender}%")
    if name:
        # Whole-word match on sender / thread_display so "chris" hits
        # "Chris Tierney" but not "Christine" / "Christopher".
        clauses = []
        for col in ("m.sender", "m.thread_display"):
            clauses.append(
                f"({col} = ? OR {col} LIKE ? OR {col} LIKE ? OR {col} LIKE ?)"
            )
            params.extend([name, f"{name} %", f"% {name} %", f"% {name}"])
        where.append("(" + " OR ".join(clauses) + ")")
    if source:
        where.append("m.source = ?")
        params.append(source)

    if not where:
        return []

    sql = (
        f"SELECT m.id, m.source, m.thread_id, m.thread_display, m.sender, "
        f"m.recipient, m.ts_utc, m.date_est, m.time_est, m.body, m.participants "
        f"FROM messages m {joins} WHERE {' AND '.join(where)} "
        f"ORDER BY {order} LIMIT ?"
    )
    params.append(limit)

    conn = _connect()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def get_thread(thread_id: str, limit: int = 500) -> List[Dict[str, Any]]:
    """All messages in a thread, chronological."""
    if not db_exists():
        return []
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, source, thread_id, thread_display, sender, recipient, "
            "ts_utc, date_est, time_est, body, participants "
            "FROM messages WHERE thread_id = ? ORDER BY ts_utc ASC LIMIT ?",
            (thread_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def recent_threads(limit: int = 50, source: Optional[str] = None) -> List[Dict[str, Any]]:
    """Thread summaries (id, display, participants, message count, date range),
    newest activity first."""
    if not db_exists():
        return []
    conn = _connect()
    try:
        where = "WHERE source = ?" if source else ""
        params: List[Any] = [source] if source else []
        params.append(limit)
        rows = conn.execute(
            f"SELECT thread_id, "
            f"       MAX(thread_display) AS thread_display, "
            f"       source, COUNT(*) AS msg_count, "
            f"       MIN(date_est) AS first_date, MAX(date_est) AS last_date, "
            f"       MAX(ts_utc) AS last_ts "
            f"FROM messages {where} "
            f"GROUP BY thread_id ORDER BY last_ts DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _short_thread(display: str, limit: int = 55) -> str:
    """Trim sprawling group-thread titles (dozens of names) for prompt headers."""
    d = (display or "").strip()
    if len(d) <= limit:
        return d
    head = d[:limit].rsplit(",", 1)[0].strip()
    extra = d.count(",")
    return f"{head} +{extra} others" if extra else d[:limit].rstrip() + "…"


def thread_samples(per_thread: int = 5) -> Dict[str, str]:
    """Map thread_id -> concatenated sample of its first few message bodies,
    for lightweight keyword categorization in the memory browser."""
    if not db_exists():
        return {}
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT thread_id, body FROM ("
            "  SELECT thread_id, body, "
            "         ROW_NUMBER() OVER (PARTITION BY thread_id ORDER BY ts_utc) rn "
            "  FROM messages"
            ") WHERE rn <= ?",
            (per_thread,),
        ).fetchall()
    finally:
        conn.close()
    out: Dict[str, str] = {}
    for r in rows:
        out[r["thread_id"]] = (out.get(r["thread_id"], "") + " " + (r["body"] or "")).strip()
    return out


def _format_line(row: Dict[str, Any]) -> str:
    """One message: '[2025-09-19 10:53 PM EDT] Brian Wallace → Chris: body'."""
    stamp = f"{row['date_est']} {row['time_est']}".strip()
    who = row.get("sender") or "(unknown)"
    to = row.get("recipient") or ""
    arrow = f" → {to}" if to and to != who else ""
    body = " ".join((row.get("body") or "").split())
    return f"[{stamp}] {who}{arrow}: {body}"


def group_into_threads(
    rows: List[Dict[str, Any]],
    max_threads: int = 6,
    max_per_thread: int = 40,
) -> List[Dict[str, Any]]:
    """Group message rows into threads (preserving first-seen thread order),
    each thread's messages sorted chronologically."""
    order: List[str] = []
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        tid = r["thread_id"]
        if tid not in buckets:
            buckets[tid] = []
            order.append(tid)
        buckets[tid].append(r)

    threads = []
    for tid in order[:max_threads]:
        msgs = sorted(buckets[tid], key=lambda r: r["ts_utc"])
        if len(msgs) > max_per_thread:
            msgs = msgs[-max_per_thread:]  # keep the most recent within the thread
        display = next((m.get("thread_display") for m in msgs if m.get("thread_display")), tid)
        threads.append(
            {
                "thread_id": tid,
                "thread_display": display,
                "first_date": min(m["date_est"] for m in msgs),
                "last_date": max(m["date_est"] for m in msgs),
                "messages": msgs,
            }
        )
    return threads


def format_thread_blocks(
    rows: List[Dict[str, Any]],
    max_threads: int = 6,
    max_per_thread: int = 40,
) -> List[str]:
    """Render grouped, chronological thread text blocks for prompt injection."""
    blocks = []
    for t in group_into_threads(rows, max_threads, max_per_thread):
        span = t["first_date"]
        if t["last_date"] != t["first_date"]:
            span = f"{t['first_date']} → {t['last_date']}"
        header = f"Thread: {_short_thread(t['thread_display'])} — {span} ({len(t['messages'])} messages)"
        lines = [header] + [_format_line(m) for m in t["messages"]]
        blocks.append("\n".join(lines))
    return blocks


def search_threads(
    query: Optional[str] = None,
    on_date: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    thread: Optional[str] = None,
    sender: Optional[str] = None,
    name: Optional[str] = None,
    max_threads: int = 6,
    max_per_thread: int = 40,
    fetch_limit: int = 400,
) -> List[str]:
    """High-level entry point: run a search and return formatted thread blocks."""
    rows = search(
        query=query,
        on_date=on_date,
        start_date=start_date,
        end_date=end_date,
        thread=thread,
        sender=sender,
        name=name,
        limit=fetch_limit,
    )
    return format_thread_blocks(rows, max_threads, max_per_thread)


_NAME_TOKENS_CACHE: Optional[set] = None
# Never treat these as routing name hints — too generic, or the primary people
# whose messages we always search anyway.
_NAME_TOKEN_EXCLUDE = {
    "unknown", "facebook", "user", "others", "and", "the", "jenn", "jennifer",
    "brian", "wallace", "frotten",
}


def known_name_tokens() -> set:
    """Lowercased word tokens drawn from distinct senders and thread names,
    used to detect a contact/thread hint in a natural-language query. Cached."""
    global _NAME_TOKENS_CACHE
    if _NAME_TOKENS_CACHE is not None:
        return _NAME_TOKENS_CACHE
    toks: set = set()
    if db_exists():
        conn = _connect()
        try:
            for col in ("sender", "thread_display"):
                for (val,) in conn.execute(f"SELECT DISTINCT {col} FROM messages"):
                    for w in re.findall(r"[a-zA-Z]{3,}", (val or "").lower()):
                        toks.add(w)
        finally:
            conn.close()
    _NAME_TOKENS_CACHE = toks - _NAME_TOKEN_EXCLUDE
    return _NAME_TOKENS_CACHE


if __name__ == "__main__":
    import sys

    q = " ".join(sys.argv[1:]) or "custody"
    for block in search_threads(query=q, max_threads=3, max_per_thread=6):
        print(block)
        print("-" * 60)
