# memory_store.py
# Long-term memory with full-text search (PostgreSQL tsvector), semantic embeddings, and labels

import logging
import re
import threading
import numpy as np
import psycopg2
import psycopg2.extras
from datetime import datetime
from contextlib import contextmanager
from config import CONFIG
from db import get_conn, dict_cursor

logger = logging.getLogger(__name__)

from memory_embeddings import get_embedder as _get_embedder

SUGGESTED_LABELS = "family, preferences, technical, stories, people, places, routines, opinions, self"

STOPWORDS = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
             'of', 'with', 'by', 'from', 'as', 'is', 'was', 'are', 'were', 'be',
             'been', 'being', 'have', 'has', 'had', 'do', 'does', 'did', 'will',
             'would', 'should', 'could', 'may', 'might', 'can', 'this', 'that',
             'these', 'those', 'i', 'you', 'he', 'she', 'it', 'we', 'they'}

SIMILARITY_THRESHOLD = 0.40
MAX_MEMORY_LENGTH = 512

_db_initialized = False
_db_lock = threading.Lock()


# ─── Database ─────────────────────────────────────────────────────────────────

def _ensure_db():
    global _db_initialized
    if _db_initialized:
        return True
    with _db_lock:
        if _db_initialized:
            return True
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS memories (
                            id         SERIAL PRIMARY KEY,
                            content    TEXT NOT NULL,
                            timestamp  TIMESTAMPTZ DEFAULT NOW(),
                            importance INTEGER DEFAULT 5,
                            keywords   TEXT,
                            context    TEXT,
                            scope      TEXT NOT NULL DEFAULT 'default',
                            label      TEXT,
                            embedding  BYTEA
                        )
                    """)
                    cur.execute("""
                        CREATE INDEX IF NOT EXISTS idx_memory_timestamp ON memories(timestamp)
                    """)
                    cur.execute("""
                        CREATE INDEX IF NOT EXISTS idx_memory_scope ON memories(scope)
                    """)
                    cur.execute("""
                        CREATE INDEX IF NOT EXISTS idx_memory_label ON memories(label)
                    """)

                    # tsvector column for full-text search (stored, auto-updated)
                    # Check if tsv column exists; add if not
                    cur.execute(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_name='memories' AND column_name='tsv'"
                    )
                    if not cur.fetchone():
                        cur.execute("""
                            ALTER TABLE memories
                            ADD COLUMN tsv tsvector
                            GENERATED ALWAYS AS (
                                to_tsvector('english',
                                    coalesce(content,'') || ' ' ||
                                    coalesce(keywords,'') || ' ' ||
                                    coalesce(label,''))
                            ) STORED
                        """)
                        cur.execute("""
                            CREATE INDEX IF NOT EXISTS idx_memory_fts ON memories USING GIN(tsv)
                        """)

                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS memory_scopes (
                            name    TEXT PRIMARY KEY,
                            created TIMESTAMPTZ DEFAULT NOW()
                        )
                    """)
                    cur.execute(
                        "INSERT INTO memory_scopes (name) VALUES ('default') ON CONFLICT DO NOTHING"
                    )
                conn.commit()
            _db_initialized = True
            logger.info("Memory database ready (PostgreSQL)")
            return True
        except Exception as e:
            logger.error("Failed to initialize memory database: %s", e)
            return False


def _scope_condition(scope='default', col='scope'):
    return f"{col} IN (%s, 'global')", [scope]


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _extract_keywords(content: str) -> str:
    words = content.lower().split()
    keywords = [w.strip('.,!?;:\'\"()') for w in words if len(w) > 2 and w.lower() not in STOPWORDS]
    return ' '.join(sorted(set(keywords)))


def _format_time_ago(timestamp) -> str:
    try:
        from zoneinfo import ZoneInfo
        tz_name = CONFIG.get('USER_TIMEZONE', 'UTC') or 'UTC'
        user_tz = ZoneInfo(tz_name)
        if isinstance(timestamp, datetime):
            ts = timestamp
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=ZoneInfo('UTC'))
        else:
            ts = datetime.fromisoformat(str(timestamp))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=ZoneInfo('UTC'))
        diff = datetime.now(user_tz) - ts
        days, hours, minutes = diff.days, diff.seconds // 3600, (diff.seconds % 3600) // 60
        if days > 0:
            return f"{days}d ago"
        elif hours > 0:
            return f"{hours}h ago"
        elif minutes > 0:
            return f"{minutes}m ago"
        return "just now"
    except Exception:
        return ""


def _format_memory(row_id, content, timestamp, label):
    time_ago = _format_time_ago(timestamp)
    time_str = f" ({time_ago})" if time_ago else ""
    label_str = f" [{label}]" if label else ""
    preview = content[:150] + ('...' if len(content) > 150 else '')
    return f"[{row_id}]{time_str}{label_str} {preview}"


def _parse_labels(label) -> list:
    if not label:
        return []
    return [l.strip().lower() for l in label.split(',') if l.strip()]


