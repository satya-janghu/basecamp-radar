"""Task action routes — refresh and poll control."""

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.constants import find_project_for_task, resolve_producer_names
from app.database import get_db
from app.models import Account, Config, Task
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Task actions
# ---------------------------------------------------------------------------


@router.post(
    "/accounts/{account_id}/tasks/{task_id}/refresh",
    response_class=HTMLResponse,
)
def refresh_task(
    request: Request,
    account_id: str,
    task_id: str,
    db: Session = Depends(get_db),
):
    """Force re-fetch and re-analyze a single task."""
    from app.services.basecamp import get_basecamp_client
    from app.services.discovery import normalize_project
    from app.services.gemini import format_comments, get_gemini_client
    from app.services.scorer import score_task

    task = db.get(Task, task_id)
    if not task or str(task.account_id) != account_id:
        return HTMLResponse("Task not found", status_code=404)

    client = get_basecamp_client(db)
    if not client:
        return HTMLResponse(
            '<p class="text-red-400 text-sm p-4">Basecamp not connected</p>',
            status_code=200,
        )

    try:
        # Re-fetch todo metadata
        todo = client.get_todo(task.basecamp_bucket_id, task.basecamp_todo_id)
        if todo:
            from app.services.poller import _extract_names

            task.title = todo.get("title") or todo.get("content") or task.title
            assignees = todo.get("assignees", [])
            task.assignee = assignees[0].get("name") if assignees else None
            task.assignee_names = _extract_names(assignees) or None
            task.completion_subscribers = _extract_names(todo.get("completion_subscribers", [])) or None
            task.comment_count = todo.get("comments_count", task.comment_count)
            task.raw_data = todo
            if todo.get("due_on"):
                try:
                    task.due_date = datetime.strptime(todo["due_on"], "%Y-%m-%d").date()
                except (ValueError, TypeError):
                    pass

        # Fetch comments (cap storage to prevent unbounded JSONB growth)
        from app.services.poller import MAX_STORED_COMMENTS
        comments = client.get_comments(task.basecamp_bucket_id, task.basecamp_todo_id)
        task.raw_comments = comments[-MAX_STORED_COMMENTS:] if len(comments) > MAX_STORED_COMMENTS else comments
        task.comment_count = len(comments)

        # Update staleness from actual comment timestamps
        from app.services.poller import _update_activity_from_comments
        _update_activity_from_comments(task, comments)

        # Resolve project + producer names (needed for both Gemini and scoring)
        account = task.account
        producer_names = resolve_producer_names(task)
        project = find_project_for_task(account, task.basecamp_bucket_id)
        if project:
            project = normalize_project(project)

        # AI analysis
        gemini_client = get_gemini_client()

        if gemini_client and task.assignee:
            from app.services.scorer import compute_staleness_days

            formatted = format_comments(comments)
            all_assignees = task.assignee_names or ([task.assignee] if task.assignee else [])
            subscribers = task.completion_subscribers or []
            task_data = {
                "title": task.title,
                "project_name": (project or {}).get("basecamp_name", "Unknown"),
                "project_type": task.project_type,
                "account_name": account.name if account else "Unknown",
                "assignee": task.assignee,
                "all_assignees": all_assignees,
                "completion_subscribers": subscribers,
                "due_date": str(task.due_date) if task.due_date else None,
                "days_since_last_comment": compute_staleness_days(task),
                "comment_count": task.comment_count,
                "formatted_comments": formatted,
            }
            ai_signals, ai_success = gemini_client.analyze_task(task_data, producer_names)
            task.last_analyzed_comments_hash = hashlib.sha256(
                formatted.encode()
            ).hexdigest()
        else:
            ai_signals = None
            ai_success = False

        score_task(task, ai_signals, producer_names)
        task.has_new_activity = False
        task.last_analyzed_at = datetime.now(timezone.utc)
        task.last_checked_at = datetime.now(timezone.utc)
        if ai_success:
            task.analysis_error_count = 0
            task.analysis_status = "complete"
        else:
            task.analysis_status = "partial"
        db.commit()

    except Exception:
        logger.exception("Error refreshing task %s", task_id)
        db.rollback()
        db.expire_all()  # Re-fetch clean task state after rollback
    finally:
        client.close()

    # Return refreshed detail panel
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
# Star toggle
# ---------------------------------------------------------------------------


