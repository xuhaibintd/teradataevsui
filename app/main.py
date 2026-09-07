from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.core.access_logging import configure_uvicorn_access_logging
from app.core.errors import configure_error_handlers
from app.core.runtime_manager import RuntimeIsolationMiddleware
from app.core.security import SecurityMiddleware
from app.core.settings import Settings
from app.core.single_instance import SingleInstanceLock
from app.runtime import STATIC_DIR, TEMPLATES_DIR
from app.routers.api import router as api_router
from app.routers.web import router as web_router
from app.services.job_worker import ApplicationJobRunner, PersistentJobWorker
from app.services.maintenance_jobs import build_maintenance_job_handlers
from app.services.workflow_jobs import build_workflow_job_handlers
from app.web_support import initialize_app_state


def _build_job_runner(application: FastAPI, settings: Settings) -> ApplicationJobRunner:
    handlers = build_workflow_job_handlers(
        application.state.auth_store,
        artifact_lifecycle=application.state.artifact_lifecycle,
        artifact_retention_days=settings.artifact_retention_days,
        vectorstore_ready_timeout_seconds=settings.vectorstore_ready_timeout_seconds,
        vectorstore_ready_poll_seconds=settings.vectorstore_ready_poll_seconds,
    )
    handlers.update(
        build_maintenance_job_handlers(
            application.state.artifact_lifecycle,
            cleanup_enabled=settings.artifact_cleanup_enabled,
        )
    )
    return ApplicationJobRunner(
        PersistentJobWorker(application.state.job_repository, handlers),
        application.state.teradata_runtime_manager,
        stale_seconds=settings.job_stale_seconds,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or Settings.from_env()
    resolved_settings.validate_runtime()
    configure_uvicorn_access_logging()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        if resolved_settings.environment == "test":
            yield
            return
        lock_path = resolved_settings.database_path.with_suffix(".app.lock")
        with SingleInstanceLock(lock_path):
            runner = _build_job_runner(application, resolved_settings)
            application.state.background_job_runner = runner
            runner.start()
            try:
                yield
            finally:
                await runner.stop()

    application = FastAPI(title="teradataevsui", version="0.7.0", lifespan=lifespan)
    application.state.settings = resolved_settings
    configure_error_handlers(application)
    application.add_middleware(SecurityMiddleware)
    application.add_middleware(RuntimeIsolationMiddleware)
    application.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    initialize_app_state(
        application,
        Jinja2Templates(directory=str(TEMPLATES_DIR)),
        settings=resolved_settings,
    )
    application.include_router(web_router)
    application.include_router(api_router)
    return application


app = create_app()
