import hashlib
import json
import os
import pickle
import re
import threading
import time
from typing import List, Dict, Any, Optional, Tuple

from aion_logging import get_logger
from config import CONFIG

memory: List[Dict[str, Any]] = []
logger = get_logger("brain")

# Curated index: curated_fact, qa_pair, manual_learned, imported_fact (NOT verbatim_message).
# Only this smaller pool is fed into TF-IDF — verbatim messages inflate the matrix
# from ~2K to ~28K docs (25× larger), causing a 2-second build on every invalidation.
_INDEX_SOURCE_TYPES = {"curated_fact", "qa_pair", "manual_learned", "imported_fact"}
_index_memory: List[Dict[str, Any]] = []   # subset of memory used for TF-IDF
_index_lock = threading.Lock()             # guards TF-IDF build

# TF-IDF cache (built over _index_memory only)
_tfidf_vectorizer = None
_tfidf_matrix = None

# OpenAI embedding cache
_embed_vectors = None  # np.ndarray shape (N, dims) once built
_EMBED_CACHE_PATH = "data/embeddings_cache.pkl"

# Set of known thread names from Facebook messages, populated on load_facts()
_KNOWN_THREADS = set()

# Query-level caches (avoids repeat embeddings API calls)
_QUERY_VECTOR_CACHE: Dict[str, Any] = {}          # query_hash → np.ndarray
_FACTS_RESULT_CACHE: Dict[str, Tuple[List[str], float]] = {}  # cache_key → (results, ts)
_FACTS_CACHE_TTL: int = 600  # seconds (10 min)


def _shared_facts_path() -> str:
    return CONFIG.get("shared_facts_file", "data/shared_learned.jsonl")


def _pending_facts_path() -> str:
    return CONFIG.get("pending_facts_file", "data/pending_learned.jsonl")


def _normalize_user_scope(user_scope: Optional[str]) -> str:
    return (user_scope or "").strip().lower()


def _primary_user() -> str:
    return _normalize_user_scope(CONFIG.get("primary_user", "brian"))


def _legacy_shared_fact_owner() -> str:
    return _normalize_user_scope(CONFIG.get("legacy_shared_fact_owner", _primary_user()))


def _safe_user_key(user_scope: Optional[str]) -> str:
    normalized = _normalize_user_scope(user_scope)
    return "".join(ch for ch in normalized if ch.isalnum() or ch in {"_", "-"})


def _user_memory_dir() -> str:
    return CONFIG.get("user_memory_dir", "data/users")


def _user_facts_path(user_scope: Optional[str]) -> str:
    user_key = _safe_user_key(user_scope)
    if not user_key:
        return _shared_facts_path()
    return os.path.join(_user_memory_dir(), user_key, "learned.jsonl")


def _user_pending_facts_path(user_scope: Optional[str]) -> str:
    user_key = _safe_user_key(user_scope)
    if not user_key:
        return _pending_facts_path()
    return os.path.join(_user_memory_dir(), user_key, "pending.jsonl")


def _shared_fact_files() -> List[str]:
    return list(CONFIG.get("shared_fact_files") or [])


def _configured_user_fact_files(user_scope: Optional[str]) -> List[str]:
    user_key = _normalize_user_scope(user_scope)
    configured = CONFIG.get("user_fact_files") or {}
    files = configured.get(user_key)
    if files is not None:
        return list(files)
    # Backwards compatibility: if no per-user mapping exists, preserve the old global behavior.
    return list(CONFIG.get("facts_files") or [])


def _default_files_for_user(user_scope: Optional[str], include_pending: bool = False) -> List[str]:
    files: List[str] = []
    files.extend(_shared_fact_files())
    files.extend(_configured_user_fact_files(user_scope))

    normalized = _normalize_user_scope(user_scope)
    if normalized:
        files.append(_user_facts_path(normalized))
        if include_pending or CONFIG.get("load_pending_facts", False):
            files.append(_user_pending_facts_path(normalized))

    if normalized and normalized == _legacy_shared_fact_owner():
        files.insert(0, _shared_facts_path())
        if include_pending or CONFIG.get("load_pending_facts", False):
            files.insert(1, _pending_facts_path())

    # Preserve order while removing duplicates.
    seen = set()
    unique_files: List[str] = []
    for filename in files:
        if filename not in seen:
            unique_files.append(filename)
            seen.add(filename)
    return unique_files


