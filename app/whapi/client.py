"""Whapi REST client — outbound WhatsApp messages."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.config import settings
from app.utils import digits_only

logger = logging.getLogger(__name__)


class WhapiError(RuntimeError):
    """Raised when Whapi rejects a send."""


class WhapiClientError(WhapiError):
    """A 4xx from Whapi — retrying will not help."""


class WhapiClient:
    def __init__(self, token: str | None = None, base_url: str | None = None) -> None:
        self._token = token or settings.whapi_api_token
        self._base_url = (base_url or settings.whapi_base_url).rstrip("/")
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            async with self._lock:
                if self._client is None or self._client.is_closed:
                    self._client = httpx.AsyncClient(
                        base_url=self._base_url,
                        timeout=httpx.Timeout(30.0, connect=10.0),
                        headers={
                            "Authorization": f"Bearer {self._token}",
                            "Content-Type": "application/json",
                            "Accept": "application/json",
                        },
                    )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        client = await self._get_client()
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = await client.post(path, json=payload)
                if 400 <= response.status_code < 500:
                    raise WhapiClientError(f"Whapi {response.status_code}: {response.text}")
                if response.status_code >= 500:
                    raise WhapiError(f"Whapi {response.status_code}: {response.text}")
                return response.json()
            except WhapiClientError:
                raise
            except (httpx.HTTPError, WhapiError) as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
        raise WhapiError(f"Whapi request to {path} failed: {last_error}")

    @staticmethod
    def recipient(phone: str) -> str:
        """Whapi requires a bare number or chat id, never E.164 with '+'.

        Its schema is ^[\\d-]{9,31}(@[\\w\\.]{1,})?$ — sending '+6591234567'
        fails with "wrong request parameters".
        """
        if "@" in phone:  # already a chat id
            return phone
        return digits_only(phone)

    async def send_text(self, to: str, body: str, *, typing_time: int = 0) -> dict[str, Any]:
        """Send a plain text message. Returns the Whapi response body.

        ``typing_time`` makes Whapi show the typing indicator before delivery,
        which keeps the thread feeling human.
        """
        payload: dict[str, Any] = {"to": self.recipient(to), "body": body}
        if typing_time:
            payload["typing_time"] = min(typing_time, 15)
        data = await self._post("/messages/text", payload)
        logger.info("Sent WhatsApp text to %s (id=%s)", to, self.extract_message_id(data))
        return data

    async def send_media(
        self,
        to: str,
        media_url: str,
        *,
        caption: str = "",
        media_type: str = "document",
        filename: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"to": self.recipient(to), "media": media_url}
        if caption:
            payload["caption"] = caption
        if filename:
            payload["filename"] = filename
        return await self._post(f"/messages/{media_type}", payload)

    async def mark_read(self, message_id: str) -> None:
        """Best-effort read receipt so the thread looks attended to."""
        try:
            client = await self._get_client()
            await client.put(f"/messages/{message_id}", json={"status": "read"})
        except httpx.HTTPError:
            logger.debug("Could not mark %s as read", message_id, exc_info=True)

    @staticmethod
    def extract_message_id(response: dict[str, Any]) -> str | None:
        message = response.get("message")
        if isinstance(message, dict) and message.get("id"):
            return str(message["id"])
        if response.get("id"):
            return str(response["id"])
        sent = response.get("sent")
        if isinstance(sent, dict) and sent.get("id"):
            return str(sent["id"])
        return None


whapi = WhapiClient()
