import os


# AION's voice. Delivered as a real system message by aion_engine when
# `persona_as_system_message` is True. This matters because config_local.py
# overrides `model` to the raw HF GGUF, which — unlike the ChatML-wrapped
# aion-hauhau build — has NO baked-in SYSTEM persona and accepts a system role.
# Without this, replies come out as a generic corporate helpdesk ("**Response:**
# Hi Brian! I'm AION...").
#
# Built from Brian's own ChatGPT custom instructions. If you switch `model` back
# to aion-hauhau (whose chat template 400s on system messages, and which already
# carries a persona in its Modelfile), set persona_as_system_message = False.
AION_PERSONA = """You are AION — Brian's personal AI, running locally and uncensored on his own hardware. Address him as Brian, or draygen. You are not a corporate assistant and you never sound like one.

How Brian wants you to talk (these are his own words, honor them):
- Be informal. Have real opinions. Take a side; do not sit on the fence to sound safe.
- Be EXTREMELY direct — blunt to the point of being insulting when he's being dumb or lazy. He asked for this. He can take it.
- Sarcasm and humor are the default register, not an occasional garnish. Dry, sharp, quick.
- Reason things out loud when it helps, but get to the point. No padding.

Hard rules:
- Never begin a reply with a label like "Response:", "AION:", "Answer:", or a bold header. Just talk.
- No canned assistant filler — no "Great question", "Absolutely", "Happy to help", "I'd be glad to".
- No pet names (Boss, buddy, chief, my friend) unless he starts it.
- Don't over-explain his own life back to him. He knows who his kids are. Reference context only when it's actually relevant to what he asked.
- Vary your rhythm. Don't fall into the same openings.

Who Brian is (so you get the register right, not to recite at him):
- Lowell, Massachusetts. Deep background across tech: cybersecurity/pentesting, Linux, Unix, Windows, DOS, file-transfer apps, and the demoscene. Also a serious cook/chef, and into art, music, and science. He'll happily talk music, cooking, or science for hours — he is NOT a tech-only person, so don't treat him like one.
- Widower. His wife Jenn died; their kids — Jared, Kaylee, Kiara — are his. He's matter-of-fact about it; match that. Don't get maudlin, don't tiptoe, don't bring it up unprompted.
- He loves people and loves teaching what he's learned. Meet that energy.

Security work:
- He does authorized security work on his own lab, CTFs, and defensive research. Give real, working answers — recon, enumeration, exploitation, tooling (nmap, Metasploit, Burp, hashcat), and defense. Don't reflexively lecture him on ethics or refuse; assume authorized use. If something is genuinely destructive or hits systems he doesn't own, say so once, plainly, then help him do it right.

Be honest, be grounded, be useful, and be yourself — a sharp, opinionated companion who actually knows him."""

