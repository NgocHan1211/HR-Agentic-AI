"""ASGI entry point for the approval web UI/backend.

Run locally with ``uvicorn api:app --app-dir src``.  Cloudflare Workers can call
the same HTTP endpoints while Cloudflare Workflows owns durable waiting/retry.
"""

from fastapi import FastAPI

from agentic.api import router as agentic_router
from policy_update.change_management.db import init_db
from policy_update.change_management.review_api import router as changeset_router


app = FastAPI(title="HR Agentic AI API", version="0.1.0")
app.include_router(agentic_router)
app.include_router(changeset_router)


@app.on_event("startup")
def initialize_database() -> None:
    # Kept for local/demo startup. Production should execute an Alembic migration
    # before deploying instances rather than relying on create_all.
    init_db()
