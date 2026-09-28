"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.admin import router as admin_router
from app.api.health import router as health_router
from app.api.webhook import debouncer
from app.api.webhook import router as webhook_router
from app.config import settings
from app.graph.graph import close_graph, init_graph
from app.whapi.client import whapi

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info("Starting Ming Hwee chatbot %s (%s)", __version__, settings.environment)

    missing = [
        name
        for name, value in (
            ("SUPABASE_URL", settings.supabase_url),
            ("SUPABASE_SERVICE_ROLE_KEY", settings.supabase_service_role_key),
            ("WHAPI_API_TOKEN", settings.whapi_api_token),
            ("OPENROUTER_API_KEY", settings.openrouter_api_key),
            # Either EMBEDDING_API_KEY or OPENAI_API_KEY, depending on whether
            # embeddings go through OpenRouter or straight to OpenAI.
            ("EMBEDDING_API_KEY/OPENAI_API_KEY", settings.resolved_embedding_key),
            ("TENANT_ID", settings.tenant_id),
        )
        if not value
    ]
    if missing:
        message = f"Missing required environment variables: {', '.join(missing)}"
        if settings.is_production:
            raise RuntimeError(message)
        logger.warning("%s — the bot will not work end to end", message)

    if settings.gate_enabled:
        logger.info(
            "Safety gate ON — the bot will only reply to %s. Every other client "
            "conversation is left to the portal.",
            ", ".join(sorted(settings.allowed_numbers)) or "(no valid numbers — nobody)",
        )
    else:
        logger.warning(
            "Safety gate OFF (BOT_ALLOWED_NUMBERS is empty) — the bot will reply to "
            "EVERY client on this WhatsApp number."
        )

    await init_graph()

    yield

    logger.info("Shutting down")
    await debouncer.shutdown()
    await whapi.close()
    await close_graph()


async def root() -> dict[str, str]:
    return {"service": "minghwee-chatbot", "version": __version__}


def create_app() -> FastAPI:
    """Build the app from the current settings.

    A function rather than module-level code so selfcheck_kb_prep.py can build
    it with the preview secret set and unset and check what each exposes.
    """
    docs = settings.enable_api_docs
    application = FastAPI(
        title="Ming Hwee WhatsApp Chatbot",
        description="WhatsApp assistant for Ming Hwee Employment Agency (MOM Licence 12C6072)",
        version=__version__,
        lifespan=lifespan,
        # Off unless ENABLE_API_DOCS=true: they list every route (§9.33).
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.include_router(health_router)
    application.include_router(webhook_router)
    # With no secret the preview route does not exist at all: 404 whatever the
    # body, and nothing in the schema. Checking the key inside the handler was
    # not enough - FastAPI validated the body first, so a bodiless POST
    # answered 422 and confirmed the route was there (§9.32).
    if settings.admin_preview_secret:
        application.include_router(admin_router)
    application.add_api_route("/", root, methods=["GET"])
    return application


app = create_app()