@router.post(
    "/accounts/{account_id}/tasks/{task_id}/star",
    response_class=HTMLResponse,
)
def toggle_star(
    request: Request,
    account_id: str,
    task_id: str,
    db: Session = Depends(get_db),
):
    """Toggle is_starred on a task, re-score, and return updated card."""
    from app.services.scorer import score_task

    task = db.get(Task, task_id)
    if not task or str(task.account_id) != account_id:
        return HTMLResponse("Task not found", status_code=404)

    producer_names = resolve_producer_names(task)
    task.is_starred = not task.is_starred
    score_task(task, None, producer_names)
    db.commit()

    return _task_response(request, task)


# ---------------------------------------------------------------------------
# Dismiss / Snooze / Restore
# ---------------------------------------------------------------------------


@router.post(
    "/accounts/{account_id}/tasks/{task_id}/dismiss",
    response_class=HTMLResponse,
)
def dismiss_task(
    request: Request,
    account_id: str,
    task_id: str,
    db: Session = Depends(get_db),
):
    """Dismiss a task — hides it from default views until manually restored."""
    task = db.get(Task, task_id)
    if not task or str(task.account_id) != account_id:
        return HTMLResponse("Task not found", status_code=404)

    task.dismissed_at = datetime.now(timezone.utc)
    task.snoozed_until = None  # Mutually exclusive
    # Auto-downvote: dismissal is an implicit negative signal
    task.vote = -1
    task.voted_at = datetime.now(timezone.utc)
    task.vote_context = _build_vote_context(task, source="dismiss")
    db.commit()

    return HTMLResponse("", headers={"HX-Trigger": "taskHidden"})


@router.post(
    "/accounts/{account_id}/tasks/{task_id}/snooze",
    response_class=HTMLResponse,
)
def snooze_task(
    request: Request,
    account_id: str,
    task_id: str,
    until: str = Form(...),
    db: Session = Depends(get_db),
):
    """Snooze a task until a given datetime (ISO format, UTC)."""
    task = db.get(Task, task_id)
    if not task or str(task.account_id) != account_id:
        return HTMLResponse("Task not found", status_code=404)

    try:
        snooze_dt = datetime.fromisoformat(until.replace("Z", "+00:00"))
        if snooze_dt.tzinfo is None:
            snooze_dt = snooze_dt.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return HTMLResponse("Invalid date", status_code=400)

    if snooze_dt <= datetime.now(timezone.utc):
        return HTMLResponse("Snooze time must be in the future", status_code=400)

    task.snoozed_until = snooze_dt
    task.dismissed_at = None  # Mutually exclusive
    db.commit()

    return HTMLResponse("", headers={"HX-Trigger": "taskHidden"})


@router.post(
    "/accounts/{account_id}/tasks/{task_id}/restore",
    response_class=HTMLResponse,
)
def restore_task(
    request: Request,
    account_id: str,
    task_id: str,
    db: Session = Depends(get_db),
):
    """Restore a dismissed or snoozed task back to normal visibility."""
    task = db.get(Task, task_id)
    if not task or str(task.account_id) != account_id:
        return HTMLResponse("Task not found", status_code=404)

    task.dismissed_at = None
    task.snoozed_until = None
    db.commit()

    from datetime import date

    account = task.account
    return templates.TemplateResponse(
        request=request,
        name="partials/task_card.html",
        context={"task": task, "account": account, "today": date.today()},
        headers={"HX-Trigger": "taskRestored"},
    )


# ---------------------------------------------------------------------------
# Vote (upvote / downvote)
# ---------------------------------------------------------------------------


@router.post(
    "/accounts/{account_id}/tasks/{task_id}/vote",
    response_class=HTMLResponse,
)
def vote_task(
    request: Request,
    account_id: str,
    task_id: str,
    value: int = Form(...),
    source: str = Form(default=""),
    db: Session = Depends(get_db),
):
    """Set or toggle an upvote/downvote on a task. No scoring impact."""
    task = db.get(Task, task_id)
    if not task or str(task.account_id) != account_id:
        return HTMLResponse("Task not found", status_code=404)

    if value not in (1, -1, 0):
        return HTMLResponse("Invalid vote value", status_code=400)

    clearing = value == 0 or (task.vote == value)

    if clearing:
        task.vote = None
        task.voted_at = None
        task.vote_context = None
    else:
        task.vote = value
        task.voted_at = datetime.now(timezone.utc)
        task.vote_context = _build_vote_context(task, source or None)

    db.commit()

    return _task_response(request, task)


