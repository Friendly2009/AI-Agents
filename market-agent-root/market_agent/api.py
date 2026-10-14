import asyncio
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings
from .diagnostics import diagnose
from .models import Profile
from .network import HTTP
from .recommendations import POLICIES
from .scheduler import scheduler
from .service import Service
from .storage import Store


def create_app(settings=None):
    settings = settings or Settings()
    markets = settings.markets()
    store = Store(settings.database_path)

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(timeout=settings.http_timeout_seconds, follow_redirects=False) as client:
            service = Service(settings, markets, store, HTTP(client, settings.http_retries))
            app.state.service = service
            worker = asyncio.create_task(scheduler(service)) if settings.schedule_enabled else None
            collector = (
                asyncio.create_task(service.collector.loop())
                if settings.mode == "live" and settings.news_collection_enabled
                else None
            )
            app.state.last_diagnostic = None
            app.state.check_lock = asyncio.Lock()
            yield
            if collector:
                collector.cancel()
                await asyncio.gather(collector, return_exceptions=True)
            if worker:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            await service.close()

    app = FastAPI(
        title="Weekly Market Agent",
        version="0.3.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        return JSONResponse(status_code=422, content={"detail": "Проверьте профиль: бюджет, срок 7–365 дней, "
                            "уровень риска и целое количество акций без повторения тикеров."})

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )
    bearer = HTTPBearer(auto_error=False)

    def authorized(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if not credentials or not secrets.compare_digest(
            credentials.credentials.encode(), settings.api_token.get_secret_value().encode()
        ):
            raise HTTPException(401, "Требуется API token", headers={"WWW-Authenticate": "Bearer"})

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'self'"
        )
        return response

    @app.get("/")
    def index():
        return FileResponse(Path(__file__).parent / "static" / "index.html")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/config", dependencies=[Depends(authorized)])
    def config():
        return {
            "mode": settings.mode,
            "version": "0.3.0",
            "llm_enabled": settings.llm_enabled,
            "llm_model": settings.llm_model,
            "news_poll_minutes": settings.news_poll_minutes,
            "news_collection_enabled": settings.news_collection_enabled,
            "instruments": [i.model_dump() for i in markets.instruments],
            "news_feed_count": len(markets.news_feeds),
            "schedule": {
                "enabled": settings.schedule_enabled,
                "timezone": settings.timezone,
                "weekday": settings.schedule_weekday,
                "hour": settings.schedule_hour,
                "minute": settings.schedule_minute,
                "catchup": settings.schedule_catchup,
            },
        }

    @app.get("/api/profile", dependencies=[Depends(authorized)])
    def profile():
        return {"saved": store.profile(), "risk_policies": POLICIES}

    @app.post("/api/profile", dependencies=[Depends(authorized)])
    def save_profile(profile: Profile):
        known = {i.symbol for i in markets.instruments}
        if any(h.symbol not in known for h in profile.holdings):
            raise HTTPException(422, "Портфель содержит тикеры вне настроенного списка MOEX")
        return store.save_profile(profile)

    @app.post("/api/runs", status_code=202, dependencies=[Depends(authorized)])
    async def run():
        run_id = app.state.service.submit()
        if not run_id:
            raise HTTPException(409, "Анализ уже выполняется")
        return {"id": run_id, "status": "running"}

    @app.get("/api/runs", dependencies=[Depends(authorized)])
    def runs(limit: int = Query(30, ge=1, le=100)):
        return store.list(limit)

    @app.get("/api/runs/{run_id}", dependencies=[Depends(authorized)])
    def detail(run_id: str):
        result = store.get(run_id)
        if result is None:
            raise HTTPException(404, "Отчёт не найден")
        return result

    @app.get("/api/status", dependencies=[Depends(authorized)])
    def status():
        return {
            "mode": settings.mode,
            "llm_configured": settings.llm_enabled,
            "news": store.news_status(markets.news_feeds),
            "diagnostic": app.state.last_diagnostic,
        }

    @app.post("/api/check", dependencies=[Depends(authorized)])
    async def check():
        if app.state.check_lock.locked():
            raise HTTPException(409, "Проверка подключения уже выполняется")
        async with app.state.check_lock:
            try:
                result = await asyncio.wait_for(diagnose(app.state.service), timeout=240)
            except TimeoutError:
                raise HTTPException(504, "Проверка не завершилась за четыре минуты") from None
            app.state.last_diagnostic = result
            return result

    @app.get("/api/openapi.json", dependencies=[Depends(authorized)])
    def schema():
        return app.openapi()

    return app