def _normalize_fact(raw_fact: Dict[str, Any]) -> Dict[str, Any]:
    fact = dict(raw_fact)
    if "input" not in fact and "question" in fact:
        fact["input"] = fact.pop("question")
    if "output" not in fact and "answer" in fact:
        fact["output"] = fact.pop("answer")
    if "output" not in fact and "text" in fact:
        fact["output"] = fact["text"]
    return fact


def _infer_source_type(filename: str, fact: Dict[str, Any]) -> str:
    meta = fact.get("_meta") or {}
    if meta.get("source_type"):
        return meta["source_type"]

    lower_name = os.path.basename(filename).lower()
    if filename == _shared_facts_path():
        return "manual_learned"
    if filename == _pending_facts_path():
        return "llm_extracted_pending"
    if "profile" in lower_name or "facts" in lower_name:
        return "curated_fact"
    if "qa" in lower_name:
        return "qa_pair"
    if "message" in lower_name:
        return "verbatim_message"
    return "imported_fact"


def _default_trust_for_source(source_type: str) -> bool:
    return source_type not in {"llm_extracted_pending", "llm_extracted"}


def _attach_provenance(fact: Dict[str, Any], filename: str) -> Dict[str, Any]:
    normalized = _normalize_fact(fact)
    meta = dict(normalized.get("_meta") or {})
    source_type = _infer_source_type(filename, normalized)
    meta.setdefault("source_type", source_type)
    meta.setdefault("source_file", filename)
    meta.setdefault("trusted", _default_trust_for_source(source_type))
    meta.setdefault("status", "active" if meta["trusted"] else "pending")
    normalized["_meta"] = meta
    return normalized


def _is_active_fact(fact: Dict[str, Any]) -> bool:
    meta = fact.get("_meta") or {}
    status = meta.get("status", "active")
    if status != "active":
        return CONFIG.get("load_pending_facts", False)
    return True


