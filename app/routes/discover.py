"""Project discovery routes."""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Account
from app.services.basecamp import get_basecamp_client
from app.services.discovery import confirm_project_associations, discover_projects
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/discover")


@router.get("", response_class=HTMLResponse)
def discover_page(request: Request, db: Session = Depends(get_db)):
    """Show Basecamp project discovery page."""
    client = get_basecamp_client(db)
    if not client:
        return RedirectResponse(url="/", status_code=302)

    try:
        suggestions = discover_projects(client, db)
    finally:
        client.close()

    accounts = db.query(Account).filter(Account.is_archived == False).order_by(Account.name).all()  # noqa: E712

    return templates.TemplateResponse(
        request=request,
        name="discover.html",
        context={
            "suggestions": suggestions,
            "accounts": accounts,
        },
    )


@router.post("/confirm", response_class=HTMLResponse)
async def confirm_discovery(request: Request, db: Session = Depends(get_db)):
    """Process discovery form submissions."""
    form = await request.form()

    associations: list[dict] = []
    # Form fields are named: action_{project_id}, account_{project_id}, new_name_{project_id}
    # Collect all project IDs from the form
    project_ids: set[str] = set()
    for key in form.keys():
        if key.startswith("action_"):
            project_ids.add(key.removeprefix("action_"))

    for pid in project_ids:
        action = form.get(f"action_{pid}", "skip")
        if action == "skip":
            continue

        associations.append({
            "basecamp_project_id": int(pid),
            "basecamp_project_name": form.get(f"name_{pid}", ""),
            "project_type": form.get(f"type_{pid}", ""),
            "service_type": form.get(f"service_{pid}", ""),
            "action": action,
            "account_id": form.get(f"account_{pid}"),
            "new_account_name": form.get(f"new_name_{pid}", "").strip(),
        })

    if associations:
        confirm_project_associations(db, associations)

    return RedirectResponse(url="/", status_code=302)
