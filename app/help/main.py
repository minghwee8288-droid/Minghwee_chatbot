"""Help's standalone FastAPI app: `uvicorn app.help.main:app --port 8001`.

Not app.main, and imports nothing of Claire's runtime: no lifespan, no webhook,
no debouncer, no checkpointer, no call-context poller, no recovery scan, no
typing indicator, no Whapi call. The modules it imports through app.graph.guards
load the graph's code but start nothing (scripts/selfcheck_help.py asserts it).
No docs, no OpenAPI schema, no CORS: it is called server to server only.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import settings
from app.help.router import error, not_found
from app.help.router import router as help_router

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)


async def health() -> dict[str, bool]:
    """Liveness only: no database call, answers even with HELP_SECRET empty."""
    return {"ok": True}


def create_app() -> FastAPI:
    application = FastAPI(
        title="Ming Hwee Help",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.include_router(help_router)
    application.add_api_route("/internal/help/health", health, methods=["GET"])

    @application.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException):
        # 404 and 405 look identical, so neither reveals which paths exist.
        if exc.status_code in (404, 405):
            return not_found()
        return error(exc.status_code, "http_error", str(exc.detail))

    @application.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, _exc: RequestValidationError):
        return error(422, "invalid_request", "invalid request")

    return application


app = create_app()