def _load_fact_records(files: List[str]) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for filename in files:
        if not os.path.exists(filename):
            continue
        with open(filename, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    fact = _attach_provenance(json.loads(line), filename)
                    if not _is_active_fact(fact):
                        continue
                    records.append(fact)
                except Exception:
                    pass
    return records


def _fact_text(fact: Dict[str, Any]) -> str:
    inp = (fact.get("input") or "").strip()
    out = (fact.get("output") or "").strip()
    return (inp + " \n " + out).strip()


def _fact_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _load_embed_cache() -> dict:
    try:
        with open(_EMBED_CACHE_PATH, "rb") as f:
            return pickle.load(f)
    except Exception:
        return {}


def _save_embed_cache(cache: dict) -> None:
    os.makedirs(os.path.dirname(_EMBED_CACHE_PATH), exist_ok=True)
    with open(_EMBED_CACHE_PATH, "wb") as f:
        pickle.dump(cache, f)


def _ensure_openai_embeddings() -> None:
    """Build OpenAI embedding matrix for current memory, using disk cache for unchanged facts."""
    global _embed_vectors
    if _embed_vectors is not None:
        return
    if not memory:
        return
    api_key = CONFIG.get("openai_api_key", "")
    if not api_key:
        return
    try:
        import numpy as np
        import openai

        embed_model = CONFIG.get("openai_embed_model", "text-embedding-3-small")
        client = openai.OpenAI(api_key=api_key)

        texts = [_fact_text(f) for f in memory]
        hashes = [_fact_hash(t) for t in texts]
        cache = _load_embed_cache()

        to_embed = [(i, texts[i]) for i, h in enumerate(hashes) if h not in cache]
        if to_embed:
            batch_size = 100
            for start in range(0, len(to_embed), batch_size):
                batch = to_embed[start : start + batch_size]
                idxs, batch_texts = zip(*batch)
                resp = client.embeddings.create(model=embed_model, input=list(batch_texts))
                for j, emb in enumerate(resp.data):
                    cache[hashes[idxs[j]]] = np.array(emb.embedding, dtype=np.float32)
            _save_embed_cache(cache)
            logger.info("Embedded %s new facts (cache: %s total).", len(to_embed), len(cache))

        _embed_vectors = np.array([cache[h] for h in hashes], dtype=np.float32)
        logger.info("OpenAI embedding index ready: %s facts, model=%s.", len(memory), embed_model)
    except Exception as e:
        logger.warning("OpenAI embeddings failed, falling back to TF-IDF: %s", e)
        _embed_vectors = None


def _rebuild_index_memory() -> None:
    """Rebuild _index_memory from memory (curated facts only, no verbatim messages)."""
    global _index_memory, _tfidf_vectorizer, _tfidf_matrix
    with _index_lock:
        _index_memory = [
            f for f in memory
            if (f.get("_meta") or {}).get("source_type", "imported_fact")
            in _INDEX_SOURCE_TYPES
        ]
        _tfidf_vectorizer = None
        _tfidf_matrix = None


def load_facts(files: List[str] = None, user_scope: Optional[str] = None) -> int:
    """Load facts into memory. Returns count loaded into active retrieval memory."""
    global memory, _tfidf_vectorizer, _tfidf_matrix, _embed_vectors, _KNOWN_THREADS
    memory.clear()
    _tfidf_vectorizer = None
    _tfidf_matrix = None
    _embed_vectors = None

    if files is None:
        files = _default_files_for_user(user_scope or _primary_user())

    memory.extend(_load_fact_records(files))
    _rebuild_index_memory()

    # Populate known threads for verbatim message filtering
    _KNOWN_THREADS.clear()
    for fact in memory:
        thread = fact.get("thread")
        if thread:
            _KNOWN_THREADS.add(thread.lower().strip())
            for tok in thread.lower().split():
                if len(tok) > 2:
                    _KNOWN_THREADS.add(tok)

    # Pre-warm TF-IDF in background so first request doesn't stall.
    threading.Thread(target=_ensure_tfidf, daemon=True, name="tfidf-warmup").start()
    return len(memory)


def _append_jsonl(path: str, obj: Dict[str, Any]) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def add_fact(
    input_text: Optional[str],
    output_text: str,
    metadata: Optional[Dict[str, Any]] = None,
    destination: str = "shared",
    user_scope: Optional[str] = None,
) -> str:
    """Persist a fact, optionally as pending review, and refresh active retrieval state."""
    global _tfidf_vectorizer, _tfidf_matrix
    fact: Dict[str, Any] = {}
    if input_text:
        fact["input"] = input_text.strip()
    if output_text:
        fact["output"] = output_text.strip()
    if not fact:
        return "Nothing to save."

    meta = dict(metadata or {})
    owner = _normalize_user_scope(user_scope or meta.get("owner") or meta.get("source_user"))
    if owner:
        meta.setdefault("owner", owner)
    if destination == "pending":
        meta.setdefault("source_type", "llm_extracted_pending")
        meta.setdefault("trusted", False)
        meta.setdefault("status", "pending")
        path = _user_pending_facts_path(owner) if owner else _pending_facts_path()
    else:
        meta.setdefault("source_type", "manual_learned")
        meta.setdefault("trusted", True)
        meta.setdefault("status", "active")
        path = _user_facts_path(owner) if owner else _shared_facts_path()

    fact["_meta"] = meta

    if meta.get("status", "active") == "active" and (not owner or owner == _primary_user()):
        memory.insert(0, fact)
        globals()["_embed_vectors"] = None
        _rebuild_index_memory()  # re-filters curated pool and clears TF-IDF
    try:
        _append_jsonl(path, fact)
    except Exception as e:
        return f"Saved in memory but failed to persist: {e}"
    return "Saved for review." if destination == "pending" else "Saved."


def _score(a: str, b: str) -> int:
    """Very simple overlap score for relevance ranking."""
    la = a.lower()
    lb = b.lower()
    score = 0
    for tok in set(la.split()):
        if tok and tok in lb:
            score += 1
    return score


def _ensure_tfidf():
    """Build TF-IDF matrix over curated _index_memory only (not verbatim messages)."""
    global _tfidf_vectorizer, _tfidf_matrix
    if CONFIG.get("retrieval", "embed") != "embed":
        return
    with _index_lock:
        if _tfidf_vectorizer is not None and _tfidf_matrix is not None:
            return
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer

            texts = []
            for fact in _index_memory:
                inp = fact.get("input") or ""
                out = fact.get("output") or ""
                combined = (inp + " \n " + out).strip()
                texts.append(combined)
            if not texts:
                return
            # 10K features is ample for ~2K curated docs; was 80K causing 2s build
            _tfidf_vectorizer = TfidfVectorizer(max_features=10000, ngram_range=(1, 2))
            _tfidf_matrix = _tfidf_vectorizer.fit_transform(texts)
            logger.info(
                "TF-IDF index ready: %s curated facts (skipped %s verbatim messages).",
                len(texts),
                len(memory) - len(texts),
            )
        except Exception as e:
            logger.warning("TF-IDF unavailable, falling back to lexical: %s", e)
            _tfidf_vectorizer = None
            _tfidf_matrix = None


def _format_snippet(fact: Dict[str, Any], max_len: int = 280) -> str:
    inp = (fact.get("input") or "").strip()
    out = (fact.get("output") or "").strip()
    if inp and out:
        s = f"Q: {inp}\nA: {out}"
    else:
        s = out or inp
    s = s.replace("\r", " ").replace("\n", " ")
    if len(s) > max_len:
        s = s[: max_len - 3] + "..."
    return _convert_utc_to_est_in_text(s)


def _query_hash(input_str: str, k: int, user_scope: Optional[str]) -> str:
    key = f"{input_str.strip().lower()}|{k}|{user_scope or ''}"
    return hashlib.md5(key.encode("utf-8")).hexdigest()


_MONTHS_MAP = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12
}


