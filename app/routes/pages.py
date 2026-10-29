"""Page routes — dashboard, account view, and HTMX partials."""

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session

from app.constants import USER_TIMEZONE
from app.database import get_db
from app.models import Account, Config, Task
from app.templating import templates

router = APIRouter()


# ---------------------------------------------------------------------------
# Dashboard shared query builder
# ---------------------------------------------------------------------------


def _build_dashboard_context(db: Session) -> dict:
    """Build shared context for dashboard and dashboard partial."""
    token_row = db.get(Config, "basecamp_access_token")
    basecamp_connected = bool(token_row and token_row.value)

    last_poll_row = db.get(Config, "last_poll_at")
    last_poll = last_poll_row.value if last_poll_row else None

    # Aggregate task counts per account and tier (only non-archived accounts)
    tier_counts = (
        db.query(
            Task.account_id,
            func.count().label("total"),
            func.count().filter(Task.priority_tier == "critical").label("critical"),
            func.count().filter(Task.priority_tier == "high").label("high"),
            func.count().filter(Task.priority_tier == "medium").label("medium"),
            func.count().filter(Task.priority_tier == "low").label("low"),
            func.count().filter(Task.priority_tier == "minimal").label("minimal"),
        )
        .join(Account, Task.account_id == Account.id)
        .filter(Task.status == "active", Account.is_archived == False, *_visibility_conditions())  # noqa: E712
        .group_by(Task.account_id)
        .all()
    )
    counts_by_account = {
        row.account_id: {
            "total": row.total, "critical": row.critical, "high": row.high,
            "medium": row.medium, "low": row.low, "minimal": row.minimal,
        }
        for row in tier_counts
    }

    # Global totals
    totals = {"total": 0, "critical": 0, "high": 0, "medium": 0, "low": 0, "minimal": 0}
    for c in counts_by_account.values():
        for k in totals:
            totals[k] += c[k]

    # Count tasks pending analysis
    pending_analysis = (
        db.query(func.count())
        .select_from(Task)
        .join(Account, Task.account_id == Account.id)
        .filter(
            Task.status == "active",
            Account.is_archived == False,  # noqa: E712
            Task.analysis_status.in_(["pending", "partial"]),
            *_visibility_conditions(),
        )
        .scalar()
    ) or 0
    totals["pending"] = pending_analysis

    # Active accounts sorted by name (re-sorted below)
    accounts = (
        db.query(Account)
        .filter(Account.is_archived == False)  # noqa: E712
        .order_by(Account.name)
        .all()
    )

    # Aggregate activity info per account
    activity_info = (
        db.query(
            Task.account_id,
            func.max(Task.last_known_activity_at).label("last_activity"),
            func.count().filter(Task.has_new_activity.is_(True)).label("new_activity_count"),
        )
        .join(Account, Task.account_id == Account.id)
        .filter(Task.status == "active", Account.is_archived == False, *_visibility_conditions())  # noqa: E712
        .group_by(Task.account_id)
        .all()
    )
    activity_by_account = {
        row.account_id: {"last_activity": row.last_activity, "new_activity_count": row.new_activity_count}
        for row in activity_info
    }

    # Attach counts to each account
    account_data = []
    for acct in accounts:
        c = counts_by_account.get(acct.id, {"total": 0, "critical": 0, "high": 0, "medium": 0, "low": 0, "minimal": 0})
        a = activity_by_account.get(acct.id, {"last_activity": None, "new_activity_count": 0})
        account_data.append({"account": acct, **c, **a})

    # Sort: weighted severity score, then critical count, then name
    account_data.sort(key=lambda a: (-(4 * a["critical"] + 2 * a["high"] + a["medium"]), -a["critical"], a["account"].name))

    return {
        "basecamp_connected": basecamp_connected,
        "last_poll": last_poll,
        "totals": totals,
        "account_data": account_data,
    }


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context=_build_dashboard_context(db),
    )


@router.get("/partials/dashboard-stats", response_class=HTMLResponse)
def dashboard_stats_partial(request: Request, db: Session = Depends(get_db)):
    """HTMX partial: dashboard summary stats + account grid for auto-refresh."""
    return templates.TemplateResponse(
        request=request,
        name="partials/dashboard_content.html",
        context=_build_dashboard_context(db),
    )


