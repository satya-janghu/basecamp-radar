import hmac
import logging
import secrets
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import verify_password
from app.config import settings
from app.database import get_db
from app.models import Config
from app.templating import templates

logger = logging.getLogger(__name__)

BASECAMP_AUTH_URL = "https://launchpad.37signals.com/authorization/new"
BASECAMP_TOKEN_URL = "https://launchpad.37signals.com/authorization/token"
BASECAMP_AUTHORIZATION_JSON = "https://launchpad.37signals.com/authorization.json"

router = APIRouter()


# --- App login/logout ---


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get("authenticated"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(request=request, name="login.html")


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request):
    form = await request.form()
    password = form.get("password", "")

    if verify_password(password):
        request.session["authenticated"] = True
        return RedirectResponse(url="/", status_code=302)

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": "Invalid password"},
    )


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)


# --- Basecamp OAuth 2.0 ---


def _get_callback_url(request: Request) -> str:
    """Build the OAuth callback URL from the current request."""
    return str(request.url_for("basecamp_callback"))


def _set_config(db: Session, key: str, value) -> None:
    """Upsert a config row."""
    row = db.get(Config, key)
    if row:
        row.value = value
    else:
        db.add(Config(key=key, value=value))


def _delete_config(db: Session, key: str) -> None:
    """Delete a config row if it exists."""
    row = db.get(Config, key)
    if row:
        db.delete(row)


def get_basecamp_token(db: Session) -> str | None:
    """Return a valid access token, refreshing if expired. Returns None if not connected."""
    token_row = db.get(Config, "basecamp_access_token")
    if not token_row or not token_row.value:
        return None

    expires_row = db.get(Config, "basecamp_token_expires_at")
    if expires_row and expires_row.value:
        expires_at = datetime.fromisoformat(expires_row.value)
        if datetime.now(timezone.utc) >= expires_at:
            # Token expired — try to refresh
            refresh_row = db.get(Config, "basecamp_refresh_token")
            if not refresh_row or not refresh_row.value:
                return None

            try:
                resp = httpx.post(
                    BASECAMP_TOKEN_URL,
                    json={
                        "type": "refresh",
                        "client_id": settings.BASECAMP_CLIENT_ID,
                        "client_secret": settings.BASECAMP_CLIENT_SECRET,
                        "refresh_token": refresh_row.value,
                    },
                    timeout=10.0,
                )
            except httpx.HTTPError:
                logger.exception("Basecamp token refresh failed")
                return None
            if resp.status_code != 200:
                logger.warning("Basecamp token refresh returned %d", resp.status_code)
                return None

            data = resp.json()
            _set_config(db, "basecamp_access_token", data["access_token"])
            _set_config(db, "basecamp_refresh_token", data.get("refresh_token", refresh_row.value))
            _set_config(db, "basecamp_token_expires_at", data["expires_at"])
            db.commit()
            return data["access_token"]

    return token_row.value


@router.get("/auth/basecamp")
def basecamp_start(request: Request):
    """Redirect user to Basecamp authorization page."""
    if not settings.BASECAMP_CLIENT_ID or not settings.BASECAMP_CLIENT_SECRET:
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={
                "title": "OAuth Not Configured",
                "message": "BASECAMP_CLIENT_ID and BASECAMP_CLIENT_SECRET environment variables must be set before connecting to Basecamp.",
            },
        )
    callback_url = _get_callback_url(request)
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    query = urlencode({
        "type": "web_server",
        "client_id": settings.BASECAMP_CLIENT_ID,
        "redirect_uri": callback_url,
        "state": state,
    })
    return RedirectResponse(url=f"{BASECAMP_AUTH_URL}?{query}", status_code=302)


@router.get("/auth/basecamp/callback", name="basecamp_callback")
def basecamp_callback(
    request: Request, code: str = "", state: str = "", db: Session = Depends(get_db),
):
    """Handle OAuth callback from Basecamp — exchange code for tokens."""
    expected_state = request.session.pop("oauth_state", "")
    if not code or not expected_state or not hmac.compare_digest(state, expected_state):
        return RedirectResponse(url="/", status_code=302)

    callback_url = _get_callback_url(request)

    # Exchange authorization code for tokens
    try:
        token_resp = httpx.post(
            BASECAMP_TOKEN_URL,
            json={
                "type": "web_server",
                "client_id": settings.BASECAMP_CLIENT_ID,
                "client_secret": settings.BASECAMP_CLIENT_SECRET,
                "redirect_uri": callback_url,
                "code": code,
            },
            timeout=10.0,
        )
    except httpx.HTTPError:
        return RedirectResponse(url="/", status_code=302)
    if token_resp.status_code != 200:
        return RedirectResponse(url="/", status_code=302)

    token_data = token_resp.json()
    access_token = token_data["access_token"]
    refresh_token = token_data.get("refresh_token", "")
    expires_at = token_data.get("expires_at", "")

    # Discover the Basecamp account ID
    account_id = ""
    try:
        auth_resp = httpx.get(
            BASECAMP_AUTHORIZATION_JSON,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10.0,
        )
    except httpx.HTTPError:
        auth_resp = None
    if auth_resp and auth_resp.status_code == 200:
        auth_data = auth_resp.json()
        # Find the first Basecamp 3 (bc3) account
        for acct in auth_data.get("accounts", []):
            if acct.get("product") == "bc3":
                account_id = str(acct["id"])
                break

    # Store tokens in config table
    _set_config(db, "basecamp_access_token", access_token)
    _set_config(db, "basecamp_refresh_token", refresh_token)
    _set_config(db, "basecamp_token_expires_at", expires_at)
    _set_config(db, "basecamp_account_id", account_id)
    db.commit()

    return RedirectResponse(url="/", status_code=302)


@router.post("/auth/basecamp/disconnect")
def basecamp_disconnect(request: Request, db: Session = Depends(get_db)):
    """Remove stored Basecamp tokens."""
    for key in ("basecamp_access_token", "basecamp_refresh_token",
                "basecamp_token_expires_at", "basecamp_account_id"):
        _delete_config(db, key)
    db.commit()
    return RedirectResponse(url="/", status_code=302)
