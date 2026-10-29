"""Core team constants and project lookup helpers."""

from __future__ import annotations

from zoneinfo import ZoneInfo

from app.config import settings

# Always-monitored producer names (RADAR_CORE_TEAM, comma-separated).
CORE_TEAM = [n.strip() for n in settings.RADAR_CORE_TEAM.split(",") if n.strip()]
# Timezone for due-date math and displayed timestamps (RADAR_TIMEZONE).
USER_TIMEZONE = ZoneInfo(settings.RADAR_TIMEZONE)


def get_relevant_names(project: dict | None) -> list[str]:
    """Return deduplicated list: CORE_TEAM + project's additional producers."""
    names = list(CORE_TEAM)
    if project:
        for name in project.get("producers", []):
            stripped = name.strip()
            if stripped and stripped not in names:
                names.append(stripped)
    return names


def find_project_for_task(account, bucket_id: str) -> dict | None:
    """Find the project dict in account.projects matching a task's bucket_id."""
    if account and account.projects:
        for proj in account.projects:
            if str(proj.get("id")) == str(bucket_id):
                return proj
    return None


def resolve_producer_names(task) -> list[str]:
    """Resolve the producer names list for a task (find project, normalize, get names)."""
    from app.services.discovery import normalize_project

    account = task.account
    project = find_project_for_task(account, task.basecamp_bucket_id)
    if project:
        project = normalize_project(project)
    return get_relevant_names(project)


def get_all_producer_names(db) -> list[str]:
    """Collect all unique producer names across all active accounts.

    Returns CORE_TEAM + all per-project producers from non-archived accounts.
    """
    from app.models import Account

    names = list(CORE_TEAM)
    accounts = db.query(Account).filter(Account.is_archived.is_(False)).all()
    for account in accounts:
        for project in (account.projects or []):
            for name in project.get("producers", []):
                stripped = name.strip()
                if stripped and stripped not in names:
                    names.append(stripped)
    return names