# ---------------------------------------------------------------------------
# Batch star
# ---------------------------------------------------------------------------


class BatchStarRequest(BaseModel):
    task_ids: List[str]
    starred: bool


class BatchDismissRequest(BaseModel):
    task_ids: List[str]


class BatchSnoozeRequest(BaseModel):
    task_ids: List[str]
    until: str  # UTC ISO 8601


class BatchVoteRequest(BaseModel):
    task_ids: List[str]
    value: int  # 1 or -1


BATCH_LIMIT = 50


def _batch_modify(db: Session, task_ids: list[str], modify_fn, eager_load_account: bool = False) -> JSONResponse:
    """Fetch tasks by IN query (capped), apply modify_fn to each, commit, return count."""
    from sqlalchemy.orm import joinedload

    capped_ids = task_ids[:BATCH_LIMIT]
    query = db.query(Task).filter(Task.id.in_(capped_ids))
    if eager_load_account:
        query = query.options(joinedload(Task.account))
    tasks = query.all()
    for task in tasks:
        modify_fn(task)
    db.commit()
    return JSONResponse({"status": "ok", "updated": len(tasks)})


@router.post("/api/tasks/batch-star")
def batch_star(
    request: Request,
    body: BatchStarRequest,
    db: Session = Depends(get_db),
):
    """Batch star/unstar multiple tasks and re-score them."""
    from app.services.scorer import score_task

    def modify(task):
        task.is_starred = body.starred
        score_task(task, None, resolve_producer_names(task))

    return _batch_modify(db, body.task_ids, modify, eager_load_account=True)


@router.post("/api/tasks/batch-dismiss")
def batch_dismiss(
    request: Request,
    body: BatchDismissRequest,
    db: Session = Depends(get_db),
):
    """Batch dismiss tasks with auto-downvote."""
    now = datetime.now(timezone.utc)

    def modify(task):
        task.dismissed_at = now
        task.snoozed_until = None
        task.vote = -1
        task.voted_at = now
        task.vote_context = _build_vote_context(task, source="batch_dismiss")

    return _batch_modify(db, body.task_ids, modify, eager_load_account=True)


@router.post("/api/tasks/batch-snooze")
def batch_snooze(
    request: Request,
    body: BatchSnoozeRequest,
    db: Session = Depends(get_db),
):
    """Batch snooze tasks until a given datetime."""
    try:
        snooze_dt = datetime.fromisoformat(body.until.replace("Z", "+00:00"))
        if snooze_dt.tzinfo is None:
            snooze_dt = snooze_dt.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return JSONResponse({"error": "Invalid date"}, status_code=400)

    if snooze_dt <= datetime.now(timezone.utc):
        return JSONResponse({"error": "Snooze time must be in the future"}, status_code=400)

    def modify(task):
        task.snoozed_until = snooze_dt
        task.dismissed_at = None

    return _batch_modify(db, body.task_ids, modify)


@router.post("/api/tasks/batch-vote")
def batch_vote(
    request: Request,
    body: BatchVoteRequest,
    db: Session = Depends(get_db),
):
    """Batch set vote on tasks (set semantics, not toggle)."""
    if body.value not in (1, -1):
        return JSONResponse({"error": "Value must be 1 or -1"}, status_code=400)

    now = datetime.now(timezone.utc)
    source = "batch_upvote" if body.value == 1 else "batch_downvote"

    def modify(task):
        task.vote = body.value
        task.voted_at = now
        task.vote_context = _build_vote_context(task, source=source)

    return _batch_modify(db, body.task_ids, modify, eager_load_account=True)


# ---------------------------------------------------------------------------
# Account-level refresh
# ---------------------------------------------------------------------------


@router.post("/accounts/{account_id}/refresh")
def refresh_account_tasks(
    account_id: str,
    db: Session = Depends(get_db),
):
    """Mark all active tasks in an account for re-analysis on next poll."""
    account = db.get(Account, account_id)
    if not account:
        return RedirectResponse(url="/", status_code=302)

    updated = (
        db.query(Task)
        .filter(
            Task.account_id == account.id,
            Task.status == "active",
        )
        .update({Task.has_new_activity: True}, synchronize_session="fetch")
    )
    db.commit()
    logger.info("Marked %d tasks for re-analysis in account %s", updated, account.name)
    return RedirectResponse(url=f"/accounts/{account_id}", status_code=302)


