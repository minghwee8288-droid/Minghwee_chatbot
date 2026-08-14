"""Application configuration, loaded from environment variables / .env."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Supabase ---
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    supabase_db_url: str = ""

    # --- Whapi ---
    whapi_api_token: str = ""
    whapi_base_url: str = "https://gate.whapi.cloud"
    whapi_webhook_secret: str = ""
    whapi_sender_phone: str = ""

    # --- OpenRouter (LLM) ---
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "moonshotai/kimi-k3"
    # Kimi K3 is a reasoning model: left alone it spends completion tokens
    # thinking before it writes anything, and those come out of max_tokens.
    # Measured on a full system prompt: ~430 reasoning tokens, 8-16s per call
    # and 18x the cost of DeepSeek, for the same answer. A WhatsApp turn makes
    # two or three calls, so that is a minute of silence for the client.
    #
    #   off     - no thinking. ~3s, the same replies. The default.
    #   low     - a little thinking, similar speed.
    #   default - the model decides. Slow and expensive; only worth it if a
    #             flow is getting the reasoning visibly wrong without it.
    llm_reasoning: str = "off"

    # Reasoning tokens are charged against max_tokens, so when thinking is on
    # every call needs room for it or the reply is cut off mid-sentence.
    llm_reasoning_headroom: int = 400

    @property
    def reasoning_body(self) -> dict:
        """OpenRouter's `reasoning` request field, or {} to leave it alone."""
        mode = (self.llm_reasoning or "").strip().lower()
        if mode in {"off", "false", "none", "disabled"}:
            return {"reasoning": {"enabled": False}}
        if mode in {"low", "medium", "high"}:
            return {"reasoning": {"effort": mode}}
        return {}

    @property
    def resolved_reasoning_headroom(self) -> int:
        return 0 if self.reasoning_body.get("reasoning", {}).get("enabled") is False else (
            self.llm_reasoning_headroom
        )
    openrouter_site_url: str = "https://minghwee.com"
    openrouter_app_name: str = "Ming Hwee Chatbot"

    # --- Embeddings ---
    # Must match the RAG pipeline exactly: same model, same dimensions, same
    # provider endpoint. A mismatch produces silently useless similarity
    # scores — and cb_knowledge_base_updated.embedding is a bare `vector` with
    # no declared dimension, so the database will not catch it either.
    openai_api_key: str = ""
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    # Blank = call OpenAI directly. Set to https://openrouter.ai/api/v1 (and
    # EMBEDDING_API_KEY to the OpenRouter key) to embed through OpenRouter, as
    # the pipeline does.
    embedding_base_url: str = ""
    embedding_api_key: str = ""

    @property
    def resolved_embedding_key(self) -> str:
        return self.embedding_api_key or (
            self.openrouter_api_key if self.embedding_base_url else self.openai_api_key
        )

    # --- Voice transcription ---
    # Voice notes are transcribed before they reach the graph, so the model
    # reads them as ordinary text (system prompt rule 14). This uses
    # OpenRouter's audio endpoint, which is NOT the multipart OpenAI one: it
    # takes JSON with base64 audio in `input_audio`. Pointing the base URL at
    # api.openai.com would need the multipart form instead.
    transcription_enabled: bool = True
    transcription_base_url: str = "https://openrouter.ai/api/v1"
    transcription_api_key: str = ""
    transcription_model: str = "openai/gpt-4o-mini-transcribe"
    # Base64 inflates the payload by a third, and a WhatsApp voice note is
    # under a megabyte in practice — this is a guard against downloading
    # something unexpected, not a real constraint.
    transcription_max_bytes: int = 10 * 1024 * 1024
    transcription_timeout_seconds: float = 90.0

    @property
    def resolved_transcription_key(self) -> str:
        return self.transcription_api_key or self.openrouter_api_key

    # --- Leads ---
    # leads.branch_id / leads_candidate.branch_id are NOT NULL with no default,
    # and a WhatsApp enquiry has no branch of its own. Blank = resolve the
    # tenant's HQ branch at runtime and cache it.
    lead_branch_id: str = ""
    lead_enabled: bool = True

    # Who assault escalations and partner enquiries go to. Blank = the oldest
    # active profile with archetype_key 'admin'. Set it when more than one
    # admin exists and it matters which of them is paged.
    escalation_profile_id: str = ""

    # --- Application ---
    tenant_id: str = ""
    environment: str = "development"
    log_level: str = "INFO"
    # How long the client has to stop typing before their messages are treated
    # as one turn. The timer restarts on every message, so a burst of five is
    # still one reply. Raised from 3s: clients routinely pause two or three
    # seconds mid-thought, and each pause was costing them a separate answer.
    debounce_seconds: float = 8.0

    # --- RAG ---
    # Calibrated against cb_knowledge_base_updated (270 chunks) with
    # scripts/check_retrieval.py:
    #
    #   answerable client questions : 0.464 / 0.673 / 0.765  (min/median/max)
    #   off-topic messages          : 0.094 / 0.267 / 0.462
    #
    # The bands separate cleanly apart from one outlier — "do you sell property
    # in singapore" scored 0.462 against "Can expats hire a maid in Singapore?"
    # on the shared words alone. Excluding it, off-topic tops out at 0.313.
    rag_match_threshold: float = 0.35
    rag_match_count: int = 5
    # NOT READ BY ANYTHING. It was the upper gate of a two-gate design; the
    # design collapsed to the single soft floor below and this was left behind.
    # Changing it has no effect — rag_soft_floor is the knob. Kept only so an
    # existing RAG_CONFIDENCE_FLOOR in a deployed .env does not fail to parse.
    rag_confidence_floor: float = 0.45
    # Below this, nothing retrieved is worth answering from: the reply is
    # replaced and the conversation goes to a human. Above it the records ARE
    # used — a partial match on a real question is still our own material, and
    # replacing it with "let me check with the team" reads as a bot that knows
    # nothing.
    #
    # 0.40 sits between the two measured bands: above the off-topic mass
    # (0.313 excluding the outlier) and below the weakest real question
    # (0.464). At the old 0.30 the bot would answer "can you help me fix my
    # aircon" (0.313) out of whatever the records happened to return.
    rag_soft_floor: float = 0.40
    # 20% overlap makes chunks longer; cap what goes into the prompt.
    rag_max_chunk_chars: int = 1200
    # A table_unit row holds a whole table and is never truncated — half a fee
    # table is worse than none, because the model answers confidently from the
    # half it got. Anything longer than this is dropped from the context
    # instead of being cut, so the bot says it will check.
    rag_max_table_chars: int = 3000
    # Blank = search every namespace.
    rag_namespace: str = ""

    # --- Conversation ---
    history_limit: int = 20

    # Safety gate. When set, the bot only engages these numbers and stays
    # completely silent on every other conversation — the portal keeps working
    # as before. Comma-separated, any format. EMPTY MEANS EVERY CLIENT, so keep
    # your own number here until you are ready to go live.
    bot_allowed_numbers: str = ""

    # Do not take over a thread a human agent has touched in the last N hours.
    # Protects live conversations an agent is in the middle of handling.
    agent_grace_hours: int = 24

    # How long a conversation stays with a human before the bot may pick it up
    # again, when the standdown's cause cannot be identified at all (no
    # handover row, or a reason outside the ones this file knows how to
    # recover). A genuine bot crash uses bot_failure_timeout_minutes instead —
    # this is the conservative "we truly don't know what's going on" fallback.
    # The clock starts after the last bot reply. 0 disables the return
    # entirely.
    human_active_timeout_hours: int = 72

    # How long to wait after the bot crashes mid-turn (REASON_CONFUSED —
    # "Bot could not answer" in the portal) before trying again on its own.
    # This used to fall through to human_active_timeout_hours (72h), which is
    # right for a standdown of unknown cause but absurd for a plain bot
    # failure — a client stuck for most of a day because the bot hit an
    # exception once. 0 disables the auto-resume (permanent until an agent
    # resolves the ticket or otherwise flips bot_status back).
    bot_failure_timeout_minutes: int = 30

    # A human agent typing on the thread pauses the bot conversation-wide —
    # the one thing that still does, now that every other trigger is
    # topic-scoped (see cb_tickets / open_topics_for_conversation). The clock
    # restarts on every further agent message and the bot resumes on its own
    # once this many minutes pass with no new one — it does not wait for the
    # agent to mark anything resolved. 0 disables the auto-resume (permanent
    # until explicitly resolved, the old behaviour).
    agent_pause_minutes: int = 10

    # The WhatsApp Business app can send greeting and away messages by itself.
    # They arrive as outbound messages the bot did not send, which the agent
    # detector would otherwise read as "a human has taken over" — silencing the
    # bot on every new conversation. Separate multiple texts with '||'.
    whatsapp_auto_reply_texts: str = (
        "Thank you for contacting Ming Hwee Agency! "
        "Please let us know how we can help you."
    )

    @property
    def auto_reply_texts(self) -> set[str]:
        from app.utils import normalize_text

        return {
            normalize_text(part)
            for part in self.whatsapp_auto_reply_texts.split("||")
            if part.strip()
        }

    @property
    def gate_enabled(self) -> bool:
        """Whether the allowlist is in force.

        Read from the RAW setting, never from the parsed set: if every entry
        were malformed the set would be empty, and treating that as "no gate"
        would silently unleash the bot on every client. Fail closed.
        """
        return bool(self.bot_allowed_numbers.strip())

    @property
    def allowed_numbers(self) -> set[str]:
        from app.utils import digits_only, normalize_phone

        numbers: set[str] = set()
        for part in self.bot_allowed_numbers.replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            if len(digits_only(part)) < 8:
                # e.g. a leftover '+65XXXXXXXX' placeholder.
                continue
            numbers.add(normalize_phone(part))
        return numbers
    # The WhatsApp portal is subscribed to the same Whapi channel and already
    # maintains last_message_body/last_message_at/last_direction/unread_count.
    # Leave true so the bot does not double-count unread badges; set false when
    # running the bot on a channel the portal is not connected to.
    portal_manages_conversation_meta: bool = True

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
