"""ポータル（まとめページ）の FastAPI アプリ。閲覧専用で、基盤を操作する機能は持たない。"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .collector import Collector, PortalSettings

BASE_DIR = Path(__file__).resolve().parent


def format_bytes(value: int | None) -> str:
    if value is None:
        return "-"
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit in ("B", "KiB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def format_uptime(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    return f"{days}日 {hours}時間" if days else f"{hours}時間 {rest // 60}分"


def create_app(settings: PortalSettings | None = None, start_collector: bool = True) -> FastAPI:
    settings = settings or PortalSettings.from_environ()
    tz = ZoneInfo(settings.timezone_name)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with httpx.AsyncClient() as client:
            app.state.collector = Collector(settings, client)
            task = asyncio.create_task(app.state.collector.run_forever()) if start_collector else None
            try:
                yield
            finally:
                if task:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

    app = FastAPI(title="MyInfraServiceBox", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=BASE_DIR / "templates")
    templates.env.filters["bytes"] = format_bytes
    templates.env.filters["uptime"] = format_uptime
    templates.env.filters["localtime"] = lambda dt: dt.astimezone(tz).strftime("%Y-%m-%d %H:%M") if dt else "-"
    templates.env.filters["short"] = lambda commit: (commit or "-")[:12]

    def context(request: Request) -> dict[str, Any]:
        snapshot = request.app.state.collector.snapshot
        return {"snapshot": snapshot, "refresh_seconds": settings.refresh_seconds}

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "index.html", context(request))

    @app.get("/partials/status", response_class=HTMLResponse)
    async def status_partial(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "_status.html", context(request))

    @app.get("/api/status")
    async def status_json(request: Request) -> dict[str, Any]:
        snapshot = asdict(request.app.state.collector.snapshot)
        return _jsonable(snapshot)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value