# ---------------------------------------------------------------------------
# Poll control API
# ---------------------------------------------------------------------------


@router.post("/api/radar/refresh")
async def trigger_poll_cycle(request: Request):
    """Trigger an immediate poll cycle (fire and forget)."""
    from app.services.scheduler import run_poll_cycle

    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler:
        await scheduler.add_job(run_poll_cycle)
        return JSONResponse({"status": "triggered"})
    else:
        # No scheduler — run directly
        asyncio.ensure_future(run_poll_cycle())
        return JSONResponse({"status": "triggered_direct"})


@router.post("/api/radar/reanalyze")
async def reanalyze_all(request: Request, db: Session = Depends(get_db)):
    """Force re-analysis of ALL active tasks, then trigger a poll cycle."""
    count = (
        db.query(Task)
        .filter(Task.status == "active")
        .update({Task.has_new_activity: True}, synchronize_session="fetch")
    )
    db.commit()
    logger.info("Marked %d active tasks for re-analysis", count)

    # Trigger immediate poll cycle
    from app.services.scheduler import run_poll_cycle

    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler:
        await scheduler.add_job(run_poll_cycle)
    else:
        asyncio.ensure_future(run_poll_cycle())

    return JSONResponse({"status": "triggered", "tasks_marked": count})


@router.get("/api/radar/status")
def poll_status(db: Session = Depends(get_db)):
    """Return current poll status and task counts."""
    last_poll_row = db.get(Config, "last_poll_at")
    last_closed_row = db.get(Config, "last_closed_check_at")
    poll_running_row = db.get(Config, "poll_running")

    from sqlalchemy import func

    counts = (
        db.query(
            func.count().label("total"),
            func.count().filter(Task.status == "active").label("active"),
            func.count().filter(Task.status == "closed").label("closed"),
            func.count().filter(Task.status == "archived").label("archived"),
            func.count().filter(Task.has_new_activity.is_(True)).label("pending_analysis"),
        )
        .one()
    )

    from app.services.gemini import get_gemini_rate_stats

    poll_running = bool(poll_running_row and poll_running_row.value)

    return JSONResponse({
        "last_poll_at": str(last_poll_row.value) if last_poll_row and last_poll_row.value else None,
        "last_closed_check_at": str(last_closed_row.value) if last_closed_row and last_closed_row.value else None,
        "poll_running": poll_running,
        "poll_started_at": str(poll_running_row.value) if poll_running else None,
        "tasks": {
            "total": counts.total,
            "active": counts.active,
            "closed": counts.closed,
            "archived": counts.archived,
            "pending_analysis": counts.pending_analysis,
        },
        "gemini_rate_limit": get_gemini_rate_stats(),
    })


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task_response(request: Request, task: Task):
    """Return task_detail or task_card partial depending on ?context= param."""
    from datetime import date

    context = request.query_params.get("context", "")
    if context == "detail":
        from app.services.scorer import MAX_POSSIBLE, build_display_segments
        return templates.TemplateResponse(
            request=request,
            name="partials/task_detail.html",
            context={
                "task": task,
                "today": date.today(),
                "max_score": MAX_POSSIBLE,
                "score_segments": build_display_segments(task.score_breakdown),
            },
        )
    return templates.TemplateResponse(
        request=request,
        name="partials/task_card.html",
        context={"task": task, "account": task.account, "today": date.today()},
    )


def _build_vote_context(task: Task, source: str | None = None) -> dict:
    """Build a scoring context snapshot for vote/dismiss analytics."""
    from app.services.scorer import compute_staleness_days

    return {
        "score": task.priority_score,
        "tier": task.priority_tier,
        "staleness_days": compute_staleness_days(task),
        "signals": task.ai_signals,
        "ball_with": task.ai_ball_with,
        "stage": task.ai_current_stage,
        "project_type": task.project_type,
        "account_name": task.account.name if task.account else None,
        "score_breakdown": task.score_breakdown,
        "source": source,
    }

