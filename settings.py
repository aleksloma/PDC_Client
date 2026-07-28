"""Client (on-prem) runtime settings.

Trimmed-down version of the B2C settings.py:
  - DATA_ROOT is the LOCAL filesystem path on the client server (raw data
    stays here per the architecture).
  - BRAIN_URL + BRAIN_TENANT_TOKEN are injected per-tenant when the container
    is installed for a customer.
  - GOOGLE_API_KEY is absent — the client never calls Gemini.
"""
from dotenv import load_dotenv
load_dotenv()

import os
from pydantic import BaseModel, Field


class Settings(BaseModel):
    DATA_ROOT: str = Field(default_factory=lambda: os.getenv("DATA_ROOT", "./client_data"))
    MAX_FILES: int = Field(default_factory=lambda: int(os.getenv("MAX_FILES", "5")))
    SECRET_KEY: str = Field(default_factory=lambda: os.getenv("SECRET_KEY", "replace-me-in-prod"))

    # Brain connection (injected per-tenant at install time)
    BRAIN_URL: str = Field(default_factory=lambda: os.getenv("BRAIN_URL", "http://localhost:8080"))
    BRAIN_TENANT_TOKEN: str = Field(default_factory=lambda: os.getenv("BRAIN_TENANT_TOKEN", ""))
    BRAIN_REQUEST_TIMEOUT: float = Field(default_factory=lambda: float(os.getenv("BRAIN_REQUEST_TIMEOUT", "180.0")))

    # Same prompt-trim defaults as the B2C app (used by the client when it
    # builds schema text and history before posting to the brain).
    PROMPT_HISTORY_TRIM_CHARS: int = Field(default_factory=lambda: int(os.getenv("PROMPT_HISTORY_TRIM_CHARS", "800")))
    PROMPT_LOG_SNIPPET_CHARS: int = Field(default_factory=lambda: int(os.getenv("PROMPT_LOG_SNIPPET_CHARS", "800")))
    PROMPT_RESULT_JSON_TRIM_CHARS: int = Field(default_factory=lambda: int(os.getenv("PROMPT_RESULT_JSON_TRIM_CHARS", "4000")))
    FINAL_FALLBACK_PREVIEW_CHARS: int = Field(default_factory=lambda: int(os.getenv("FINAL_FALLBACK_PREVIEW_CHARS", "1000")))
    STR_PREVIEW_MAX_CHARS: int = Field(default_factory=lambda: int(os.getenv("STR_PREVIEW_MAX_CHARS", "4000")))
    CODE_HASH_LEN: int = Field(default_factory=lambda: int(os.getenv("CODE_HASH_LEN", "10")))
    EXEC_ERROR_SNIPPET_LINES: int = Field(default_factory=lambda: int(os.getenv("EXEC_ERROR_SNIPPET_LINES", "6")))
    PREVIEW_HEAD_ROWS: int = Field(default_factory=lambda: int(os.getenv("PREVIEW_HEAD_ROWS", "10")))
    FIGURE_DPI: int = Field(default_factory=lambda: int(os.getenv("FIGURE_DPI", "150")))
    SCHEMA_MAX_UNIQUE_LIST: int = Field(default_factory=lambda: int(os.getenv("SCHEMA_MAX_UNIQUE_LIST", "30")))
    SCHEMA_CAT_LIMIT: int = Field(default_factory=lambda: int(os.getenv("SCHEMA_CAT_LIMIT", "20")))

    # Schema autofill — same defaults as global (backend/routes/schema.py).
    # These shape the per-file context the client builds BEFORE posting to the
    # brain so the brain receives only what global itself would feed the LLM.
    SCHEMA_AUTOFILL_MAX_COLS: int = Field(default_factory=lambda: int(os.getenv("SCHEMA_AUTOFILL_MAX_COLS", "50")))
    SCHEMA_AUTOFILL_UNIQUE_THRESHOLD: int = Field(default_factory=lambda: int(os.getenv("SCHEMA_AUTOFILL_UNIQUE_THRESHOLD", "10")))
    SCHEMA_AUTOFILL_SAMPLE_VALUES: int = Field(default_factory=lambda: int(os.getenv("SCHEMA_AUTOFILL_SAMPLE_VALUES", "3")))
    SCHEMA_AUTOFILL_VALUE_TRUNC: int = Field(default_factory=lambda: int(os.getenv("SCHEMA_AUTOFILL_VALUE_TRUNC", "60")))
    SCHEMA_AUTOFILL_NOTES_MAX_CHARS: int = Field(default_factory=lambda: int(os.getenv("SCHEMA_AUTOFILL_NOTES_MAX_CHARS", "2000")))
    SCHEMA_AUTOFILL_HINTS_JSON_MAX_CHARS: int = Field(default_factory=lambda: int(os.getenv("SCHEMA_AUTOFILL_HINTS_JSON_MAX_CHARS", "3000")))
    SCHEMA_AUTOFILL_DESC_WORD_LIMIT: int = Field(default_factory=lambda: int(os.getenv("SCHEMA_AUTOFILL_DESC_WORD_LIMIT", "20")))

    # In-memory DataFrame cache budget (MB). Parsed dataframes for recently
    # active chats are kept in RAM to avoid re-reading the disk cache on every
    # question; total residency is bounded by this budget and entries expire
    # after a short TTL, so an idle container returns to baseline memory.
    DF_CACHE_MAX_MB: int = Field(default_factory=lambda: int(os.getenv("DF_CACHE_MAX_MB", "500")))

    # --- Database sources (admin-registered tables → parquet snapshots) -----
    # Fernet key for encrypting DB connection passwords at rest in
    # data_sources.json. Generate once per install:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # Never logged, never sent to the brain. When rotating, put the previous
    # key in CLIENT_ENCRYPTION_KEY_OLD so stored passwords stay readable.
    CLIENT_ENCRYPTION_KEY: str = Field(default_factory=lambda: os.getenv("CLIENT_ENCRYPTION_KEY", ""))
    CLIENT_ENCRYPTION_KEY_OLD: str = Field(default_factory=lambda: os.getenv("CLIENT_ENCRYPTION_KEY_OLD", ""))

    # Fixed local admin account (the only role=admin user in Phase 1).
    # LOCAL_ADMIN_PASSWORD bootstraps the account ONCE (hash-only on disk,
    # forced change on first login); an existing hash is never overwritten.
    LOCAL_ADMIN_USERNAME: str = Field(default_factory=lambda: os.getenv("LOCAL_ADMIN_USERNAME", "ladmin"))
    LOCAL_ADMIN_PASSWORD: str = Field(default_factory=lambda: os.getenv("LOCAL_ADMIN_PASSWORD", ""))

    # Connector tuning. The connector only ever issues SELECT/introspection
    # statements; timeouts bound each connection attempt / snapshot query.
    DB_CONNECT_TIMEOUT: int = Field(default_factory=lambda: int(os.getenv("DB_CONNECT_TIMEOUT", "8")))
    DB_STATEMENT_TIMEOUT: int = Field(default_factory=lambda: int(os.getenv("DB_STATEMENT_TIMEOUT", "300")))
    DB_SNAPSHOT_CHUNK_ROWS: int = Field(default_factory=lambda: int(os.getenv("DB_SNAPSHOT_CHUNK_ROWS", "100000")))
    DB_PREVIEW_ROWS: int = Field(default_factory=lambda: int(os.getenv("DB_PREVIEW_ROWS", "20")))
    DB_MAX_AUTO_CONNECTORS: int = Field(default_factory=lambda: int(os.getenv("DB_MAX_AUTO_CONNECTORS", "25")))

    # Nightly snapshot refresh (container-local time; the admin UI value in
    # data_sources.json wins once set — these are first-boot defaults only).
    DB_REFRESH_TIME: str = Field(default_factory=lambda: os.getenv("DB_REFRESH_TIME", "00:00"))
    DB_REFRESH_ENABLED: bool = Field(default_factory=lambda: os.getenv("DB_REFRESH_ENABLED", "1") not in ("0", "false", "False", ""))
    DB_REFRESH_LOCK_STALE_S: int = Field(default_factory=lambda: int(os.getenv("DB_REFRESH_LOCK_STALE_S", "21600")))

    # Title display defaults (used by the dashboard sidebar)
    TITLE_MAX_LEN: int = Field(default_factory=lambda: int(os.getenv("TITLE_MAX_LEN", "80")))
    TITLE_BREAK_MIN: int = Field(default_factory=lambda: int(os.getenv("TITLE_BREAK_MIN", "40")))
    TITLE_LISTING_MAX_LEN: int = Field(default_factory=lambda: int(os.getenv("TITLE_LISTING_MAX_LEN", "120")))

    CHAT_ACTIVE_DEFAULT_DAYS: int = 36500
    CHAT_ACTIVE_MAX_DAYS: int = 36500

    # Stubs kept to satisfy any settings.X reference from the copied
    # code_exec/plot_utils helpers.
    GOOGLE_API_KEY: str = ""
    LLM_LIGHT_MODEL: str = "gemini-2.5-flash"
    LLM_SIMPLE_MODEL: str = "gemini-2.5-pro"
    LLM_COMPLEX_MODEL: str = "gemini-2.5-pro"
    LLM_AGENT_MODEL: str = "gemini-2.5-pro"
    PROMPT_HISTORY_TRIM_CHARS_DEFAULT: int = 800
    CODE_LOG_SNIPPET_CHARS: int = 800
    LOG_Q_SNIPPET_CHARS: int = 160
    TRACEBACK_LIMIT: int = 3


settings = Settings()
