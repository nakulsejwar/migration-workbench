"""FastAPI application factory.  Run with:  uvicorn app.main:app   (see README)."""
from __future__ import annotations

import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text
from starlette.exceptions import HTTPException as StarletteHTTPException

from .agent.openrouter import OpenRouterClient
from .api.routes import router
from .audit import configure_logging, log_event, request_id_var
from .config import Settings
from .database import init_db, make_engine, make_session_factory
from .errors import AppError
from .models import SourceSnapshot
from .services import workflow


def _envelope(request: Request, status: int, code: str, message: str, details=None) -> JSONResponse:
    rid = getattr(request.state, "request_id", None)
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message, "details": details, "request_id": rid}}, headers={"X-Request-ID": rid or ""})


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level)
    engine = make_engine(settings.database_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as db:
        workflow.init_data(db)

    app = FastAPI(
        title="Agentic Data Migration Planner and Reconciliation Workbench", version="1.0.0",
        description="Plan, dry-run, approve, execute, reconcile and roll back one bounded migration (legacy_customers → accounts). "
                    "Mutating endpoints need the demo identity header X-Demo-User (not authentication).")
    app.state.settings, app.state.engine, app.state.session_factory = settings, engine, session_factory
    app.state.llm_factory = lambda s: OpenRouterClient(s)  # tests replace this with a fake

    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_methods=["*"], allow_headers=["*"], expose_headers=["X-Request-ID"])

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        request.state.request_id = rid
        token = request_id_var.set(rid)
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = rid
        log_event(None, "info", "http.request", method=request.method, path=request.url.path, status=response.status_code, duration_ms=round((time.perf_counter() - t0) * 1000, 1))
        return response

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError):
        log_event(None, "warning" if exc.status < 500 else "error", "http.error", status=exc.status, code=exc.code, path=request.url.path)
        return _envelope(request, exc.status, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        details = [{"loc": list(e.get("loc", [])), "message": e.get("msg"), "type": e.get("type")} for e in exc.errors()]  # never echo the submitted values
        return _envelope(request, 422, "validation_error", "Request validation failed", details)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        return _envelope(request, exc.status_code, f"http_{exc.status_code}", str(exc.detail))

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        import logging
        logging.getLogger("workbench").error("unhandled_exception", exc_info=exc, extra={"fields": {"event": "unhandled_exception", "path": request.url.path, "request_id": getattr(request.state, "request_id", None)}})
        return _envelope(request, 500, "internal_error", "Unexpected server error. Quote the request id when reporting it.")

    @app.get("/health", tags=["meta"])
    def health():
        """Liveness: the process is up."""
        return {"status": "ok"}

    @app.get("/ready", tags=["meta"])
    def ready(request: Request):
        """Readiness: the database answers and is initialised. 503 otherwise."""
        try:
            with session_factory() as db:
                db.execute(text("SELECT 1"))
                initialised = db.scalar(select(SourceSnapshot.id).limit(1)) is not None
        except Exception:
            return JSONResponse(status_code=503, content={"status": "unavailable", "database": False})
        return JSONResponse(status_code=200 if initialised else 503, content={"status": "ready" if initialised else "not_initialised", "database": True, "llm_configured": settings.llm_configured})

    app.include_router(router)

    fe = Path(settings.frontend_dir) if settings.frontend_dir else Path(__file__).resolve().parents[2] / "frontend"
    if (fe / "index.html").exists():
        app.mount("/", StaticFiles(directory=str(fe), html=True), name="frontend")
    return app


app = create_app()


