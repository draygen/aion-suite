"""Authentication, PostgreSQL sessions, and decorators for Aion."""
import os
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

import bcrypt
import psycopg2
import psycopg2.extras
from flask import g, jsonify, request

from aion_logging import get_logger
from config import CONFIG
from db import get_conn, dict_cursor

MIN_PASSWORD_LENGTH = 10
PASSWORD_CHANGE_ALLOWED_PATHS = {"/api/change-password", "/api/whoami", "/api/logout"}
GLOBAL_CHANNEL = "global"
logger = get_logger("auth")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _parse_db_datetime(value) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id      SERIAL PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    pw_hash  TEXT NOT NULL,
                    role     TEXT NOT NULL DEFAULT 'user',
                    must_change_password INTEGER NOT NULL DEFAULT 0,
                    created  TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tokens (
                    token    TEXT PRIMARY KEY,
                    user_id  INTEGER NOT NULL,
                    expires  TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS history (
                    id         SERIAL PRIMARY KEY,
                    user_id    INTEGER NOT NULL,
                    role       TEXT NOT NULL,
                    content    TEXT NOT NULL,
                    ts         TEXT NOT NULL,
                    session_id TEXT,
                    channel    TEXT,
                    thread_id  TEXT,
                    message_id TEXT,
                    author_username TEXT
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_history_user ON history(user_id, id DESC)
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS channels (
                    name         TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    is_private   INTEGER NOT NULL DEFAULT 0,
                    created_by   INTEGER,
                    created      TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS channel_memberships (
                    channel_name    TEXT NOT NULL,
                    user_id         INTEGER NOT NULL,
                    membership_role TEXT NOT NULL DEFAULT 'member',
                    invited_by      INTEGER,
                    joined          TEXT NOT NULL,
                    PRIMARY KEY (channel_name, user_id)
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_channel_memberships_user
                    ON channel_memberships(user_id, channel_name)
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS channel_invites (
                    channel_name    TEXT NOT NULL,
                    invitee_user_id INTEGER NOT NULL,
                    invited_by      INTEGER NOT NULL,
                    created         TEXT NOT NULL,
                    accepted        TEXT,
                    revoked         TEXT,
                    PRIMARY KEY (channel_name, invitee_user_id)
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_channel_invites_user
                    ON channel_invites(invitee_user_id, channel_name)
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS channel_presence (
                    channel_name TEXT NOT NULL,
                    occupant_key TEXT NOT NULL,
                    user_id      INTEGER,
                    display_name TEXT NOT NULL,
                    is_system    INTEGER NOT NULL DEFAULT 0,
                    joined       TEXT NOT NULL,
                    last_seen    TEXT NOT NULL,
                    PRIMARY KEY (channel_name, occupant_key)
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_channel_presence_seen
                    ON channel_presence(channel_name, last_seen)
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id         SERIAL PRIMARY KEY,
                    user_id    INTEGER,
                    session_id TEXT,
                    channel    TEXT,
                    thread_id  TEXT,
                    message_id TEXT,
                    event_type TEXT NOT NULL,
                    source     TEXT NOT NULL,
                    tool_name  TEXT,
                    content    TEXT,
                    payload    TEXT,
                    ts         TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id, id DESC)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, id DESC)
            """)

            # Add any new columns that may not exist yet (idempotent)
            _ensure_column(cur, "users", "must_change_password", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(cur, "history", "session_id", "TEXT")
            _ensure_column(cur, "history", "channel", "TEXT")
            _ensure_column(cur, "history", "thread_id", "TEXT")
            _ensure_column(cur, "history", "message_id", "TEXT")
            _ensure_column(cur, "history", "author_username", "TEXT")

        conn.commit()

        # Create Brian's admin account on first run
        admin_pass = CONFIG.get("admin_password", "")
        with dict_cursor(conn) as cur:
            cur.execute("SELECT id FROM users WHERE username = 'brian'")
            existing_brian = cur.fetchone()

        if not existing_brian:
            if not admin_pass:
                admin_pass = secrets.token_urlsafe(18)
                logger.warning("No admin_password configured. Generated bootstrap password for 'brian'.")
                logger.warning("Bootstrap password: %s", admin_pass)
            pw_hash = bcrypt.hashpw(admin_pass.encode(), bcrypt.gensalt()).decode()
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (username, pw_hash, role, must_change_password, created) "
                    "VALUES (%s, %s, 'admin', 1, %s)",
                    ("brian", pw_hash, _utc_now_iso()),
                )
            conn.commit()
            logger.info("Created admin user: brian")
        else:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET role = 'admin' WHERE username = 'brian'")
            conn.commit()
            _mark_bootstrap_password_if_needed(conn, "brian", admin_pass)

        _ensure_default_channels(conn)

    _migrate_learned_facts()


def _ensure_column(cur, table: str, column: str, definition: str) -> None:
    cur.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = %s",
        (table, column),
    )
    if not cur.fetchone():
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _mark_bootstrap_password_if_needed(conn, username: str, configured_password: str) -> None:
    if not configured_password:
        return
    with dict_cursor(conn) as cur:
        cur.execute(
            "SELECT id, pw_hash, must_change_password FROM users WHERE username = %s",
            (username.lower(),),
        )
        row = cur.fetchone()
    if not row or row["must_change_password"]:
        return
    try:
        if bcrypt.checkpw(configured_password.encode(), row["pw_hash"].encode()):
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET must_change_password = 1 WHERE id = %s",
                    (row["id"],),
                )
            conn.commit()
    except ValueError:
        return


def _ensure_default_channels(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO channels (name, display_name, is_private, created_by, created) "
            "VALUES (%s, %s, 0, NULL, %s) ON CONFLICT DO NOTHING",
            (GLOBAL_CHANNEL, "Global", _utc_now_iso()),
        )
    with dict_cursor(conn) as cur:
        cur.execute("SELECT id FROM users WHERE username = 'brian'")
        brian = cur.fetchone()
    if brian:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO channel_memberships "
                "(channel_name, user_id, membership_role, invited_by, joined) "
                "VALUES (%s, %s, 'owner', NULL, %s) ON CONFLICT DO NOTHING",
                (GLOBAL_CHANNEL, brian["id"], _utc_now_iso()),
            )
    conn.commit()


def _migrate_learned_facts():
    old_path = "data/user_learned.jsonl"
    new_path = CONFIG.get("shared_facts_file", "data/shared_learned.jsonl")
    if os.path.exists(old_path) and not os.path.exists(new_path):
        import shutil
        shutil.copy2(old_path, new_path)
        logger.info("Migrated %s -> %s", old_path, new_path)


def _validate_new_password(password: str) -> "str | None":
    if not password:
        return "Password is required."
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    return None


def create_user(
    username: str,
    password: str,
    role: str = "user",
    must_change_password: bool = True,
) -> int:
    err = _validate_new_password(password)
    if err:
        raise ValueError(err)
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (username, pw_hash, role, must_change_password, created) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (username.lower(), pw_hash, role, 1 if must_change_password else 0, _utc_now_iso()),
            )
            user_id = cur.fetchone()[0]
        conn.commit()
    return user_id


def verify_login(username: str, password: str):
    with get_conn() as conn:
        with dict_cursor(conn) as cur:
            cur.execute("SELECT * FROM users WHERE username = %s", (username.lower(),))
            row = cur.fetchone()
    if not row:
        return None
    if bcrypt.checkpw(password.encode(), row["pw_hash"].encode()):
        return dict(row)
    return None


def create_token(user_id: int) -> str:
    token = secrets.token_hex(32)
    expires = (_utc_now() + timedelta(days=30)).isoformat()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tokens (token, user_id, expires) VALUES (%s, %s, %s)",
                (token, user_id, expires),
            )
        conn.commit()
    return token


def change_password(user_id: int, current_password: str, new_password: str) -> "str | None":
    err = _validate_new_password(new_password)
    if err:
        return err

    with get_conn() as conn:
        with dict_cursor(conn) as cur:
            cur.execute("SELECT pw_hash FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
        if not row:
            return "User not found."
        if not bcrypt.checkpw(current_password.encode(), row["pw_hash"].encode()):
            return "Current password is incorrect."
        if bcrypt.checkpw(new_password.encode(), row["pw_hash"].encode()):
            return "New password must be different from the current password."

        new_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET pw_hash = %s, must_change_password = 0 WHERE id = %s",
                (new_hash, user_id),
            )
        conn.commit()
    return None


def get_user_by_token(token: str):
    with get_conn() as conn:
        with dict_cursor(conn) as cur:
            cur.execute(
                "SELECT u.*, t.expires FROM users u "
                "JOIN tokens t ON u.id = t.user_id WHERE t.token = %s",
                (token,),
            )
            row = cur.fetchone()
        if not row:
            return None
        if _parse_db_datetime(row["expires"]) < _utc_now():
            with conn.cursor() as cur:
                cur.execute("DELETE FROM tokens WHERE token = %s", (token,))
            conn.commit()
            return None
        # Sliding 30-day expiry
        new_expires = (_utc_now() + timedelta(days=30)).isoformat()
        with conn.cursor() as cur:
            cur.execute("UPDATE tokens SET expires = %s WHERE token = %s", (new_expires, token))
        conn.commit()
        return dict(row)


def delete_token(token: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM tokens WHERE token = %s", (token,))
        conn.commit()


def delete_user(user_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
            cur.execute("DELETE FROM tokens WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM channel_memberships WHERE user_id = %s", (user_id,))
            cur.execute(
                "DELETE FROM channel_invites WHERE invitee_user_id = %s OR invited_by = %s",
                (user_id, user_id),
            )
            cur.execute("DELETE FROM channel_presence WHERE user_id = %s", (user_id,))
        conn.commit()


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.cookies.get("aion_token")
        if not token:
            return jsonify({"error": "Unauthorized", "login_required": True}), 401
        user = get_user_by_token(token)
        if not user:
            return jsonify({"error": "Unauthorized", "login_required": True}), 401
        if user.get("must_change_password") and request.path not in PASSWORD_CHANGE_ALLOWED_PATHS:
            return jsonify({"error": "Password change required", "requires_password_change": True}), 403
        g.user = user
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.cookies.get("aion_token")
        if not token:
            return jsonify({"error": "Unauthorized", "login_required": True}), 401
        user = get_user_by_token(token)
        if not user:
            return jsonify({"error": "Unauthorized", "login_required": True}), 401
        if user.get("must_change_password") and request.path not in PASSWORD_CHANGE_ALLOWED_PATHS:
            return jsonify({"error": "Password change required", "requires_password_change": True}), 403
        if user["role"] != "admin":
            return jsonify({"error": "Forbidden"}), 403
        g.user = user
        return f(*args, **kwargs)
    return decorated


def vast_required(f):
    """Allow access to users with 'admin' or 'vast' role."""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.cookies.get("aion_token")
        if not token:
            return jsonify({"error": "Unauthorized", "login_required": True}), 401
        user = get_user_by_token(token)
        if not user:
            return jsonify({"error": "Unauthorized", "login_required": True}), 401
        if user.get("must_change_password") and request.path not in PASSWORD_CHANGE_ALLOWED_PATHS:
            return jsonify({"error": "Password change required", "requires_password_change": True}), 403
        if user["role"] not in ("admin", "vast"):
            return jsonify({"error": "Forbidden"}), 403
        g.user = user
        return f(*args, **kwargs)
    return decorated


# Legacy compatibility: some modules call get_db() directly
def get_db():
    """Return a SQLite-compatible connection wrapper backed by PostgreSQL.

    Callers must call conn.close() when done (returns connection to pool).
    Prefer using get_conn() context manager for new code.
    """
    from db import get_compat_conn
    return get_compat_conn()
