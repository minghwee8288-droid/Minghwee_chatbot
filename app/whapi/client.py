"""Whapi REST client — outbound WhatsApp messages."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from app import readonly
from app.config import settings
from app.utils import digits_only, normalize_phone

logger = logging.getLogger(__name__)


# How many LID -> phone pairs to remember.
#
# Sized from the live channel rather than guessed, and the first guess (500)
# was wrong. Measured 2026-09-10: the channel holds **3,422 chats**, and of
# 2,000 scanned, **1,892 (94%) are already LIDs** - every one of them carrying
# a phone number Whapi can return. So this is not a handful of migrated
# testers, it is nearly the whole estate.
#
# That matters here because resolution happens BEFORE the allowlist (it has
# to - the allowlist is keyed on the phone number), so every inbound message
# on the shared number needs one lookup the first time its chat is seen,
# including the thousands of conversations the portal owns and the bot then
# immediately stands down on. At 500 the long tail would have churned and
# re-fetched forever. An entry is two short strings, so covering the estate
# costs nothing worth measuring.
_LID_CACHE_MAX = 5000
# The fallback sweep: how many chats to read, in what page size, and how often
# it may run at all. 3,622 chats on this channel (2026-09-14), so the cap is
# headroom rather than a limit. The interval is what stops a client on an
# unresolvable LID sweeping once per message.
_LID_SWEEP_PAGE = 500
_LID_SWEEP_MAX_CHATS = 6000
_LID_SWEEP_INTERVAL = 300.0


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
        # LID -> phone. See resolve_lid().
        self._lid_phones: dict[str, str] = {}
        # Monotonic, so a clock change cannot make the sweep
        # interval negative and let it run on every message.
        self._last_sweep: float = 0.0

    async def _get_client(self) -> httpx.AsyncClient:
        # Every Whapi request - send, read receipt, lookup - comes through here.
        # A read-only request (POST /admin/preview) must never reach WhatsApp.
        if readonly.active():
            readonly.record("WHAPI", self._base_url)
            raise readonly.ReadOnlyViolation(
                "read-only request tried to contact WhatsApp (Whapi)"
            )
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

    # The only failures a SEND may be retried on: the ones where the request
    # provably never reached Whapi, so nothing can have been delivered.
    #
    # Every _post in this client is a message going to a client's phone
    # (/messages/text and /messages/<media>), and none of them is idempotent -
    # Whapi has no idempotency key, so a second POST is a second WhatsApp
    # message. A connection that was never established, or a request that
    # never left the pool, cannot have sent one. Everything else can:
    # ReadTimeout, ReadError and RemoteProtocolError all mean the request was
    # written and the RESPONSE was lost, and a 5xx means their server had it.
    _SAFE_TO_RESEND = (
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.PoolTimeout,
    )

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST a message, retrying ONLY where nothing can have been sent.

        This used to retry three times on any httpx error or any 5xx, and that
        is how the same message went out twice - which would be a cosmetic
        defect if the second copy were the end of it, and it is not.

        Live, 2026-09-18, conversation 3766: "Usually about 4 to 6 weeks for a
        direct hire from overseas." was sent at 14:20:36 and again at
        14:20:38, 1.9 seconds apart, which is this loop's own 1.5s backoff. A
        retry gets a NEW Whapi message id, and only the id of the attempt that
        finally returned is handed back to `send_bot_reply` - so only that one
        is passed to `mark_sent_by_bot` and only that one is written to
        `wp_chat_messages` as ours. When Whapi then echoed the FIRST copy back
        as a from_me webhook, `was_sent_by_bot` had never heard of its id,
        `handle_outbound` read it as a human agent picking the thread up, and
        the bot stood down. The row is still on the database as `is_bot=False,
        sent_by='agent'`, and the client's next message - "ok and what is the
        fees for this" - got no reply at all, on a conversation that is still
        `human_active` days later.

        So a retry here does not cost a duplicate message. It costs the whole
        conversation, silently, and the transcript blames a human agent who
        was never there. The accepted trade is the other way round: a send
        that fails after the request was written is reported and not repeated,
        because a message that may have gone out already must not go out
        twice. `handle_outbound` also no longer mistakes our own words for an
        agent, which is the second lock on the same door.
        """
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
            except self._SAFE_TO_RESEND as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
            except (httpx.HTTPError, WhapiError) as exc:
                logger.warning(
                    "Whapi POST %s failed after the request was sent (%s: %s) - NOT "
                    "retrying, because the message may already have been delivered",
                    path,
                    type(exc).__name__,
                    exc,
                )
                raise WhapiError(f"Whapi request to {path} failed: {exc}") from exc
        raise WhapiError(f"Whapi request to {path} failed: {last_error}")

    async def _get(self, path: str) -> dict[str, Any] | None:
        """Read-only GET. Returns None rather than raising — every caller is
        best-effort, and a lookup failure must never break handling a message."""
        try:
            client = await self._get_client()
            response = await client.get(path)
            if response.status_code >= 400:
                logger.warning("Whapi GET %s -> %s: %s", path, response.status_code, response.text[:200])
                return None
            data = response.json()
            return data if isinstance(data, dict) else None
        except (httpx.HTTPError, ValueError):
            logger.warning("Whapi GET %s failed", path, exc_info=True)
            return None

    def _remember_lid(self, lid: str, phone: str) -> str:
        """Cache one LID -> phone mapping, oldest out first."""
        resolved = normalize_phone(phone)
        # Bounded, unlike the caches section 9.13 lists: a runaway LID stream
        # must not grow this without limit. Oldest out first - a re-lookup is
        # one cheap GET, and the active conversations are the recent ones.
        if len(self._lid_phones) >= _LID_CACHE_MAX:
            self._lid_phones.pop(next(iter(self._lid_phones)))
        self._lid_phones[lid] = resolved
        return resolved

    async def _sweep_chats(self) -> None:
        """Fill the LID cache from the chat LIST, for the LIDs /chats misses.

        Rate-limited, because it is pages rather than one GET and a client
        messaging from an unresolvable LID would otherwise sweep on every
        message. One sweep serves every LID on the channel, so the second
        unresolvable client costs nothing.

        A chat can appear in the list TWICE - once as type "unknown" with no
        phone, once as type "contact" with one. That duplicate is exactly what
        made /chats/<lid> unreliable, so reading the list carelessly reproduces
        the bug this is here to fix: entries without a phone are skipped, and
        the first entry that HAS one wins.
        """
        now = time.monotonic()
        if now - self._last_sweep < _LID_SWEEP_INTERVAL:
            return
        self._last_sweep = now

        seen = offset = 0
        learned = 0
        while offset < _LID_SWEEP_MAX_CHATS:
            data = await self._get(f"/chats?count={_LID_SWEEP_PAGE}&offset={offset}")
            chats = (data or {}).get("chats") or []
            if not chats:
                break
            for chat in chats:
                seen += 1
                chat_id = str(chat.get("id") or "").strip()
                phone = str(chat.get("phone") or "").strip()
                if not chat_id.endswith("@lid") or not phone:
                    continue
                if chat_id in self._lid_phones:
                    # First entry wins. The phone-LESS duplicate is already
                    # gone (the `not phone` test above), so what this catches
                    # is the rarer shape: the same LID listed twice with two
                    # DIFFERENT numbers. Overwriting there would make the
                    # answer depend on page order, which is not something to
                    # decide a client's identity on. Proved by injection: with
                    # both this and `not phone` removed the duplicate check
                    # goes red, with either one present it does not.
                    continue
                self._remember_lid(chat_id, phone)
                learned += 1
            offset += len(chats)
        logger.info(
            "Swept %s Whapi chats for LIDs the /chats endpoint could not resolve - "
            "learned %s mapping(s)", seen, learned
        )

    async def resolve_lid(self, lid: str) -> str | None:
        """The phone number behind a WhatsApp LID, or None.

        WhatsApp has begun identifying senders by an opaque LID
        ("116909177569373@lid") instead of a phone JID once a business number
        moves onto Meta's hosted service. Every lookup in this codebase is
        keyed on the phone number - the allowlist, get_by_phone, identify, the
        lead - so a LID that reaches them silences the bot on a client it is
        supposed to answer (live, 2026-09-10, on an allowlisted tester).

        Measured against the live channel the same day, the chats endpoint
        carries it and the contacts endpoint does NOT:

            GET /chats/116909177569373@lid
              {"id":"116909177569373@lid","phone":"917970027379", ...}
            GET /contacts/116909177569373@lid
              {"pushname":"Vaidik Dubey","saved":false,"id":"...@lid"}

        so this reads /chats first. The result is cached because it cannot
        change: a LID identifies one WhatsApp account.

        **And /chats/<lid> is not enough, which cost a tester a whole morning**
        (2026-09-14). For the same channel, the same minute:

            GET /chats/95786411008174@lid           {"type":"unknown"}   no phone
            GET /chats/917354708111@s.whatsapp.net  {"id":"95786411008174@lid",
                                                     "phone":"917354708111",
                                                     "type":"contact"}

        - the same chat, resolvable by phone JID and not by its own LID. We
        cannot use the second form, because the phone is the thing we are
        looking for. But the chat LIST carries the resolvable record, so a miss
        falls back to sweeping it. Measured: that recovers 95786411008174
        (a tester, live and unanswerable until then) and 77262519025804, whose
        /chats/<lid> 404s outright while the list gives 6589466562.
        """
        lid = (lid or "").strip()
        if not lid:
            return None
        if lid in self._lid_phones:
            return self._lid_phones[lid]

        data = await self._get(f"/chats/{lid}")
        phone = str((data or {}).get("phone") or "").strip()
        if not phone:
            # The list, not this one chat. One sweep fills the cache for EVERY
            # unresolved LID on the channel, which is why it is worth the pages
            # - and why it is rate-limited rather than run per message.
            await self._sweep_chats()
            if lid in self._lid_phones:
                return self._lid_phones[lid]
            logger.warning("Whapi could not resolve LID %s to a phone number", lid)
            return None

        resolved = self._remember_lid(lid, phone)
        logger.info("Resolved LID %s to %s", lid, resolved)
        return resolved

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
