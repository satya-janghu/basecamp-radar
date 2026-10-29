"""Global settings page — view and update app configuration."""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.constants import CORE_TEAM
from app.database import get_db
from app.models import Config, PollRun
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings")


@router.get("", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    """Render the global settings page."""
    config = _load_config(db)
    poll_history = (
        db.query(PollRun)
        .order_by(PollRun.started_at.desc())
        .limit(10)
        .all()
    )
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "config": config,
            "core_team": CORE_TEAM,
            "poll_history": poll_history,
        },
    )


@router.post("", response_class=HTMLResponse)
async def save_settings(request: Request, db: Session = Depends(get_db)):
    """Save global settings to config table."""
    form = await request.form()

    updates = {
        "poll_interval_minutes": _parse_int(form.get("poll_interval_minutes"), 30, 5),
        "closed_check_interval_hours": _parse_int(form.get("closed_check_interval_hours"), 24, 1),
        "archive_after_months": _parse_int(form.get("archive_after_months"), 3, 1),
    }

    for key, value in updates.items():
        row = db.get(Config, key)
        if row:
            row.value = value
        else:
            db.add(Config(key=key, value=value))

    db.commit()
    logger.info("Settings updated: %s", updates)
    return RedirectResponse(url="/settings", status_code=302)


def _load_config(db: Session) -> dict:
    """Load all relevant config values into a dict."""
    keys = [
        "poll_interval_minutes",
        "closed_check_interval_hours",
        "archive_after_months",
        "last_poll_at",
        "last_closed_check_at",
        "basecamp_access_token",
    ]
    config = {}
    for key in keys:
        row = db.get(Config, key)
        config[key] = row.value if row else None
    config["basecamp_connected"] = bool(config.get("basecamp_access_token"))
    return config


def _parse_int(value: str | None, default: int, minimum: int = 1) -> int:
    """Parse an integer from form input with default and floor."""
    if not value:
        return default
    try:
        return max(int(value), minimum)
    except (ValueError, TypeError):
        return default
