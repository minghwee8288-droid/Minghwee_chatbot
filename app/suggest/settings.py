"""Settings for the suggestion endpoint, read from the process environment.

Deliberately not fields on app/config.py: the endpoint is additive and must not
change how the bot's own settings load. In Docker, env_file puts every .env
line into the process environment, so these are read there. Locally they have
to be exported in the shell (pydantic loads .env into `settings`, not into
os.environ).

Read on every call rather than cached, so a test can patch os.environ.
"""

from __future__ import annotations

import os

from app.config import settings

DEFAULT_MAX_CONCURRENCY = 2
DEFAULT_TIMEOUT_SECONDS = 20.0


def secret() -> str:
    return os.environ.get("SUGGEST_SECRET", "")


def model() -> str:
    return os.environ.get("SUGGEST_MODEL", "").strip() or settings.openrouter_model


def max_concurrency() -> int:
    try:
        return max(1, int(os.environ.get("SUGGEST_MAX_CONCURRENCY", DEFAULT_MAX_CONCURRENCY)))
    except ValueError:
        return DEFAULT_MAX_CONCURRENCY


def timeout_seconds() -> float:
    try:
        value = float(os.environ.get("SUGGEST_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_TIMEOUT_SECONDS