def _parse_date_from_query(text: str) -> Optional[str]:
    import re
    text = text.lower().strip()

    # 1. Look for YYYY-MM-DD
    match = re.search(r'\b(20\d{2})[-/](0?[1-9]|1[0-2])[-/](0?[1-9]|[12]\d|3[01])\b', text)
    if match:
        year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
        return f"{year:04d}-{month:02d}-{day:02d}"

    # 2. Look for MM/DD/YYYY or MM/DD/YY
    match = re.search(r'\b(0?[1-9]|1[0-2])[-/](0?[1-9]|[12]\d|3[01])[-/](20\d{2}|\d{2})\b', text)
    if match:
        month, day, year_val = int(match.group(1)), int(match.group(2)), match.group(3)
        if len(year_val) == 2:
            year = 2000 + int(year_val)
        else:
            year = int(year_val)
        return f"{year:04d}-{month:02d}-{day:02d}"

    # 3. Look for Month Name followed by Day, Year (optional)
    month_pattern = "|".join(_MONTHS_MAP.keys())
    match = re.search(rf'\b({month_pattern})\b\s*(?:the\s+)?(0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?\b(?:\s*,\s*|\s+)?\b(20\d{2}|\d{2})?\b', text)
    if match:
        m_name = match.group(1)
        month = _MONTHS_MAP[m_name]
        day = int(match.group(2))
        year_val = match.group(3)
        if year_val:
            if len(year_val) == 2:
                year = 2000 + int(year_val)
            else:
                year = int(year_val)
        else:
            year = 2016  # fallback to 2016 for Jenn's messages if year is omitted
        return f"{year:04d}-{month:02d}-{day:02d}"

    # 4. Look for Day followed by Month Name, Year
    match = re.search(rf'\b(0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?\b\s*(?:of\s+)?\b({month_pattern})\b(?:\s*,\s*|\s+)?\b(20\d{2}|\d{2})?\b', text)
    if match:
        day = int(match.group(1))
        m_name = match.group(2)
        month = _MONTHS_MAP[m_name]
        year_val = match.group(3)
        if year_val:
            if len(year_val) == 2:
                year = 2000 + int(year_val)
            else:
                year = int(year_val)
        else:
            year = 2016
        return f"{year:04d}-{month:02d}-{day:02d}"

    return None


