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

    # Verbose brain-call debug logging. OFF by default. When ON, every brain
    # request/response is logged to the LOCAL client log (BRAIN_REQUEST /
    # BRAIN_RESPONSE), each field truncated to CLIENT_LLM_DEBUG_MAX_CHARS. These
    # payloads carry no raw row values by design (Art. II); the logs stay on the
    # local host. A failed brain call always logs its HTTP status + body snippet
    # regardless of this flag.
    CLIENT_LLM_DEBUG: bool = Field(default_factory=lambda: os.getenv("CLIENT_LLM_DEBUG", "").strip().lower() in ("1", "true", "yes", "on"))
    CLIENT_LLM_DEBUG_MAX_CHARS: int = Field(default_factory=lambda: int(os.getenv("CLIENT_LLM_DEBUG_MAX_CHARS", "20000")))

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
