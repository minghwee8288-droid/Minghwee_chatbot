"""Help's request, validated by hand (no FastAPI body model).

The body is not a declared route parameter: FastAPI would decode it before the
secret is checked and answer 422 to anyone (the §9.32 lesson). The route checks
the secret first, then reads the bytes and calls parse() here.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from app.help import settings as help_settings


class InvalidRequest(ValueError):
    """The body is not a valid Help request (422)."""


@dataclass(frozen=True)
class Message:
    role: str
    content: str


@dataclass(frozen=True)
class HelpRequest:
    request_id: str
    user_role: str
    messages: tuple[Message, ...]

    @property
    def last_user(self) -> str:
        return self.messages[-1].content


def parse(raw: bytes) -> HelpRequest:
    try:
        body = json.loads(raw or b"")
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidRequest("body is not JSON") from exc
    if not isinstance(body, dict):
        raise InvalidRequest("body must be a JSON object")

    request_id = body.get("request_id")
    if not isinstance(request_id, str):
        raise InvalidRequest("request_id must be a string")
    try:
        request_id = str(uuid.UUID(request_id))
    except ValueError as exc:
        raise InvalidRequest("request_id must be a UUID") from exc

    role = body.get("user_role")
    if not isinstance(role, str) or not role.strip() or len(role) > help_settings.MAX_ROLE_CHARS:
        raise InvalidRequest(f"user_role must be a non-empty string of at most {help_settings.MAX_ROLE_CHARS} characters")

    messages = body.get("messages")
    limit = help_settings.history_turns()
    if not isinstance(messages, list) or not 1 <= len(messages) <= limit:
        raise InvalidRequest(f"messages must be a list of 1 to {limit} items")
    parsed: list[Message] = []
    for index, item in enumerate(messages):
        if not isinstance(item, dict):
            raise InvalidRequest(f"messages[{index}] must be an object")
        msg_role = item.get("role")
        content = item.get("content")
        if msg_role not in ("user", "assistant"):
            raise InvalidRequest(f"messages[{index}].role must be 'user' or 'assistant'")
        if not isinstance(content, str) or not content.strip():
            raise InvalidRequest(f"messages[{index}].content must be a non-empty string")
        if len(content) > help_settings.MAX_CONTENT_CHARS:
            raise InvalidRequest(f"messages[{index}].content is over {help_settings.MAX_CONTENT_CHARS} characters")
        parsed.append(Message(msg_role, content))
    if parsed[-1].role != "user":
        raise InvalidRequest("the last message must have role 'user'")
    return HelpRequest(request_id=request_id, user_role=role.strip(), messages=tuple(parsed))
