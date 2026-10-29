import hmac

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.config import settings


def verify_password(password: str) -> bool:
    return hmac.compare_digest(password.encode(), settings.PASSWORD.encode())


class AuthMiddleware(BaseHTTPMiddleware):
    ALLOWED_PATHS = {"/login"}
    ALLOWED_PREFIXES = ("/static/",)

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if path in self.ALLOWED_PATHS or any(
            path.startswith(p) for p in self.ALLOWED_PREFIXES
        ):
            return await call_next(request)

        if not request.session.get("authenticated"):
            return RedirectResponse(url="/login", status_code=302)

        return await call_next(request)