def _sanitize_fts_query(query: str, use_or=False) -> str:
    """Convert a plain query string to a PostgreSQL tsquery string."""
    # Strip characters that confuse tsquery, keep alphanumeric + spaces
    sanitized = re.sub(r'[^\w\s]', ' ', query)
    sanitized = re.sub(r'\s+', ' ', sanitized).strip()
    if not sanitized:
        return ''
    terms = [t for t in sanitized.split() if len(t) > 1]
    if not terms:
        return ''
    op = ' | ' if use_or else ' & '
    return op.join(terms)


# ─── Core Operations ──────────────────────────────────────────────────────────

_backfill_done = False


def _backfill_embeddings():
    global _backfill_done
    if _backfill_done:
        return
    embedder = _get_embedder()
    if not embedder.available:
        _backfill_done = True
        return

    _ensure_db()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT id, content FROM memories WHERE embedding IS NULL')
            rows = cur.fetchall()

    if not rows:
        _backfill_done = True
        return

    logger.info("Backfilling embeddings for %d memories...", len(rows))
    batch_size = 32
    filled = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        ids = [r[0] for r in batch]
        texts = [r[1] for r in batch]
        embs = embedder.embed(texts, prefix='search_document')
        if embs is None:
            break
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    for row_id, emb in zip(ids, embs):
                        cur.execute(
                            'UPDATE memories SET embedding = %s WHERE id = %s',
                            (psycopg2.Binary(emb.tobytes()), row_id),
                        )
                conn.commit()
                filled += len(batch)
        except Exception as e:
            logger.error("Backfill batch failed: %s", e)
            break

    _backfill_done = True
    if filled:
        logger.info("Backfill complete: %d/%d memories embedded", filled, len(rows))


def _save_memory(content: str, label: str = None, scope: str = 'default') -> tuple:
    try:
        _ensure_db()
        if not content or not content.strip():
            return "Cannot save empty memory.", False
        if len(content) > MAX_MEMORY_LENGTH:
            return f"Memory too long ({len(content)} chars). Max is {MAX_MEMORY_LENGTH}.", False

        content = content.strip()
        keywords = _extract_keywords(content)
        label = label.strip().lower() if label else None

        embedding_bytes = None
        embedder = _get_embedder()
        if embedder.available:
            embs = embedder.embed([content], prefix='search_document')
            if embs is not None:
                embedding_bytes = embs[0].tobytes()

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    'INSERT INTO memories (content, keywords, scope, label, embedding) '
                    'VALUES (%s, %s, %s, %s, %s) RETURNING id',
                    (content, keywords, scope, label,
                     psycopg2.Binary(embedding_bytes) if embedding_bytes else None),
                )
                memory_id = cur.fetchone()[0]
            conn.commit()

        label_str = f", label: {label}" if label else ""
        logger.info("Stored memory ID %s%s", memory_id, label_str)
        return f"Memory saved (ID: {memory_id}{label_str})", True

    except Exception as e:
        logger.error("Error saving memory: %s", e)
        return f"Failed to save memory: {e}", False


def _fts_search(cur, fts_query: str, scope: str, labels: list, limit: int) -> list:
    """Full-text search using PostgreSQL tsvector."""
    scope_sql, scope_params = _scope_condition(scope)
    try:
        if labels:
            placeholders = ','.join(['%s'] * len(labels))
            cur.execute(f"""
                SELECT id, content, timestamp, label,
                       ts_rank(tsv, plainto_tsquery('english', %s)) AS rank
                FROM memories
                WHERE tsv @@ plainto_tsquery('english', %s)
                  AND {scope_sql}
                  AND label IN ({placeholders})
                ORDER BY rank DESC
                LIMIT %s
            """, [fts_query, fts_query] + scope_params + labels + [limit])
        else:
            cur.execute(f"""
                SELECT id, content, timestamp, label,
                       ts_rank(tsv, plainto_tsquery('english', %s)) AS rank
                FROM memories
                WHERE tsv @@ plainto_tsquery('english', %s)
                  AND {scope_sql}
                ORDER BY rank DESC
                LIMIT %s
            """, [fts_query, fts_query] + scope_params + [limit])
        return cur.fetchall()
    except Exception as e:
        logger.warning("FTS query failed: %s", e)
        return []


def _vector_search(query: str, scope: str, labels: list, limit: int) -> list:
    embedder = _get_embedder()
    if not embedder.available:
        return []

    query_emb = embedder.embed([query], prefix='search_query')
    if query_emb is None:
        return []
    query_vec = query_emb[0]

    _ensure_db()
    scope_sql, scope_params = _scope_condition(scope)
    with get_conn() as conn:
        with conn.cursor() as cur:
            if labels:
                placeholders = ','.join(['%s'] * len(labels))
                cur.execute(
                    f'SELECT id, content, timestamp, label, embedding FROM memories '
                    f'WHERE {scope_sql} AND label IN ({placeholders}) AND embedding IS NOT NULL '
                    f'LIMIT 10000',
                    scope_params + labels,
                )
            else:
                cur.execute(
                    f'SELECT id, content, timestamp, label, embedding FROM memories '
                    f'WHERE {scope_sql} AND embedding IS NOT NULL LIMIT 10000',
                    scope_params,
                )
            rows = cur.fetchall()

    if not rows:
        return []

    scored = []
    for row_id, content, timestamp, lbl, emb_bytes in rows:
        if emb_bytes is None:
            continue
        emb = np.frombuffer(bytes(emb_bytes), dtype=np.float32)
        sim = float(np.dot(query_vec, emb))
        if sim >= SIMILARITY_THRESHOLD:
            scored.append((row_id, content, timestamp, lbl, sim))

    scored.sort(key=lambda x: x[4], reverse=True)
    return scored[:limit]