def _convert_utc_to_est_in_text(text: str) -> str:
    """Rewrite any `[YYYY-MM-DD HH:MM UTC]` stamps a model emits into local
    US/Eastern with AM/PM (DST-aware). Applied to snippets and chat responses."""
    import re
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    pattern = r'\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}) UTC\]'
    eastern = ZoneInfo("America/New_York")

    def replace_match(match):
        date_str, time_str = match.group(1), match.group(2)
        try:
            dt_utc = datetime.strptime(
                f"{date_str} {time_str}", "%Y-%m-%d %H:%M"
            ).replace(tzinfo=timezone.utc)
            dt_est = dt_utc.astimezone(eastern)
            return dt_est.strftime("[%Y-%m-%d %I:%M %p %Z]")
        except Exception:
            return match.group(0)

    return re.sub(pattern, replace_match, text)


def _extract_name_hint(input_str: str) -> Optional[str]:
    """Detect a contact/thread name hint in a query (routes the search to a
    person/thread rather than the message body)."""
    try:
        import messages_store
        known = messages_store.known_name_tokens()
    except Exception:
        return None
    import re
    for w in re.findall(r"[a-zA-Z]{3,}", (input_str or "").lower()):
        if w in known:
            return w
    return None


def _strip_routing_tokens(text: str, name: Optional[str]) -> str:
    """Remove name + date tokens so the remainder is just the topical body
    query (if any)."""
    residual = text or ""
    if name:
        residual = re.sub(re.escape(name), " ", residual, flags=re.IGNORECASE)
    month_pat = r"\b(" + "|".join(_MONTHS_MAP.keys()) + r")\b"
    residual = re.sub(r"\b\d{1,4}([-/]\d{1,4}){1,2}\b", " ", residual)  # numeric dates
    residual = re.sub(r"\b20\d{2}\b", " ", residual)                     # years
    residual = re.sub(r"\b\d{1,2}(st|nd|rd|th)?\b", " ", residual, flags=re.IGNORECASE)
    residual = re.sub(month_pat, " ", residual, flags=re.IGNORECASE)
    return residual


def _get_verbatim_messages(input_str: str, fact_pool: List[Dict[str, Any]] = None,
                           k: int = 15) -> List[str]:
    """Retrieve real message logs from messages.db as grouped, chronological,
    Eastern-time thread blocks. Routes by date and/or contact name; otherwise
    full-text searches the message bodies."""
    try:
        import messages_store
    except Exception as e:
        logger.warning("messages_store unavailable: %s", e)
        return []
    if not messages_store.db_exists():
        return []

    date_str = _parse_date_from_query(input_str)
    name = _extract_name_hint(input_str)

    # A routing query (specific date or contact) returns fuller threads; a pure
    # topic query searches bodies and returns tighter, ranked matches.
    routing = bool(date_str or name)
    if routing:
        # Only add a body filter when there are clear content terms left after
        # removing the routing tokens (name + date words) — otherwise a query
        # like "messages from January 15th, 2016" would wrongly require the body
        # to contain "january"/"2016" and match nothing.
        residual = _strip_routing_tokens(input_str, name)
        content_query = residual if messages_store._fts_query(residual) else None
        max_threads, max_per_thread = 6, 40
    else:
        content_query = input_str
        max_threads, max_per_thread = 5, 12

    try:
        return messages_store.search_threads(
            query=content_query,
            on_date=date_str,
            name=name,
            max_threads=max_threads,
            max_per_thread=max_per_thread,
        )
    except Exception as e:
        logger.warning("messages_store search failed: %s", e)
        return []