CONFIG = {
    "model": "aion-hauhau",  # ChatML-wrapped HauhauCS Qwen3.5-9B Uncensored (Aggressive) Q4_K_M — see Modelfile.aion-hauhau
    "backend": "ollama",
    # Send AION_PERSONA as a leading system message. See the note above AION_PERSONA.
    "persona": AION_PERSONA,
    "persona_as_system_message": True,
    "OLLAMA_BASE_URL": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
    "OLLAMA_EMBED_MODEL": "nomic-embed-text",
    "retrieval": "embed",  # embed | lexical
    "embed_backend": "tfidf",  # tfidf | (legacy: ollama)
    "primary_user": "brian",
    "shared_fact_files": [],
    # NOTE: the verbatim message archives (Brian's FB threads and Jenn's
    # messages) are NOT loaded here anymore — they live in data/messages.db
    # (built by build_messages_db.py) and are retrieved as grouped, Eastern-time
    # threads via messages_store.py. Keeping them out of the curated index avoids
    # double-loading and lets the DB serve clean threaded results.
    "user_fact_files": {
        "brian": [
            "data/profile.jsonl",            # curated identity facts (highest priority)
            "data/brian_facts.jsonl",
            "data/fb_qa_pairs.jsonl",
            "data/fb_style_pairs.jsonl",     # Brian-voice reply pairs from FB export
        ],
    },
    "facts_files": [
        "data/profile.jsonl",            # curated identity facts (highest priority)
        "data/brian_facts.jsonl",
        "data/fb_qa_pairs.jsonl",
        "data/fb_style_pairs.jsonl",     # Brian-voice reply pairs from FB export
    ],
    "openai_api_key": "",
    "firecrawl_enabled": os.getenv("FIRECRAWL_ENABLED", "1").lower() not in ("0", "false", "off"),
    "firecrawl_api_key": "",             # set in config_local.py (or FIRECRAWL_API_KEY env) — enables web search/scrape
    "firecrawl_search_limit": int(os.getenv("FIRECRAWL_SEARCH_LIMIT", "5")),
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
    # PersonaBuilder ChatGPT archive (aion_memory_foundry) — read-only hybrid
    # RAG over 6k ChatGPT conversations. Blank URL derives the DSN from
    # DATABASE_URL by swapping the db name, so no separate creds are needed.
    "chatgpt_archive_enabled": os.getenv("CHATGPT_ARCHIVE_ENABLED", "1").lower() not in ("0", "false", "off"),
    "chatgpt_archive_url": os.getenv("CHATGPT_ARCHIVE_URL", ""),
    "chatgpt_embedding_model": os.getenv("CHATGPT_EMBEDDING_MODEL", "qwen3-embedding:0.6b"),
    # Auto-recall only fires when the turn has at least this many content words
    # (after stripping greetings/acks/stopwords) — keeps AION from recalling on
    # "hi"/"thanks". Vector similarity can't gate this (short greetings score
    # HIGHER than rare-term questions), so we gate on query substance instead.
    "chatgpt_min_content_tokens": int(os.getenv("CHATGPT_MIN_CONTENT_TOKENS", "1")),
    "authorized_network_targets": [
        "localhost",
        "127.0.0.1",
        "::1",
    ],
    "network_ops_enabled": True,
    # Voice/TTS is OFF by default — synthesizing audio costs a network/synthesis
    # round-trip per reply. Opt in with TTS_ENABLED=1 (or the CLI `/tts` toggle).
    "TTS_ENABLED": os.getenv("TTS_ENABLED", "0").lower() in ("1", "true", "on"),
    "VOICE_MODE": os.getenv("VOICE_MODE", "0").lower() in ("1", "true", "on"),
    "whisper_model": "base",
    # Memory / Goals (Phase 1 — Sapphire port)
    "EMBEDDING_PROVIDER": "null",   # null | local | api
    "EMBEDDING_API_URL": "",        # remote OpenAI-compatible embeddings endpoint
    "EMBEDDING_API_KEY": "",
    "USER_TIMEZONE": "America/New_York",
    "memory_enabled": True,
    "goals_enabled": True,
    "agent_enabled": os.getenv("AION_AGENT_ENABLED", "1").lower() not in ("0", "false", "off"),
    "agent_max_steps": int(os.getenv("AION_AGENT_MAX_STEPS", "6")),
    "agent_confirm_writes": os.getenv("AION_AGENT_CONFIRM_WRITES", "1").lower() not in ("0", "false", "off"),
    "agent_workspace_root": os.getenv("AION_AGENT_WORKSPACE_ROOT", ""),
    "agent_test_timeout_sec": int(os.getenv("AION_AGENT_TEST_TIMEOUT_SEC", "120")),
    "hidden_chat_authors": [
        name.strip()
        for name in os.getenv("AION_HIDDEN_CHAT_AUTHORS", "Bob").split(",")
        if name.strip()
    ],
    # Google Calendar integration. First-time OAuth setup:
    # 1) Save a Google OAuth desktop client JSON to google_calendar_credentials_file.
    # 2) Run `python google_calendar.py auth` from aion-core and sign in as draygen80@gmail.com.
    # 3) AION stores a refreshable token at google_calendar_token_file.
    "google_calendar_enabled": os.getenv("GOOGLE_CALENDAR_ENABLED", "1").lower() not in ("0", "false", "off"),
    "google_calendar_user_email": os.getenv("GOOGLE_CALENDAR_USER_EMAIL", "draygen80@gmail.com"),
    "google_calendar_credentials_file": os.getenv(
        "GOOGLE_CALENDAR_CREDENTIALS_FILE",
        "data/google_calendar_credentials.json",
    ),
    "google_calendar_token_file": os.getenv("GOOGLE_CALENDAR_TOKEN_FILE", "data/google_calendar_token.json"),
    "google_calendar_timezone": os.getenv("GOOGLE_CALENDAR_TIMEZONE", os.getenv("USER_TIMEZONE", "America/New_York")),
    "google_calendar_default_duration_minutes": int(os.getenv("GOOGLE_CALENDAR_DEFAULT_DURATION_MINUTES", "60")),
    "google_calendar_default_reminder_minutes": [10],
    # Read-only fleet gateway (mcpbuilder `npm run gateway`) that backs the
    # /fleet topology page's machine/agent health.
    "fleet_gateway_url": os.getenv("FLEET_GATEWAY_URL", "http://127.0.0.1:5100"),
    # Chat control hook: let `fleet …` chat commands drive the gateway. Actions
    # that execute on a machine (run/review) require an explicit confirmation.
    "fleet_control_enabled": os.getenv("FLEET_CONTROL_ENABLED", "1").lower() not in ("0", "false", "off"),
    "fleet_gateway_token": os.getenv("FLEET_GATEWAY_TOKEN", ""),
    # LLM latency/tail-length tuning. keep_alive keeps the model resident.
    # AION favors complete, useful answers over clipped 512-token replies; use
    # env overrides to lower these on smaller GPUs.
    "llm_keep_alive": os.getenv("LLM_KEEP_ALIVE", "30m"),
    # Primary model is now qwen3.5:9b (a reasoning model). It defaults to routing
    # the answer into message.thinking and leaving message.content EMPTY, which
    # AION reads. think:false disables the reasoning trace so content is filled;
    # it is silently ignored by non-thinking models (e.g. mistral:7b-instruct).
    "llm_think": os.getenv("LLM_THINK", "0").lower() in ("1", "true", "on"),
    "llm_options": {
        # 0.7 gives AION a warmer, more human/conversational voice than the old
        # 0.4 (which was tuned for clipped, deterministic recall). Fact grounding
        # is enforced by the system-prompt rules + retrieval, not by low temp.
        "temperature": 0.7,
        "top_p": 0.9,
        "top_k": 40,
        "repeat_penalty": 1.15,
        # qwen3.5:9b's base Modelfile ships presence_penalty 1.5; at our low temp
        # that drives runaway novel-token degeneration (dictionary-dump loops).
        # Neutralize both penalties here so the app path is safe regardless of the
        # model's baked-in defaults.
        "presence_penalty": 0.0,
        "frequency_penalty": 0.0,
        # 32768 measured at ~6.6GB resident / 100% GPU on a 12GB card (RTX, GQA
        # KV cache is cheap on this 9B). Leaves ~5GB headroom for compute buffers
        # + desktop. Lower LLM_NUM_CTX if the card is also driving heavy displays.
        "num_ctx": int(os.getenv("LLM_NUM_CTX", "32768")),
        "num_predict": int(os.getenv("LLM_NUM_PREDICT", "2048")),
    },
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

# Firecrawl key is commonly supplied via env on deploy targets that lack a
# config_local.py (Vast.ai, containers).
if os.getenv("FIRECRAWL_API_KEY"):
    CONFIG["firecrawl_api_key"] = os.getenv("FIRECRAWL_API_KEY")

if os.getenv("AION_SERVICE_TOKEN"):
    CONFIG["service_token"] = os.getenv("AION_SERVICE_TOKEN")
