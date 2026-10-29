"""Project discovery — parse Basecamp project names and fuzzy-match to accounts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from rapidfuzz import fuzz
from sqlalchemy.orm import Session

from app.models import Account
from app.services.basecamp import BasecampClient

PROJECT_NAME_PATTERN = re.compile(
    r"\|\s*(.+?)\s*\|\s*//\s*(Joint|Internal)\s*//\s*(.+)"
)

FUZZY_THRESHOLD = 70


@dataclass
class ParsedProject:
    client_name: str
    project_type: str  # "Joint" or "Internal"
    service_type: str


@dataclass
class ProjectSuggestion:
    basecamp_project_id: int
    basecamp_project_name: str
    parsed: ParsedProject | None
    matched_account_id: str | None  # UUID as string
    matched_account_name: str | None
    match_confidence: float
    already_tracked: bool


def parse_project_name(name: str) -> ParsedProject | None:
    """Parse '| Client Name | // Joint // SEO' style project names."""
    m = PROJECT_NAME_PATTERN.search(name)
    if not m:
        return None
    return ParsedProject(
        client_name=m.group(1).strip(),
        project_type=m.group(2).strip(),
        service_type=m.group(3).strip(),
    )


def _get_tracked_project_ids(accounts: list[Account]) -> set[int]:
    """Collect all Basecamp project IDs already tracked across all accounts."""
    tracked: set[int] = set()
    for acct in accounts:
        for proj in acct.projects or []:
            if "id" in proj:
                tracked.add(int(proj["id"]))
    return tracked


def discover_projects(client: BasecampClient, db: Session) -> list[ProjectSuggestion]:
    """Fetch all Basecamp projects, parse names, and fuzzy-match to existing accounts."""
    bc_projects = client.list_projects()
    accounts = db.query(Account).filter(Account.is_archived == False).all()  # noqa: E712
    tracked_ids = _get_tracked_project_ids(accounts)

    suggestions: list[ProjectSuggestion] = []

    for bp in bc_projects:
        project_id = bp["id"]
        project_name = bp.get("name", "")
        parsed = parse_project_name(project_name)

        matched_account_id = None
        matched_account_name = None
        best_score = 0.0

        if parsed and accounts:
            for acct in accounts:
                score = fuzz.token_sort_ratio(parsed.client_name, acct.name)
                if score > best_score:
                    best_score = score
                    matched_account_id = str(acct.id)
                    matched_account_name = acct.name

        if best_score < FUZZY_THRESHOLD:
            matched_account_id = None
            matched_account_name = None
            best_score = 0.0

        suggestions.append(
            ProjectSuggestion(
                basecamp_project_id=project_id,
                basecamp_project_name=project_name,
                parsed=parsed,
                matched_account_id=matched_account_id,
                matched_account_name=matched_account_name,
                match_confidence=best_score,
                already_tracked=project_id in tracked_ids,
            )
        )

    # Sort: untracked first (by confidence desc), then tracked
    suggestions.sort(key=lambda s: (s.already_tracked, -s.match_confidence))
    return suggestions


def confirm_project_associations(
    db: Session, associations: list[dict]
) -> None:
    """Apply user decisions from the discovery page.

    Each dict in associations: {
        "basecamp_project_id": int,
        "basecamp_project_name": str,
        "project_type": str | None,
        "service_type": str | None,
        "action": "accept" | "create" | "assign" | "skip",
        "account_id": str | None,      # for accept/assign
        "new_account_name": str | None, # for create
    }
    """
    # Track accounts created in this batch to auto-pair same-client projects
    created_accounts: dict[str, Account] = {}  # name_lower -> Account

    for assoc in associations:
        action = assoc.get("action", "skip")
        if action == "skip":
            continue

        project_entry = {
            "id": assoc["basecamp_project_id"],
            "type": assoc.get("project_type", ""),
            "service_type": assoc.get("service_type", ""),
            "basecamp_name": assoc["basecamp_project_name"],
            "producers": [],
            "poll_interval_minutes": None,
            "last_polled_at": None,
        }

        account: Account | None = None

        if action == "create":
            name = assoc["new_account_name"].strip()
            name_lower = name.lower()
            if name_lower in created_accounts:
                # Reuse account created earlier in this batch
                account = created_accounts[name_lower]
            else:
                account = Account(name=name)
                db.add(account)
                db.flush()  # generate ID
                created_accounts[name_lower] = account
        elif action in ("accept", "assign"):
            account_id = assoc.get("account_id")
            if account_id:
                account = db.get(Account, account_id)

        if not account:
            continue

        # Reassign the list (don't mutate in-place) for SQLAlchemy change detection
        current_projects = list(account.projects or [])
        # Avoid duplicates
        if not any(p.get("id") == project_entry["id"] for p in current_projects):
            current_projects.append(project_entry)
            account.projects = current_projects

    db.commit()


def normalize_project(project: dict[str, Any]) -> dict[str, Any]:
    """Fill missing keys with defaults for projects created before schema extension."""
    project.setdefault("producers", [])
    project.setdefault("poll_interval_minutes", None)
    project.setdefault("last_polled_at", None)
    return project
