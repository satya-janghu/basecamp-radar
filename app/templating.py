import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.constants import USER_TIMEZONE

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def _get_nav_accounts() -> list[dict]:
    """Load account list for command palette (lightweight query, cached per-request)."""
    from app.database import SessionLocal
    from app.models import Account

    db = SessionLocal()
    try:
        accounts = (
            db.query(Account.id, Account.name)
            .filter(Account.is_archived == False)  # noqa: E712
            .order_by(Account.name)
            .all()
        )
        return [{"id": str(a.id), "name": a.name} for a in accounts]
    finally:
        db.close()


templates.env.globals["get_nav_accounts"] = _get_nav_accounts


def _relative_time(value) -> str:
    """Convert a datetime or ISO string to a relative time like '2h ago'."""
    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return value
    if not isinstance(value, datetime):
        return str(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    diff = now - value
    seconds = int(diff.total_seconds())
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 30:
        return f"{days}d ago"
    months = days // 30
    if months < 12:
        return f"{months}mo ago"
    years = days // 365
    return f"{years}y ago"


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(value) -> str:
    """Strip HTML tags from a string."""
    if not value:
        return ""
    return _HTML_TAG_RE.sub("", str(value))


PST = USER_TIMEZONE  # configurable via RADAR_TIMEZONE


def _pst_time(value, fmt: str = "short") -> str:
    """Convert a UTC datetime or ISO string to the configured local timezone.

    Formats:
      "short"     -> "Jan 9, 2:30 PM"
      "full"      -> "Jan 9, 2026 2:30 PM PST" (zone abbreviation varies)
      "time_only" -> "2:30 PM"
      "date_only" -> "Jan 9"
    """
    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return value
    if not isinstance(value, datetime):
        return str(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    pst_dt = value.astimezone(PST)
    if fmt == "full":
        tz_abbr = pst_dt.strftime("%Z")  # e.g. PST or PDT
        return pst_dt.strftime(f"%b %-d, %Y %-I:%M %p {tz_abbr}")
    elif fmt == "time_only":
        return pst_dt.strftime("%-I:%M %p")
    elif fmt == "date_only":
        return pst_dt.strftime("%b %-d")
    else:  # "short"
        return pst_dt.strftime("%b %-d, %-I:%M %p")


templates.env.filters["relative_time"] = _relative_time
templates.env.filters["strip_html"] = _strip_html
templates.env.filters["pst_time"] = _pst_time