def _get_curated_facts(input_str: str, fact_pool: List[Dict[str, Any]], use_global_cache: bool, k: int = 12) -> List[str]:
    """Helper to retrieve curated facts (TF-IDF or OpenAI embeddings over curated index)."""
    # Path 1: OpenAI semantic embeddings
    if use_global_cache and CONFIG.get("embed_backend") == "openai":
        _ensure_openai_embeddings()
        if _embed_vectors is not None:
            try:
                import numpy as np
                import openai

                embed_model = CONFIG.get("openai_embed_model", "text-embedding-3-small")

                # Cache the query vector itself to avoid repeat embedding API calls
                q_hash = hashlib.md5(input_str.strip().lower().encode()).hexdigest()
                if q_hash in _QUERY_VECTOR_CACHE:
                    q_vec = _QUERY_VECTOR_CACHE[q_hash]
                else:
                    client = openai.OpenAI(api_key=CONFIG["openai_api_key"])
                    resp = client.embeddings.create(model=embed_model, input=[input_str])
                    q_vec = np.array(resp.data[0].embedding, dtype=np.float32)
                    _QUERY_VECTOR_CACHE[q_hash] = q_vec

                norms = np.linalg.norm(_embed_vectors, axis=1)
                q_norm = np.linalg.norm(q_vec)
                sims = (_embed_vectors @ q_vec) / (norms * q_norm + 1e-9)
                ranked = sorted(enumerate(sims), key=lambda t: t[1], reverse=True)
                results: List[str] = []
                seen: set = set()
                for idx, score in ranked[: k * 3]:
                    if score < 0.1:
                        continue
                    fact = memory[idx]
                    if (fact.get("_meta") or {}).get("source_type", "imported_fact") not in _INDEX_SOURCE_TYPES:
                        continue
                    snip = _format_snippet(fact)
                    if not snip or snip in seen:
                        continue
                    seen.add(snip)
                    results.append(snip)
                    if len(results) >= k:
                        break
                if results:
                    return results
            except Exception as e:
                logger.warning("OpenAI embedding query failed, falling back: %s", e)

    # Path 2: TF-IDF (queries over _index_memory — curated facts only)
    if use_global_cache:
        _ensure_tfidf()
    if use_global_cache and _tfidf_vectorizer is not None and _tfidf_matrix is not None:
        try:
            import numpy as np
            from sklearn.metrics.pairwise import cosine_similarity

            q = _tfidf_vectorizer.transform([input_str])
            sims = cosine_similarity(q, _tfidf_matrix).ravel()

            # Use argpartition for O(n) top-k selection instead of O(n log n) full sort
            n = len(sims)
            top_k = min(k * 3, n)
            if top_k < n:
                top_idx = np.argpartition(sims, -top_k)[-top_k:]
                top_idx = top_idx[np.argsort(sims[top_idx])[::-1]]
            else:
                top_idx = np.argsort(sims)[::-1]

            results: List[str] = []
            seen = set()
            for idx in top_idx:
                score = sims[idx]
                if score <= 0:
                    continue
                snip = _format_snippet(_index_memory[idx])
                if not snip or snip in seen:
                    continue
                seen.add(snip)
                results.append(snip)
                if len(results) >= k:
                    break
            if results:
                return results
        except Exception as e:
            logger.warning("TF-IDF query failed, falling back to lexical: %s", e)

    # Lexical fallback restricted to curated index
    pool = _index_memory if use_global_cache else [
        f for f in fact_pool
        if (f.get("_meta") or {}).get("source_type", "imported_fact") in _INDEX_SOURCE_TYPES
    ]
    if not pool:
        return []
    scored = []
    for fact in pool:
        source = fact.get("input") or fact.get("output") or ""
        out = fact.get("output") or fact.get("input") or ""
        if not out:
            continue
        scored.append((_score(input_str, source + " " + out), _format_snippet(fact)))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [snip for score, snip in scored[:k] if score > 0]


