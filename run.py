"""Application entrypoint.

Use this instead of calling uvicorn directly:

    python run.py

On Windows, asyncio defaults to the ProactorEventLoop, which psycopg cannot use
in async mode — the LangGraph Postgres checkpointer silently falls back to
memory. The policy has to be set before the event loop is created, which is
earlier than uvicorn imports the app, so it cannot live in app/main.py.
"""

from __future__ import annotations

import asyncio
import os
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import uvicorn  # noqa: E402 - must come after the policy is set

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=os.getenv("RELOAD", "").lower() in {"1", "true", "yes"},
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
