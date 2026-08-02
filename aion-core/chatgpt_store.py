"""Read-only hybrid retrieval over the PersonaBuilder ChatGPT archive.

The archive lives in the `aion_memory_foundry` Postgres DB (6k conversations /
87k messages exported from ChatGPT). PersonaBuilder owns the derived `pb_*`
tables — this module NEVER writes; it only reads for RAG.

It's a synchronous (psycopg2 + requests) port of
personabuilder/backend/retrieval.py: Postgres full-text search + pgvector
similarity, fused with Reciprocal Rank Fusion. If the archive DB is unreachable
or the embedding model is down, retrieval degrades (vector→FTS-only→empty)
rather than raising, so the CLI keeps working.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse, urlunparse

import psycopg2
import psycopg2.extras
import requests

from config import CONFIG

logger = logging.getLogger("aion.chatgpt")

_ARCHIVE_DB = "aion_memory_foundry"
_conn = None  # cached read-only connection, reconnected on failure

# Greetings / acknowledgements / filler / stopwords. A turn made up only of
# these carries no topic to recall against — auto-recall stays quiet on it.
# (Vector similarity can't gate this: short greetings match the archive's many
# short messages with HIGHER similarity than a real rare-term question.)
_TRIVIAL = {
    "hi", "hey", "hello", "yo", "sup", "heya", "hiya", "howdy", "greetings",
    "thanks", "thank", "thx", "ty", "cheers", "please", "pls",
    "ok", "okay", "k", "kk", "yep", "yes", "yeah", "yup", "no", "nope", "nah",
    "sure", "cool", "nice", "good", "great", "awesome", "fine", "alright",
    "lol", "lmao", "haha", "hmm", "huh", "oh", "ah", "wow", "bye", "later",
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were", "be",
    "to", "of", "in", "on", "at", "for", "with", "you", "your", "yours", "me",
    "my", "mine", "i", "it", "its", "this", "that", "these", "those", "we",
    "us", "our", "do", "did", "does", "done", "how", "what", "when", "who",
    "why", "where", "about", "up", "down", "so", "just", "now", "then", "here",
    "there", "again", "still", "man", "dude", "bro",
    # filler verbs / vague nouns — meaningless as the *only* content word
    "say", "said", "saying", "tell", "told", "ask", "asked", "get", "got",
    "gonna", "wanna", "thing", "things", "stuff", "something", "anything",
}


def _is_substantive(query: str) -> bool:
    """True if the query has at least one real content word to recall against.
    Filters out greetings / acknowledgements / pure-stopword turns."""
    min_content = int(CONFIG.get("chatgpt_min_content_tokens", 1))
    tokens = re.findall(r"[a-z0-9']{2,}", (query or "").lower())
    content = [t for t in tokens if t not in _TRIVIAL]
    return len(content) >= min_content

FTS_SQL = """
SELECT m.message_key, m.conversation_id, m.author_role, m.text_content,
       m.create_time, c.title,
       ts_rank(m.search_document, q) AS score
FROM messages m
JOIN conversations c ON c.id = m.conversation_id,
     websearch_to_tsquery('english', immutable_unaccent(%s)) q
WHERE m.search_document @@ q
ORDER BY score DESC
LIMIT %s
"""

VEC_SQL = """
SELECT e.message_key, e.conversation_id, e.chunk_text AS text_content,
       m.author_role, m.create_time, c.title,
       1 - (e.embedding <=> %s::vector) AS score
