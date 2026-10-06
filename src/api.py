"""ASGI entry point for the approval web UI/backend.

Run locally with ``uvicorn api:app --app-dir src``.  Cloudflare Workers can call
the same HTTP endpoints while Cloudflare Workflows owns durable waiting/retry.

Giao diện: đặt các file .html vào thư mục web/ ở gốc dự án, rồi mở http://localhost:8000/
"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from agentic.api import router as agentic_router
from payroll_web import router as payroll_router
from policy_web import router as policy_router
from policy_update.change_management.db import init_db
from policy_update.change_management.review_api import router as changeset_router


app = FastAPI(title="HR Agentic AI API", version="0.1.0")

# Cho phép mở payroll-ui.html trực tiếp từ file://. Khi triển khai thật, thay "*" bằng domain giao diện.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

app.include_router(agentic_router)
app.include_router(changeset_router)
app.include_router(payroll_router)
app.include_router(policy_router)

# Phục vụ giao diện (index.html + 3 trang con). Phải mount SAU khi include các router API.
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")


@app.on_event("startup")
def initialize_database() -> None:
    # Kept for local/demo startup. Production should execute an Alembic migration
    # before deploying instances rather than relying on create_all.
    init_db()