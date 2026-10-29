import logging
from contextlib import asynccontextmanager
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.auth import AuthMiddleware
from app.config import settings
from app.database import SessionLocal
from app.models import Config
from app.routes import accounts_router, auth_router, discover_router, pages_router, settings_router, tasks_router
from app.services.scheduler import scheduler_lifespan

DEFAULT_CONFIG = {
    "poll_interval_minutes": 30,
    "closed_check_interval_hours": 24,
    "archive_after_months": 3,
}


def seed_config():
    """Seed each default config key individually if missing."""
    db = SessionLocal()
    try:
        for key, value in DEFAULT_CONFIG.items():
            existing = db.get(Config, key)
            if not existing:
                db.add(Config(key=key, value=value))
        db.commit()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    seed_config()
    async with scheduler_lifespan() as scheduler:
        app.state.scheduler = scheduler
        yield


app = FastAPI(title="Basecamp Radar", lifespan=lifespan)

# Middleware ordering: added first = inner, added last = outer (runs first)
app.add_middleware(AuthMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.SECRET_KEY,
    max_age=86400 * 180,  # 180 days
    same_site="lax",
    https_only=True,  # production is always served over HTTPS
)

# Static files
app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).parent / "static"),
    name="static",
)

# Routes
app.include_router(auth_router)
app.include_router(accounts_router)
app.include_router(discover_router)
app.include_router(settings_router)
app.include_router(tasks_router)
app.include_router(pages_router)
