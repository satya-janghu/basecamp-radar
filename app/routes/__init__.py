from app.routes.accounts import router as accounts_router
from app.routes.auth import router as auth_router
from app.routes.discover import router as discover_router
from app.routes.pages import router as pages_router
from app.routes.settings import router as settings_router
from app.routes.tasks import router as tasks_router

__all__ = [
    "accounts_router",
    "auth_router",
    "discover_router",
    "pages_router",
    "settings_router",
    "tasks_router",
]