FROM pb_embeddings e
JOIN messages m ON m.message_key = e.message_key
JOIN conversations c ON c.id = e.conversation_id
ORDER BY e.embedding <=> %s::vector
LIMIT %s
"""


def enabled() -> bool:
    return bool(CONFIG.get("chatgpt_archive_enabled", True))


def _dsn() -> str | None:
    """DSN for the archive DB. Prefer an explicit override; otherwise derive it
    from the main DATABASE_URL by swapping the database name (same server/creds).
    """
    explicit = CONFIG.get("chatgpt_archive_url")
    if explicit:
        return explicit
    base = CONFIG.get("DATABASE_URL")
    if not base:
        return None
    try:
        return urlunparse(urlparse(base)._replace(path="/" + _ARCHIVE_DB))
    except Exception:
        return None


def _get_conn():
    """Lazily open and cache a read-only connection; reconnect if it died."""
    global _conn
    if _conn is not None:
        try:
            with _conn.cursor() as cur:
                cur.execute("SELECT 1")
            return _conn
        except Exception:
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None
    dsn = _dsn()
    if not dsn:
        return None
    _conn = psycopg2.connect(dsn)
    _conn.autocommit = True
    return _conn


def _embed(query: str) -> list[float]:
    base = CONFIG.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    model = CONFIG.get("chatgpt_embedding_model", "qwen3-embedding:0.6b")
    resp = requests.post(
        f"{base}/api/embed",
        json={
            "model": model,
            "input": [query],
            # Keep the small embedder resident so per-turn retrieval stays fast.
            "keep_alive": CONFIG.get("llm_keep_alive", "30m"),
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["embeddings"][0]


def search(query: str, *, fts_limit: int = 20, vector_limit: int = 20,
           final_limit: int = 5, rrf_k: int = 60) -> list[dict]:
    """Hybrid FTS + vector search, RRF-fused. Returns up to `final_limit` hits:
    {title, role, text, time}. Empty list on any failure."""
    if not enabled() or not (query or "").strip():
        return []
    # Skip trivial turns (greetings/acks) — and skip the embed round-trip too.
    if not _is_substantive(query):
        return []
    try:
        conn = _get_conn()
    except Exception as exc:
        logger.warning("ChatGPT archive unreachable: %s", exc)
        return []
    if conn is None:
        return []

    fts_rows: list = []
    vec_rows: list = []
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur.execute(FTS_SQL, (query, fts_limit))
            fts_rows = cur.fetchall()
        except Exception as exc:
            conn.rollback() if not conn.autocommit else None
            logger.warning("archive FTS failed: %s", exc)
        try:
            qvec = _embed(query)
            vec_str = "[" + ",".join(f"{v:.7f}" for v in qvec) + "]"
            cur.execute(VEC_SQL, (vec_str, vec_str, vector_limit))
            vec_rows = cur.fetchall()
        except Exception as exc:
            logger.warning("archive vector search skipped: %s", exc)
    except Exception as exc:
        logger.warning("archive query error: %s", exc)
        return []

    # Reciprocal Rank Fusion across the two result lists. (Trivial queries are
    # already filtered out above, so what reaches here has a real topic.)
    scores: dict[str, float] = {}
    hits: dict[str, dict] = {}
    for rows in (fts_rows, vec_rows):
        for rank, row in enumerate(rows):
            key = row["message_key"]
            scores[key] = scores.get(key, 0.0) + 1.0 / (rrf_k + rank + 1)
            if key not in hits:
                hits[key] = {
                    "title": row["title"],
                    "role": row["author_role"],
                    "text": row["text_content"],
                    "time": row["create_time"].isoformat() if row["create_time"] else None,
                }
    ranked = sorted(hits.keys(), key=lambda k: scores[k], reverse=True)
    return [hits[k] for k in ranked[:final_limit]]


def format_hits(hits: list[dict], max_chars: int = 320) -> str:
    """Render hits as a compact context block for prompt injection."""
    if not hits:
        return ""
    lines = []
    for h in hits:
        when = (h.get("time") or "")[:10]
        who = "you" if h.get("role") == "user" else "ChatGPT"
        title = (h.get("title") or "untitled")[:50]
        txt = " ".join((h.get("text") or "").split())[:max_chars]
        lines.append(f"- [{when}] ({who} · {title}) {txt}")
    return "From your past ChatGPT conversations:\n" + "\n".join(lines)


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "setting up ollama and aion"
    print(format_hits(search(q)) or "(no hits)")