@router.get("/accounts/{account_id}", response_class=HTMLResponse)
def account_view(
    request: Request,
    account_id: str,
    tier: str = Query(default="all"),
    db: Session = Depends(get_db),
):
    account = db.get(Account, account_id)
    if not account:
        return HTMLResponse("Account not found", status_code=404)

    # Base query for active, visible tasks
    base_q = (
        db.query(Task)
        .filter(Task.account_id == account.id, Task.status == "active", *_visibility_conditions())
    )

    # Tier counts for tab labels
    tier_counts = (
        base_q.with_entities(
            func.count().label("total"),
            func.count().filter(Task.priority_tier == "critical").label("critical"),
            func.count().filter(Task.priority_tier == "high").label("high"),
            func.count().filter(Task.priority_tier == "medium").label("medium"),
            func.count().filter(Task.priority_tier == "low").label("low"),
            func.count().filter(Task.priority_tier == "minimal").label("minimal"),
        ).one()
    )

    # Grouped counts for tabs
    urgent_count = tier_counts.critical + tier_counts.high
    monitor_count = tier_counts.medium
    background_count = tier_counts.low + tier_counts.minimal

    # Filter tasks by grouped tier tab
    tasks_q = base_q
    if tier == "urgent":
        tasks_q = tasks_q.filter(Task.priority_tier.in_(["critical", "high"]))
    elif tier == "monitor":
        tasks_q = tasks_q.filter(Task.priority_tier == "medium")
    elif tier == "background":
        tasks_q = tasks_q.filter(Task.priority_tier.in_(["low", "minimal"]))

    tasks = tasks_q.order_by(
        case(
            (Task.priority_tier == "critical", 0),
            (Task.priority_tier == "high", 1),
            (Task.priority_tier == "medium", 2),
            (Task.priority_tier == "low", 3),
            (Task.priority_tier == "minimal", 4),
            else_=5,
        ),
        Task.priority_score.desc(),
    ).all()

    return templates.TemplateResponse(
        request=request,
        name="account.html",
        context={
            "account": account,
            "tasks": tasks,
            "tier": tier,
            "tier_counts": {
                "all": tier_counts.total,
                "urgent": urgent_count,
                "monitor": monitor_count,
                "background": background_count,
            },
            "today": date.today(),
        },
    )


def _visibility_conditions(filters: str = ""):
    """Return SQLAlchemy filter conditions for visibility.

    Always hides unassigned tasks. Additionally handles dismiss/snooze filters.
    """
    filter_set = {f.strip() for f in (filters or "").split(",") if f.strip()}
    now = datetime.now(timezone.utc)
    base = [Task.assignee.isnot(None)]
    if "dismissed" in filter_set:
        return base + [Task.dismissed_at.isnot(None)]
    elif "snoozed" in filter_set:
        return base + [Task.snoozed_until.isnot(None), Task.snoozed_until > now]
    else:
        # Default: hide dismissed and actively-snoozed tasks
        return base + [
            Task.dismissed_at.is_(None),
            or_(Task.snoozed_until.is_(None), Task.snoozed_until <= now),
        ]


def _apply_smart_filters(query, filters: str, db: Session | None = None):
    """Apply smart filter predicates to a Task query.

    Accepts comma-separated filter names: ball_with_me, blocker, approval, stale7, starred.
    Filters combine with AND logic. Pass db for ball_with_me to match all producers.
    """
    if not filters:
        return query

    filter_set = {f.strip() for f in filters.split(",") if f.strip()}
    stale_cutoff = datetime.now(timezone.utc) - timedelta(days=7)

    for f in filter_set:
        if f == "ball_with_me":
            from app.constants import CORE_TEAM, get_all_producer_names
            names = get_all_producer_names(db) if db else list(CORE_TEAM)
            # Substring match: Gemini may return a first name or a full name
            query = query.filter(or_(
                *[func.lower(Task.ai_ball_with).contains(n.lower()) for n in names]
            ))
        elif f == "blocker":
            query = query.filter(Task.ai_signals["is_blocked"].as_boolean() == True)  # noqa: E712
        elif f == "approval":
            query = query.filter(Task.ai_signals["needs_approval"].as_boolean() == True)  # noqa: E712
        elif f == "stale7":
            query = query.filter(
                func.coalesce(Task.last_known_activity_at, Task.added_at) <= stale_cutoff
            )
        elif f == "starred":
            query = query.filter(Task.is_starred == True)  # noqa: E712
        elif f == "upvoted":
            query = query.filter(Task.vote == 1)
        elif f == "downvoted":
            query = query.filter(Task.vote == -1)
        elif f in ("dismissed", "snoozed"):
            pass  # Handled by _visibility_conditions

    return query