def get_facts(input_str: str, k: int = 12, user_scope: Optional[str] = None) -> List[str]:
    """Return up to k relevant snippets. Prefer OpenAI embeddings, then TF-IDF, then lexical.

    Results are cached in memory for _FACTS_CACHE_TTL seconds to avoid redundant
    embedding API calls for identical or near-identical queries.
    """
    # Check full-result cache first
    cache_key = _query_hash(input_str, k, user_scope)
    cached = _FACTS_RESULT_CACHE.get(cache_key)
    if cached is not None:
        results, ts = cached
        if time.time() - ts < _FACTS_CACHE_TTL:
            return results
        del _FACTS_RESULT_CACHE[cache_key]

    normalized_scope = _normalize_user_scope(user_scope)
    use_global_cache = not normalized_scope or normalized_scope == _primary_user()
    fact_pool = memory if use_global_cache else _load_fact_records(_default_files_for_user(normalized_scope))

    # Keep the curated TF-IDF pool aligned with in-memory facts when callers mutate
    # `memory` directly in tests or ad hoc scripts.
    if use_global_cache and memory:
        curated_count = sum(
            1
            for fact in memory
            if (fact.get("_meta") or {}).get("source_type", "imported_fact") in _INDEX_SOURCE_TYPES
        )
        if curated_count != len(_index_memory):
            _rebuild_index_memory()

    # Determine if this is a query looking for message archives.
    msg_keywords = {"message", "messages", "messenger", "chat", "chats", "chatting",
                    "text", "texts", "texting", "thread", "threads", "conversation",
                    "conversations", "verbatim", "said", "say", "wrote", "write",
                    "facebook", "talk", "talked", "talking", "messaged", "messaging",
                    "dm", "dms", "told", "tell", "discuss", "discussed"}
    date_str = _parse_date_from_query(input_str)
    lowered = input_str.lower()
    name_hint = _extract_name_hint(input_str)
    # "who is/was X" style questions want curated identity facts, not message logs.
    identity_intent = bool(re.match(r"\s*(who\s+(is|was|are|were)\b|who's\b)", lowered))

    is_message_query = (
        not identity_intent
        and (date_str is not None
             or bool(name_hint)
             or any(kw in lowered for kw in msg_keywords))
    )

    # Get curated context facts
    curated_results = _get_curated_facts(input_str, fact_pool, use_global_cache, k=k)

    # Get verbatim messages if requested. Gated to the primary user: the message
    # archive is Brian's/Jenn's private data and must not leak into other users'
    # scoped retrieval.
    verbatim_results = []
    if is_message_query and use_global_cache:
        verbatim_results = _get_verbatim_messages(input_str, fact_pool, k=15)

    if verbatim_results:
        # For a message query, lead with the actual message threads, then add a
        # few curated facts for grounding (who people are, dates, family).
        results = verbatim_results + curated_results[:3]
    else:
        results = curated_results[:k]

    _FACTS_RESULT_CACHE[cache_key] = (results, time.time())
    return results


def get_fact(input_str: str, user_scope: Optional[str] = None):
    """Backwards-compatible: return a single best fact (first of top-k)."""
    facts = get_facts(input_str, k=1, user_scope=user_scope)
    return facts[0] if facts else None


def remember(message):
    # For future runtime learning
    pass


def recall(n: int = 10):
    lines = []
    for fact in memory[:n]:
        i = fact.get("input")
        o = fact.get("output")
        meta = fact.get("_meta") or {}
        label = meta.get("source_type", "unknown")
        if i and o:
            lines.append(f"[{label}] {i} -> {o}")
        else:
            lines.append(f"[{label}] {o or i or '(empty)'}")
    return "\n".join(lines)


load_facts()
