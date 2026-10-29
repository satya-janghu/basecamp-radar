"""Account CRUD routes (HTMX-friendly form POSTs)."""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Account, Task
from app.services.discovery import normalize_project
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/accounts")


@router.post("", response_class=HTMLResponse)
async def create_account(request: Request, db: Session = Depends(get_db)):
    """Create a new account from form data."""
    form = await request.form()
    name = form.get("name", "").strip()
    if not name:
        return RedirectResponse(url="/", status_code=302)

    account = Account(name=name)
    db.add(account)
    db.commit()
    return RedirectResponse(url=f"/accounts/{account.id}", status_code=302)


@router.post("/{account_id}", response_class=HTMLResponse)
async def update_account(account_id: str, request: Request, db: Session = Depends(get_db)):
    """Update account name."""
    account = db.get(Account, account_id)
    if not account:
        return RedirectResponse(url="/", status_code=302)

    form = await request.form()
    name = form.get("name", "").strip()
    if name:
        account.name = name
        db.commit()

    return RedirectResponse(url=f"/accounts/{account.id}", status_code=302)


@router.post("/{account_id}/archive", response_class=HTMLResponse)
def archive_account(account_id: str, db: Session = Depends(get_db)):
    """Toggle account archive status."""
    account = db.get(Account, account_id)
    if not account:
        return RedirectResponse(url="/", status_code=302)

    account.is_archived = not account.is_archived
    db.commit()
    return RedirectResponse(url="/", status_code=302)


@router.post("/{account_id}/delete", response_class=HTMLResponse)
def delete_account(account_id: str, db: Session = Depends(get_db)):
    """Delete account and its associated tasks."""
    account = db.get(Account, account_id)
    if not account:
        return RedirectResponse(url="/", status_code=302)

    db.query(Task).filter(Task.account_id == account.id).delete()
    db.delete(account)
    db.commit()
    return RedirectResponse(url="/", status_code=302)


@router.get("/{account_id}/edit", response_class=HTMLResponse)
def edit_account_form(account_id: str, request: Request, db: Session = Depends(get_db)):
    """Render inline edit form partial."""
    account = db.get(Account, account_id)
    if not account:
        return HTMLResponse('<p class="text-slate-400 text-sm p-4">Account not found</p>', status_code=404)

    return templates.TemplateResponse(
        request=request,
        name="partials/account_edit.html",
        context={"account": account},
    )


# ---------------------------------------------------------------------------
# Project settings
# ---------------------------------------------------------------------------


@router.get("/{account_id}/settings", response_class=HTMLResponse)
def account_settings_form(
    account_id: str, request: Request, db: Session = Depends(get_db),
):
    """Render account-level settings partial (shared across all projects)."""
    account = db.get(Account, account_id)
    if not account:
        return HTMLResponse('<p class="text-slate-400 text-sm p-4">Account not found</p>', status_code=404)

    projects = [normalize_project({**p}) for p in (account.projects or [])]
    # Read producers + poll_interval from the first project (shared across all)
    first = projects[0] if projects else {}
    producers = first.get("producers", [])
    poll_interval = first.get("poll_interval_minutes")

    return templates.TemplateResponse(
        request=request,
        name="partials/project_settings.html",
        context={
            "account_id": account_id,
            "producers": producers,
            "poll_interval_minutes": poll_interval,
            "projects": projects,
        },
    )


@router.post("/{account_id}/settings", response_class=HTMLResponse)
async def save_account_settings(
    account_id: str, request: Request, db: Session = Depends(get_db),
):
    """Update settings for all projects in the account."""
    account = db.get(Account, account_id)
    if not account:
        return HTMLResponse("Account not found", status_code=404)

    form = await request.form()

    # Parse producers from comma-separated string
    producers_raw = form.get("producers", "")
    producers = [n.strip() for n in producers_raw.split(",") if n.strip()]

    # Parse poll interval (None if blank/0)
    poll_raw = form.get("poll_interval_minutes", "")
    poll_interval = None
    if poll_raw.strip():
        try:
            val = int(poll_raw)
            poll_interval = max(val, 5) if val > 0 else None
        except (ValueError, TypeError):
            pass

    # Apply same values to ALL projects, reassign for SQLAlchemy change detection
    updated = []
    for p in (account.projects or []):
        p = normalize_project({**p})
        p["producers"] = producers
        p["poll_interval_minutes"] = poll_interval
        updated.append(p)
    account.projects = updated
    db.commit()

    logger.info(
        "Account settings saved for %s: producers=%s, poll=%s",
        account.name, producers, poll_interval,
    )

    return templates.TemplateResponse(
        request=request,
        name="partials/project_settings.html",
        context={
            "account_id": account_id,
            "producers": producers,
            "poll_interval_minutes": poll_interval,
            "projects": updated,
        },
    )