@router.get("/accounts/{account_id}/tasks", response_class=HTMLResponse)
def account_tasks_partial(
    request: Request,
    account_id: str,
    tier: str = Query(default="all"),
    filters: str = Query(default=""),
    db: Session = Depends(get_db),
):
    """HTMX partial: filtered task list for account view."""
    account = db.get(Account, account_id)
    if not account:
        return HTMLResponse('<p class="text-slate-400 text-sm p-4">Account not found</p>', status_code=404)

    base_q = (
        db.query(Task)
        .filter(Task.account_id == account.id, Task.status == "active", *_visibility_conditions(filters))
    )

    if tier == "urgent":
        base_q = base_q.filter(Task.priority_tier.in_(["critical", "high"]))
    elif tier == "monitor":
        base_q = base_q.filter(Task.priority_tier == "medium")
    elif tier == "background":
        base_q = base_q.filter(Task.priority_tier.in_(["low", "minimal"]))

    base_q = _apply_smart_filters(base_q, filters, db=db)

    tasks = base_q.order_by(
        case(
            (Task.priority_tier == "critical", 0),
            (Task.priority_tier == "high", 1),
            (Task.priority_tier == "medium", 2),
            (Task.priority_tier == "low", 3),
            (Task.priority_tier == "minimal", 4),
            else_=5,
        ),
        Task.priority_score.desc(),
    ).all()

    return templates.TemplateResponse(
        request=request,
        name="partials/task_list.html",
        context={"tasks": tasks, "account": account, "today": date.today(), "active_filters": filters},
    )


@router.get("/accounts/{account_id}/tasks/{task_id}/comments", response_class=HTMLResponse)
def task_comments_partial(
    request: Request,
    account_id: str,
    task_id: str,
    db: Session = Depends(get_db),
):
    """HTMX partial: expanded comment list for task detail."""
    task = db.get(Task, task_id)
    if not task:
        return HTMLResponse('<p class="text-slate-400 text-sm p-4">Task not found</p>', status_code=404)

    return templates.TemplateResponse(
        request=request,
        name="partials/comment_preview.html",
        context={
            "task": task,
            "comments": task.raw_comments or [],
            "show_all": True,
        },
    )


@router.get("/accounts/{account_id}/tasks/{task_id}/detail", response_class=HTMLResponse)
def task_detail_partial(
    request: Request,
    account_id: str,
    task_id: str,
    db: Session = Depends(get_db),
):
    """HTMX partial: task detail panel."""
    task = db.get(Task, task_id)
    if not task:
        return HTMLResponse('<p class="text-slate-400 text-sm p-4">Task not found</p>', status_code=404)

    from app.services.scorer import MAX_POSSIBLE, build_display_segments
    return templates.TemplateResponse(
        request=request,
        name="partials/task_detail.html",
        context={
            "task": task,
            "max_score": MAX_POSSIBLE,
            "score_segments": build_display_segments(task.score_breakdown),
        },
    )


# ---------------------------------------------------------------------------
# Morning Briefing
# ---------------------------------------------------------------------------

BRIEFING_LIMIT = 50


def _briefing_query(db: Session, filters: str = "") -> tuple[list, dict]:
    """Shared query for briefing views: returns (tasks, accounts_map)."""
    base_q = (
        db.query(Task)
        .join(Account, Task.account_id == Account.id)
        .filter(Task.status == "active", Account.is_archived == False, *_visibility_conditions(filters))  # noqa: E712
    )

    base_q = base_q.filter(Task.priority_tier.in_(["critical", "high"]))
    if filters:
        base_q = _apply_smart_filters(base_q, filters, db=db)

    tasks = base_q.order_by(Task.priority_score.desc()).limit(BRIEFING_LIMIT).all()

    account_ids = {t.account_id for t in tasks}
    accounts_map = {}
    if account_ids:
        accts = db.query(Account).filter(Account.id.in_(account_ids)).all()
        accounts_map = {a.id: a for a in accts}

    return tasks, accounts_map


@router.get("/briefing", response_class=HTMLResponse)
def briefing(
    request: Request,
    filters: str = Query(default=""),
    db: Session = Depends(get_db),
):
    """Morning Briefing — cross-account priority view."""
    tasks, accounts_map = _briefing_query(db, filters)
    return templates.TemplateResponse(
        request=request,
        name="briefing.html",
        context={
            "tasks": tasks,
            "accounts_map": accounts_map,
            "task_count": len(tasks),
            "today": datetime.now(USER_TIMEZONE).date(),
            "active_filters": filters,
        },
    )


@router.get("/manual", response_class=HTMLResponse)
def manual_page(request: Request):
    return templates.TemplateResponse(request=request, name="manual.html")


@router.get("/briefing/tasks", response_class=HTMLResponse)
def briefing_tasks_partial(
    request: Request,
    filters: str = Query(default=""),
    db: Session = Depends(get_db),
):
    """HTMX partial: filtered briefing task list."""
    tasks, accounts_map = _briefing_query(db, filters)
    return templates.TemplateResponse(
        request=request,
        name="partials/briefing_task_list.html",
        context={
            "tasks": tasks,
            "accounts_map": accounts_map,
            "task_count": len(tasks),
            "today": datetime.now(USER_TIMEZONE).date(),
            "active_filters": filters,
        },
    )
