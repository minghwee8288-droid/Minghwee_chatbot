"""Help's settings, read from the process environment on every call.

Deliberately not fields on app/config.py (the pattern of app/suggest/settings.py):
Help is additive and must not change how the bot's own settings load. In Docker,
env_file puts every .env line into the process environment; locally they have to
be exported in the shell.
"""

from __future__ import annotations

import os

from app.config import settings as bot_settings

DEFAULT_MAX_CONCURRENCY = 4
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_PLANNER_TIMEOUT_SECONDS = 6.0
DEFAULT_HISTORY_TURNS = 10
DEFAULT_MATCH_COUNT = 8

# The app's own body cap (nginx's client_max_body_size is the same, 32k).
MAX_BODY_BYTES = 32 * 1024
MAX_CONTENT_CHARS = 2000
MAX_ROLE_CHARS = 64


def _int(name: str, default: int, low: int = 1, high: int = 1000) -> int:
    try:
        value = int(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return min(max(value, low), high)


def _float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


def secret() -> str:
    return os.environ.get("HELP_SECRET", "")


def model() -> str:
    """HELP_MODEL, else the bot's model (OPENROUTER_MODEL). Never changes the bot's."""
    return (
        os.environ.get("HELP_MODEL", "").strip()
        or os.environ.get("OPENROUTER_MODEL", "").strip()
        or bot_settings.openrouter_model
    )


def max_concurrency() -> int:
    return _int("HELP_MAX_CONCURRENCY", DEFAULT_MAX_CONCURRENCY, 1, 64)


def timeout_seconds() -> float:
    return _float("HELP_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)


def planner_timeout_seconds() -> float:
    return _float("HELP_PLANNER_TIMEOUT_SECONDS", DEFAULT_PLANNER_TIMEOUT_SECONDS)


def history_turns() -> int:
    return _int("HELP_HISTORY_TURNS", DEFAULT_HISTORY_TURNS, 1, 50)


def match_count() -> int:
    return _int("HELP_MATCH_COUNT", DEFAULT_MATCH_COUNT, 1, 20)
