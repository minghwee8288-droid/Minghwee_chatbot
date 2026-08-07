"""Speech-to-text for inbound voice notes.

A voice note is useless to the conversation engine as ``[voice]``: the client
said something, and the bot has to answer it. This downloads the audio from
Whapi and sends it to OpenRouter's transcription endpoint, so the rest of the
pipeline sees ordinary text (system prompt rule 14).

Note the request shape. OpenRouter's ``/audio/transcriptions`` is *not* the
multipart OpenAI one — it takes JSON with base64 audio under ``input_audio``,
and ``format`` must be the container extension ('ogg' for a WhatsApp voice
note; 'opus' and 'oga' are both rejected). Changing the host to OpenAI or Groq
means switching back to a multipart ``file`` upload.

Never raises. A failed transcription leaves the message exactly as it arrived —
the bot then treats it as an attachment, raises a media ticket and hands over,
which is the right outcome when nobody knows what was said.
"""

from __future__ import annotations

import base64
import logging

import httpx

from app.config import settings
from app.whapi.parser import IncomingMessage

logger = logging.getLogger(__name__)

# Whapi message types that carry speech worth transcribing. 'audio' is included
# because forwarded recordings arrive as audio rather than a push-to-talk voice
# note, and they are just as likely to be the client talking.
SPEECH_TYPES = ("voice", "audio", "ptt")

# Container extensions the endpoint accepts. WhatsApp sends Opus in an Ogg
# container as audio/ogg; declaring it as 'opus' fails.
_FORMATS = {
    "audio/ogg": "ogg",
    "audio/opus": "ogg",
    "audio/oga": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "mp4",
    "audio/m4a": "m4a",
    "audio/x-m4a": "m4a",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/webm": "webm",
    "audio/flac": "flac",
}
DEFAULT_FORMAT = "ogg"


def is_speech(message: IncomingMessage) -> bool:
    return message.message_type in SPEECH_TYPES and bool(message.media_url)


def audio_format(message: IncomingMessage) -> str:
    """Container extension for the endpoint, from the mime type Whapi reports."""
    mime = (message.media_mime or "").split(";")[0].strip().lower()
    if mime in _FORMATS:
        return _FORMATS[mime]
    # Fall back to the filename, then to Ogg — what WhatsApp actually sends.
    suffix = (message.media_filename or "").rsplit(".", 1)
    if len(suffix) == 2 and suffix[1].lower() in set(_FORMATS.values()):
        return suffix[1].lower()
    if mime:
        logger.info("Unrecognised audio mime %r — trying %s", mime, DEFAULT_FORMAT)
    return DEFAULT_FORMAT


async def _download(url: str) -> bytes | None:
    """Fetch the audio. Whapi media links need the channel token."""
    headers = {"Authorization": f"Bearer {settings.whapi_api_token}"}
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
            if response.status_code in (401, 403):
                # Some Whapi links are pre-signed and reject the bearer token.
                response = await client.get(url)
            response.raise_for_status()
            audio = response.content
    except httpx.HTTPError:
        logger.exception("Could not download voice note from %s", url)
        return None

    if not audio:
        logger.warning("Voice note at %s was empty", url)
        return None
    if len(audio) > settings.transcription_max_bytes:
        logger.warning(
            "Voice note is %d bytes, over the %d limit — not transcribing",
            len(audio),
            settings.transcription_max_bytes,
        )
        return None
    return audio


async def transcribe(message: IncomingMessage) -> str | None:
    """Return what the client said, or None if it could not be transcribed."""
    if not settings.transcription_enabled:
        return None
    if not is_speech(message):
        return None

    api_key = settings.resolved_transcription_key
    if not api_key:
        logger.warning(
            "A voice note arrived but no transcription key is set — it will reach "
            "the bot as an attachment"
        )
        return None

    audio = await _download(message.media_url or "")
    if audio is None:
        return None

    url = f"{settings.transcription_base_url.rstrip('/')}/audio/transcriptions"
    payload = {
        "model": settings.transcription_model,
        "input_audio": {
            "data": base64.b64encode(audio).decode(),
            "format": audio_format(message),
        },
    }
    try:
        async with httpx.AsyncClient(timeout=settings.transcription_timeout_seconds) as client:
            response = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "HTTP-Referer": settings.openrouter_site_url,
                    "X-Title": settings.openrouter_app_name,
                },
                json=payload,
            )
            if response.status_code >= 400:
                logger.error(
                    "Transcription failed (%s) for message %s: %s",
                    response.status_code,
                    message.whapi_message_id,
                    response.text[:300],
                )
                return None
            body = response.json()
    except (httpx.HTTPError, ValueError):
        logger.exception("Transcription request failed for message %s", message.whapi_message_id)
        return None

    text = str(body.get("text") or "").strip()
    if not text:
        # Silence, or a recording with no speech in it. Nothing to answer.
        logger.info("Transcription returned nothing for message %s", message.whapi_message_id)
        return None

    logger.info(
        "Transcribed %s note on message %s (%d chars, %s)",
        message.message_type,
        message.whapi_message_id,
        len(text),
        (body.get("usage") or {}).get("total_tokens", "? tokens"),
    )
    return text
