import os


CONFIG = {
    "model": "qwen3.5:9b",
    "backend": "ollama",
    "OLLAMA_BASE_URL": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
    "OLLAMA_EMBED_MODEL": "nomic-embed-text",
    "retrieval": "embed",  # embed | lexical
    "embed_backend": "tfidf",  # tfidf | (legacy: ollama)
    "primary_user": "brian",
    "shared_fact_files": [],
    "user_fact_files": {
        "brian": [
            "data/profile.jsonl",            # curated identity facts (highest priority)
            "data/brian_facts.jsonl",
            "data/fb_qa_pairs.jsonl",
            "data/fb_messages_parsed.jsonl", # Brian's FB messages
            "data/jenn_messages.jsonl",      # Jennifer's FB messages (verbatim, with from/to/date)
        ],
    },
    "facts_files": [
        "data/profile.jsonl",            # curated identity facts (highest priority)
        "data/brian_facts.jsonl",
        "data/fb_qa_pairs.jsonl",
        "data/fb_messages_parsed.jsonl", # Brian's FB messages
        "data/jenn_messages.jsonl",      # Jennifer's FB messages (verbatim, with from/to/date)
    ],
    "openai_api_key": "",
    "mistral_api_key": "",               # set in config_local.py — enables Voxtral TTS
    "voxtral_voice_id": "Paul",          # built-in Voxtral voice (Paul, Oliver, Marie, etc.)
    "elevenlabs_api_key": "",            # set in config.local.py
    "elevenlabs_voice_id": "pNInz6obpgDQGcFmaJgB",  # "Adam" - deep male voice (default)
    "vast_api_key": "",                  # set in config.local.py
    "vast_ssh_key": "~/.ssh/id_ed25519",
    "vast_repo_url": "https://github.com/draygen/drayhub-platform.git",
    "vast_repo_branch": "main",
    "admin_key": "",                     # set in config.local.py
    "admin_password": "",                # set in config.local.py
    "auto_extract_facts": True,
    "auto_extract_mode": "pending",      # pending | shared | off
    "shared_facts_file": "data/shared_learned.jsonl",
    "pending_facts_file": "data/pending_learned.jsonl",
    "user_memory_dir": "data/users",
    "legacy_shared_fact_owner": "brian",
    "cors_origins": [
        "http://localhost",
        "http://127.0.0.1",
        "http://localhost:5000",
        "http://127.0.0.1:5000",
        "http://localhost:3001",   # PromptGen server (WSL → Windows via mirrored networking)
        "http://localhost:5173",   # PromptGen client (Vite)
        "null",  # Electron/file:// clients
    ],
    "cookie_samesite": "Lax",
    "cookie_secure": False,
    "service_token": os.getenv("AION_SERVICE_TOKEN", "change-me-service-token"),
    "log_level": "INFO",
    "log_file": "data/logs/aion.log",
    "log_max_bytes": 1000000,
    "log_backup_count": 3,
    "memory_browser_requires_auth": True,
    "load_pending_facts": False,
    "authorized_network_targets": [
        "localhost",
        "127.0.0.1",
        "::1",
    ],
    "network_ops_enabled": True,
    "TTS_ENABLED": False,
    "VOICE_MODE": False,
    "whisper_model": "base",
    # Memory / Goals (Phase 1 — Sapphire port)
    "EMBEDDING_PROVIDER": "null",   # null | local | api
    "EMBEDDING_API_URL": "",        # remote OpenAI-compatible embeddings endpoint
    "EMBEDDING_API_KEY": "",
    "USER_TIMEZONE": "America/New_York",
    "memory_enabled": True,
    "goals_enabled": True,
    # Read-only fleet gateway (mcpbuilder `npm run gateway`) that backs the
    # /fleet topology page's machine/agent health.
    "fleet_gateway_url": os.getenv("FLEET_GATEWAY_URL", "http://127.0.0.1:5100"),
}

# Load local overrides (API keys, passwords — never committed to git)
try:
    from config_local import CONFIG_LOCAL
    CONFIG.update(CONFIG_LOCAL)
except ImportError:
    pass

# Environment variables should be able to override local secrets/config in
# deploy scripts without editing config_local.py.
for _env_key in (
    "DATABASE_URL",
    "OLLAMA_BASE_URL",
    "AION_SERVICE_TOKEN",
):
    if os.getenv(_env_key):
        CONFIG[_env_key] = os.getenv(_env_key)

if os.getenv("AION_SERVICE_TOKEN"):
    CONFIG["service_token"] = os.getenv("AION_SERVICE_TOKEN")