def _search_memory(query: str, limit: int = 10, label: str = None, scope: str = 'default') -> tuple:
    try:
        _ensure_db()
        if not query or not query.strip():
            return "Search query cannot be empty.", False

        labels = _parse_labels(label)
        label_note = f" with labels '{label}'" if labels else ""

        _backfill_embeddings()

        # FTS search (exact / plainto)
        with get_conn() as conn:
            with conn.cursor() as cur:
                rows = _fts_search(cur, query, scope, labels, limit)
                if rows:
                    results = [_format_memory(r[0], r[1], r[2], r[3]) for r in rows]
                    return f"Found {len(rows)} memories:\n" + "\n".join(results), True

                # Broad OR search
                or_query = _sanitize_fts_query(query, use_or=True)
                if or_query:
                    rows = _fts_search(cur, or_query, scope, labels, limit)
                    if rows:
                        results = [_format_memory(r[0], r[1], r[2], r[3]) for r in rows]
                        return f"Found {len(rows)} memories:\n" + "\n".join(results), True

        # Semantic vector search
        vec_results = _vector_search(query, scope, labels, limit)
        if vec_results:
            results = [_format_memory(r[0], r[1], r[2], r[3]) for r in vec_results]
            return f"Found {len(vec_results)} memories:\n" + "\n".join(results), True

        # ILIKE fallback
        terms = query.lower().split()[:5]
        if terms:
            scope_sql, scope_params = _scope_condition(scope)
            conditions = ' OR '.join(['(content ILIKE %s OR keywords ILIKE %s)' for _ in terms])
            params = []
            for term in terms:
                params.extend([f'%{term}%', f'%{term}%'])
            if labels:
                placeholders = ','.join(['%s'] * len(labels))
                label_filter = f" AND label IN ({placeholders})"
                params.extend(labels)
            else:
                label_filter = ""

            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT id, content, timestamp, label FROM memories
                        WHERE {scope_sql} AND ({conditions}){label_filter}
                        ORDER BY timestamp DESC LIMIT %s
                    """, scope_params + params + [limit])
                    rows = cur.fetchall()
            if rows:
                results = [_format_memory(r[0], r[1], r[2], r[3]) for r in rows]
                return f"Found {len(rows)} memories:\n" + "\n".join(results), True

        return f"No memories found for '{query}'{label_note}.", True

    except Exception as e:
        logger.error("Error searching memory: %s", e)
        return f"Search failed: {e}", False


def _get_recent_memories(count: int = 10, label: str = None, scope: str = 'default') -> tuple:
    try:
        _ensure_db()
        labels = _parse_labels(label)
        scope_sql, scope_params = _scope_condition(scope)
        with get_conn() as conn:
            with conn.cursor() as cur:
                if labels:
                    placeholders = ','.join(['%s'] * len(labels))
                    cur.execute(f"""
                        SELECT id, content, timestamp, label FROM memories
                        WHERE {scope_sql} AND label IN ({placeholders})
                        ORDER BY timestamp DESC LIMIT %s
                    """, scope_params + labels + [count])
                else:
                    cur.execute(f"""
                        SELECT id, content, timestamp, label FROM memories
                        WHERE {scope_sql}
                        ORDER BY timestamp DESC LIMIT %s
                    """, scope_params + [count])
                rows = cur.fetchall()
        if not rows:
            label_note = f" with labels '{label}'" if labels else ""
            return f"No memories stored{label_note}.", True
        results = [_format_memory(r[0], r[1], r[2], r[3]) for r in rows]
        return f"Recent {len(rows)} memories:\n" + "\n".join(results), True
    except Exception as e:
        logger.error("Error getting recent memories: %s", e)
        return f"Failed to retrieve memories: {e}", False


def _delete_memory(memory_id: int, scope: str = 'default') -> tuple:
    try:
        _ensure_db()
        if not isinstance(memory_id, int) or memory_id < 1:
            return "Invalid memory ID. Use the number shown in brackets [N].", False
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    'SELECT id, content FROM memories WHERE id = %s AND scope = %s',
                    (memory_id, scope),
                )
                row = cur.fetchone()
                if not row:
                    return f"Memory [{memory_id}] not found.", False
                cur.execute(
                    'DELETE FROM memories WHERE id = %s AND scope = %s',
                    (memory_id, scope),
                )
            conn.commit()
        preview = row[1][:50] + ('...' if len(row[1]) > 50 else '')
        return f"Deleted memory [{memory_id}]: {preview}", True
    except Exception as e:
        logger.error("Error deleting memory: %s", e)
        return f"Failed to delete memory: {e}", False
