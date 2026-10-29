"""Basecamp polling service — discovers new tasks and detects changes."""

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session, selectinload

from app.models import Account, Task
from app.services.basecamp import AsyncBasecampClient
from app.services.discovery import normalize_project

logger = logging.getLogger(__name__)

CONCURRENCY_LIMIT = 10  # Max concurrent Basecamp API requests
ANALYSIS_BATCH_SIZE = 10  # Commit every N tasks in analysis pass
AI_CACHE_TTL_DAYS = 7  # Force re-analysis after this many days
MAX_ANALYSIS_RETRIES = 3  # Stop retrying after this many consecutive failures
MAX_STORED_COMMENTS = 20  # Cap raw_comments storage
MAX_GROUP_DEPTH = 5  # Max recursion depth for nested todolist groups


def _extract_names(people: list[dict] | None) -> list[str]:
    """Extract name strings from a list of Basecamp person objects."""
    if not people:
        return []
    return [p.get("name") for p in people if p.get("name")]


# ---------------------------------------------------------------------------
# Per-project poll interval helpers
# ---------------------------------------------------------------------------


def _get_global_poll_interval(db: Session) -> int:
    """Read global poll interval from config, default 30 minutes."""
    from app.models import Config

    row = db.get(Config, "poll_interval_minutes")
    if row and row.value is not None:
        return int(row.value)
    return 30


def _is_project_due(project: dict, global_interval: int) -> bool:
    """Check if a project is due for polling based on its interval."""
    project = normalize_project(project)
    interval = project["poll_interval_minutes"] or global_interval
    last_polled = project.get("last_polled_at")
    if not last_polled:
        return True  # never polled
    try:
        last_dt = datetime.fromisoformat(last_polled)
        return datetime.now(timezone.utc) >= last_dt + timedelta(minutes=interval)
    except (ValueError, TypeError):
        return True


def _update_project_polled(db: Session, account: Account, project_id) -> None:
    """Stamp last_polled_at on a project inside account's JSONB."""
    now_iso = datetime.now(timezone.utc).isoformat()
    updated = []
    for p in (account.projects or []):
        if str(p.get("id")) == str(project_id):
            p = {**p, "last_polled_at": now_iso}
        updated.append(p)
    account.projects = updated  # reassign for SQLAlchemy change detection


# ---------------------------------------------------------------------------
# Pass 1: Discovery — find new todos across all configured projects
# ---------------------------------------------------------------------------


async def discovery_pass(client: AsyncBasecampClient, db: Session, due_bucket_ids: set[str] | None = None) -> dict:
    """Discover new todos across all configured projects.

    For each account's projects, fetches open todos and creates Task records
    for any that aren't already tracked. Newly created tasks are immediately
    scored with deterministic-only scoring so they always have a tier.

    Returns dict with counts: {accounts, projects, todos_found, tasks_created}.
    """
    from app.services.scorer import score_task

    stats = {"accounts": 0, "projects": 0, "todos_found": 0, "tasks_created": 0}
    sem = asyncio.Semaphore(CONCURRENCY_LIMIT)

    accounts = (
        db.query(Account)
        .filter(Account.is_archived.is_(False))
        .all()
    )

    for account in accounts:
        if not account.projects:
            continue
        stats["accounts"] += 1

        for project in account.projects:
            project_id = project.get("id")
            if not project_id:
                continue

            # Skip projects not due for polling
            if due_bucket_ids is not None and str(project_id) not in due_bucket_ids:
                continue

            stats["projects"] += 1

            try:
                todos = await _fetch_project_todos(client, sem, project_id)
                stats["todos_found"] += len(todos)

                created = _create_new_tasks(
                    db, account, project, todos, client.account_id,
                )
                stats["tasks_created"] += created
                _update_project_polled(db, account, project_id)
            except Exception:
                logger.exception(
                    "Discovery: error fetching project %s for account %s",
                    project_id,
                    account.name,
                )

    # Score any newly created tasks (NULL tier) with deterministic-only scoring
    unscored = (
        db.query(Task)
        .filter(Task.status == "active", Task.priority_tier.is_(None))
        .all()
    )
    if unscored:
        for task in unscored:
            try:
                score_task(task, None)
                # Mark as pending analysis since no AI signals yet
                task.analysis_status = "pending"
            except Exception:
                logger.exception("Discovery: failed to score new task %s", task.id)

    db.commit()

    logger.info(
        "Discovery pass: %d accounts, %d projects, %d todos found, %d tasks created",
        stats["accounts"],
        stats["projects"],
        stats["todos_found"],
        stats["tasks_created"],
    )
    return stats


async def _fetch_project_todos(
    client: AsyncBasecampClient,
    sem: asyncio.Semaphore,
    project_id: int | str,
) -> list[dict]:
    """Fetch all open todos for a project, including nested todolist groups.

    Traverses: Project → Todoset → Todolists → Todos + Groups → (recurse).
    Groups are sub-lists within todolists that contain their own todos.
    """
    todoset_id = await client.get_todoset_id(project_id)
    if not todoset_id:
        return []

    todolists = await client.get_todolists(project_id, todoset_id)

    all_todos: list[dict] = []
    total_groups = 0

    async def fetch_todolist_todos(todolist_id: int | str, depth: int = 0) -> list[dict]:
        """Fetch todos from a todolist, then recurse into its groups."""
        nonlocal total_groups

        # Single semaphore acquire for both API calls — avoids deadlock
        async with sem:
            todos = await client.get_todos(project_id, todolist_id)
            groups = await client.get_todolist_groups(project_id, todolist_id)

        # Semaphore released before recursive gather — no deadlock risk
        if groups and depth < MAX_GROUP_DEPTH:
            valid_groups = [g for g in groups if g.get("id")]
            total_groups += len(valid_groups)
            if valid_groups:
                group_results = await asyncio.gather(
                    *[fetch_todolist_todos(g["id"], depth + 1) for g in valid_groups],
                    return_exceptions=True,
                )
                for result in group_results:
                    if isinstance(result, Exception):
                        logger.warning("Failed to fetch group in project %s: %s", project_id, result)
                    else:
                        todos.extend(result)

        return todos

    results = await asyncio.gather(
        *[fetch_todolist_todos(tl["id"]) for tl in todolists if tl.get("id")],
        return_exceptions=True,
    )

    for result in results:
        if isinstance(result, Exception):
            logger.warning("Failed to fetch todolist in project %s: %s", project_id, result)
        else:
            all_todos.extend(result)

    if total_groups:
        logger.info(
            "Project %s: found %d todolist groups containing nested todos",
            project_id,
            total_groups,
        )

    return all_todos


# ---------------------------------------------------------------------------
# Pass 2: Change Detection — check active tasks for updates
# ---------------------------------------------------------------------------


async def change_detection_pass(
    client: AsyncBasecampClient, db: Session, due_bucket_ids: set[str] | None = None,
) -> dict:
    """Check active tasks for changes in Basecamp.

    Fetches todo metadata for each active task, compares updated_at,
    marks completed todos as closed, and flags changed tasks for re-analysis.
    If due_bucket_ids is provided, only checks tasks in those projects.

    Returns dict with counts: {tasks_checked, changed, closed, errors}.
    """
    stats = {"tasks_checked": 0, "changed": 0, "closed": 0, "errors": 0}
    sem = asyncio.Semaphore(CONCURRENCY_LIMIT)
    now = datetime.now(timezone.utc)

    q = db.query(Task).filter(Task.status == "active")
    if due_bucket_ids is not None:
        q = q.filter(Task.basecamp_bucket_id.in_(due_bucket_ids))
    active_tasks = q.all()

    async def check_task(task: Task) -> tuple[Task, dict | None]:
        try:
            async with sem:
                todo = await client.get_todo(
                    task.basecamp_bucket_id, task.basecamp_todo_id,
                )
            return (task, todo)
        except Exception:
            logger.exception(
                "Change detection: error fetching todo %s/%s",
                task.basecamp_bucket_id,
                task.basecamp_todo_id,
            )
            return (task, None)

    results = await asyncio.gather(
        *[check_task(t) for t in active_tasks]
    )

    for task, todo in results:
        stats["tasks_checked"] += 1

        if todo is None:
            stats["errors"] += 1
            continue

        # Check if completed in Basecamp
        if todo.get("completed"):
            task.status = "closed"
            task.closed_at = now
            task.last_checked_at = now
            stats["closed"] += 1
            continue

        # Compare updated_at against stored raw_data to detect any change
        bc_updated = todo.get("updated_at")
        if bc_updated:
            stored_updated = (task.raw_data or {}).get("updated_at")
            if stored_updated is None or bc_updated != stored_updated:
                task.has_new_activity = True
                stats["changed"] += 1

        # Update metadata that may have changed
        task.title = todo.get("title") or todo.get("content") or task.title
        assignees = todo.get("assignees", [])
        task.assignee = assignees[0].get("name") if assignees else None
        task.assignee_names = _extract_names(assignees) or None
        task.completion_subscribers = _extract_names(todo.get("completion_subscribers", [])) or None
        task.comment_count = todo.get("comments_count", task.comment_count)
        if todo.get("due_on"):
            try:
                task.due_date = datetime.strptime(todo["due_on"], "%Y-%m-%d").date()
            except (ValueError, TypeError):
                pass
        task.raw_data = todo
        task.last_checked_at = now

    db.commit()
    logger.info(
        "Change detection: %d checked, %d changed, %d closed, %d errors",
        stats["tasks_checked"],
        stats["changed"],
        stats["closed"],
        stats["errors"],
    )
    return stats


# ---------------------------------------------------------------------------
# Pass 3: AI Analysis — analyze tasks with new activity
# ---------------------------------------------------------------------------


def _is_cache_stale(task: Task) -> bool:
    """Check if AI cache is older than TTL threshold."""
    if not task.last_analyzed_at:
        return True
    now = datetime.now(timezone.utc)
    analyzed = task.last_analyzed_at
    if analyzed.tzinfo is None:
        analyzed = analyzed.replace(tzinfo=timezone.utc)
    return (now - analyzed).days > AI_CACHE_TTL_DAYS


async def analysis_pass(client: AsyncBasecampClient, db: Session) -> dict:
    """Analyze tasks that have new activity using Gemini + scorer.

    Fetches comments, runs AI analysis, computes scores. Processes tasks
    sequentially to respect Gemini rate limits. Commits in batches.

    Returns dict with counts: {tasks_analyzed, skipped, errors, cache_expired}.
    """
    from app.constants import find_project_for_task, get_relevant_names
    from app.services.discovery import normalize_project
    from app.services.gemini import format_comments, get_gemini_client
    from app.services.scorer import score_task

    stats = {"tasks_analyzed": 0, "skipped_ai": 0, "errors": 0, "cache_expired": 0}

    gemini_client = get_gemini_client()
    if not gemini_client:
        logger.warning("Analysis pass: no Gemini API key — will use deterministic-only scoring")

    now = datetime.now(timezone.utc)

    # Include tasks with new activity OR stale AI cache (exclude those exceeding max retries)
    tasks_new_activity = (
        db.query(Task)
        .options(selectinload(Task.account))
        .filter(
            Task.has_new_activity.is_(True),
            Task.status == "active",
            Task.analysis_error_count < MAX_ANALYSIS_RETRIES,
        )
        .all()
    )

    # Also find tasks with expired AI cache (even without new activity)
    tasks_stale_cache = (
        db.query(Task)
        .options(selectinload(Task.account))
        .filter(
            Task.status == "active",
            Task.has_new_activity.isnot(True),
            Task.last_analyzed_at.isnot(None),
            Task.last_analyzed_at < now - timedelta(days=AI_CACHE_TTL_DAYS),
        )
        .all()
    )
    if tasks_stale_cache:
        stats["cache_expired"] = len(tasks_stale_cache)
        logger.info("Analysis pass: %d tasks with expired AI cache (>%dd)", len(tasks_stale_cache), AI_CACHE_TTL_DAYS)

    # Auto-retry tasks stuck in "partial" status (failed Gemini but below max retries)
    tasks_partial = (
        db.query(Task)
        .options(selectinload(Task.account))
        .filter(
            Task.status == "active",
            Task.analysis_status == "partial",
            Task.has_new_activity.isnot(True),
            Task.analysis_error_count < MAX_ANALYSIS_RETRIES,
        )
        .all()
    )
    if tasks_partial:
        logger.info("Analysis pass: %d partial tasks queued for retry", len(tasks_partial))

    # Combine and deduplicate
    seen_ids = set()
    tasks = []
    for t in tasks_new_activity + tasks_stale_cache + tasks_partial:
        if t.id not in seen_ids:
            seen_ids.add(t.id)
            tasks.append(t)

    batch_count = 0
    for task in tasks:
        try:
            account = task.account

            # Resolve producer names for this task
            project = find_project_for_task(account, task.basecamp_bucket_id)
            if project:
                project = normalize_project(project)
            producer_names = get_relevant_names(project)

            # Skip unassigned tasks entirely — hidden from all views
            if not task.assignee:
                task.has_new_activity = False
                stats["skipped_ai"] += 1
                batch_count += 1
                if batch_count >= ANALYSIS_BATCH_SIZE:
                    db.commit()
                    batch_count = 0
                continue

            if not gemini_client:
                # No Gemini — deterministic-only scoring, preserve cached AI signals
                score_task(task, task.ai_signals, producer_names)
                task.has_new_activity = False
                task.analysis_error_count = 0
                task.last_analyzed_at = now
                task.analysis_status = "partial"
                stats["tasks_analyzed"] += 1
                batch_count += 1
                if batch_count >= ANALYSIS_BATCH_SIZE:
                    db.commit()
                    batch_count = 0
                continue

            # Fetch comments from Basecamp
            comments = await client.get_comments(
                task.basecamp_bucket_id, task.basecamp_todo_id,
            )
            task.raw_comments = comments[-MAX_STORED_COMMENTS:] if len(comments) > MAX_STORED_COMMENTS else comments
            task.comment_count = len(comments)
            _update_activity_from_comments(task, comments)

            # Build task_data for Gemini prompt
            task_data = _build_task_data(task, account, comments, format_comments)

            # Compute hash of formatted comments to detect actual content changes
            comments_hash = hashlib.sha256(
                task_data["formatted_comments"].encode()
            ).hexdigest()

            # Force re-analysis if AI cache is stale, even if hash matches
            force_reanalysis = _is_cache_stale(task)

            if not force_reanalysis and comments_hash == task.last_analyzed_comments_hash and task.ai_signals:
                # Comments unchanged and cache fresh — skip Gemini, re-score with cached AI signals
                score_task(task, task.ai_signals, producer_names)
                task.has_new_activity = False
                task.analysis_error_count = 0
                task.last_analyzed_at = now
                task.analysis_status = "complete"
                stats["skipped_ai"] += 1
            else:
                # Comments changed or cache stale — run full Gemini analysis
                ai_signals, ai_success = await asyncio.to_thread(
                    gemini_client.analyze_task, task_data, producer_names,
                )

                score_task(task, ai_signals, producer_names)
                task.last_analyzed_comments_hash = comments_hash
                task.has_new_activity = False
                task.last_analyzed_at = now

                if ai_success:
                    task.analysis_error_count = 0
                    task.analysis_status = "complete"
                else:
                    # Gemini failed — scored with deterministic signals only
                    task.analysis_error_count = (task.analysis_error_count or 0) + 1
                    task.analysis_status = "partial"
                    stats["errors"] += 1

                stats["tasks_analyzed"] += 1

        except Exception:
            logger.exception(
                "Analysis pass: error analyzing task %s/%s",
                task.basecamp_bucket_id,
                task.basecamp_todo_id,
            )
            stats["errors"] += 1
            task.analysis_error_count = (task.analysis_error_count or 0) + 1
            task.has_new_activity = False  # Always clear to prevent infinite loop
            if task.analysis_error_count >= MAX_ANALYSIS_RETRIES:
                task.analysis_status = "error"
                logger.error(
                    "Task %s exceeded max retries (%d), pausing analysis",
                    task.id, MAX_ANALYSIS_RETRIES,
                )
            else:
                task.analysis_status = "partial"
                # Fallback: at least assign deterministic score
                try:
                    _fallback_score(task)
                except Exception:
                    logger.exception("Fallback scoring also failed for task %s", task.id)

        batch_count += 1
        if batch_count >= ANALYSIS_BATCH_SIZE:
            db.commit()
            batch_count = 0

    # Final commit for remaining tasks
    db.commit()
    logger.info(
        "Analysis pass: %d analyzed, %d skipped (cached), %d cache expired, %d errors",
        stats["tasks_analyzed"],
        stats["skipped_ai"],
        stats["cache_expired"],
        stats["errors"],
    )
    return stats


def _build_task_data(
    task: Task,
    account: Account | None,
    comments: list[dict],
    format_comments_fn,
) -> dict:
    """Build the task_data dict expected by the Gemini prompt."""
    from app.constants import find_project_for_task
    from app.services.scorer import compute_staleness_days

    project = find_project_for_task(account, task.basecamp_bucket_id)
    project_name = (project or {}).get("basecamp_name", "Unknown")

    # Build full assignee list with fallback for old data
    all_assignees = task.assignee_names or ([task.assignee] if task.assignee else [])
    subscribers = task.completion_subscribers or []

    return {
        "title": task.title,
        "project_name": project_name,
        "project_type": task.project_type,
        "account_name": account.name if account else "Unknown",
        "assignee": task.assignee,
        "all_assignees": all_assignees,
        "completion_subscribers": subscribers,
        "due_date": str(task.due_date) if task.due_date else None,
        "days_since_last_comment": compute_staleness_days(task),
        "comment_count": task.comment_count or len(comments),
        "formatted_comments": format_comments_fn(comments),
    }


# ---------------------------------------------------------------------------
# Pass 4: Closed Task Pass — archive old + check for reopened (daily)
# ---------------------------------------------------------------------------

_DEFAULT_CLOSED_CHECK_INTERVAL_HOURS = 24


def _get_archive_after_days(db: Session) -> int:
    """Read archive threshold from config table, default 90 days (3 months)."""
    from app.models import Config

    row = db.get(Config, "archive_after_months")
    months = int(row.value) if row and row.value is not None else 3
    return months * 30


def _get_closed_check_interval_hours(db: Session) -> int:
    """Read closed-check interval from config table, default 24 hours."""
    from app.models import Config

    row = db.get(Config, "closed_check_interval_hours")
    return int(row.value) if row and row.value is not None else _DEFAULT_CLOSED_CHECK_INTERVAL_HOURS


async def closed_task_pass(client: AsyncBasecampClient, db: Session) -> dict:
    """Daily maintenance for closed tasks.

    - Archives tasks closed for 3+ months.
    - Checks recently closed tasks in Basecamp for reopening.
    - Guarded: only runs if last check was 24+ hours ago.

    Returns dict with counts: {archived, reopened, checked, skipped}.
    """
    from app.models import Config
    from datetime import timedelta

    stats = {"archived": 0, "reopened": 0, "checked": 0, "skipped": False}
    now = datetime.now(timezone.utc)

    # Guard: check if enough time has passed since last run
    last_check_row = db.get(Config, "last_closed_check_at")
    if last_check_row and last_check_row.value:
        try:
            last_check = datetime.fromisoformat(str(last_check_row.value))
            if (now - last_check).total_seconds() < _get_closed_check_interval_hours(db) * 3600:
                logger.info("Closed task pass: skipping, last check was %s", last_check)
                stats["skipped"] = True
                return stats
        except (ValueError, TypeError):
            pass

    # 1. Archive tasks closed for 3+ months
    archive_cutoff = now - timedelta(days=_get_archive_after_days(db))
    old_closed = (
        db.query(Task)
        .filter(
            Task.status == "closed",
            Task.closed_at.isnot(None),
            Task.closed_at < archive_cutoff,
        )
        .all()
    )
    for task in old_closed:
        task.status = "archived"
        stats["archived"] += 1

    # 2. Check recently closed tasks for reopening
    sem = asyncio.Semaphore(CONCURRENCY_LIMIT)
    closed_tasks = (
        db.query(Task)
        .filter(Task.status == "closed")
        .all()
    )

    async def check_reopened(task: Task) -> tuple[Task, dict | None]:
        try:
            async with sem:
                return (task, await client.get_todo(
                    task.basecamp_bucket_id, task.basecamp_todo_id,
                ))
        except Exception:
            logger.exception(
                "Closed task pass: error checking %s/%s",
                task.basecamp_bucket_id, task.basecamp_todo_id,
            )
            return (task, None)

    results = await asyncio.gather(*[check_reopened(t) for t in closed_tasks])

    for task, todo in results:
        stats["checked"] += 1
        if todo is None:
            continue
        if not todo.get("completed"):
            # Reopened in Basecamp
            task.status = "active"
            task.closed_at = None
            task.has_new_activity = True
            stats["reopened"] += 1

    # Update last check timestamp
    _upsert_config(db, "last_closed_check_at", now.isoformat())
    db.commit()

    logger.info(
        "Closed task pass: %d archived, %d reopened, %d checked",
        stats["archived"],
        stats["reopened"],
        stats["checked"],
    )
    return stats


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fallback_score(task: Task) -> None:
    """Assign deterministic-only score as fallback when full analysis fails."""
    from app.constants import find_project_for_task, get_relevant_names
    from app.services.scorer import score_task

    account = task.account
    project = find_project_for_task(account, task.basecamp_bucket_id)
    if project:
        project = normalize_project(project)
    producer_names = get_relevant_names(project)
    score_task(task, task.ai_signals, producer_names)
    task.last_analyzed_at = datetime.now(timezone.utc)


def _update_activity_from_comments(task: Task, comments: list[dict]) -> None:
    """Update last_known_activity_at from the latest comment's created_at.

    This ensures staleness tracks actual comment time, not metadata edits.
    """
    if not comments:
        return
    latest = None
    for c in comments:
        created = c.get("created_at")
        if created:
            try:
                dt = datetime.fromisoformat(created)
                if latest is None or dt > latest:
                    latest = dt
            except (ValueError, TypeError):
                pass
    if latest:
        task.last_known_activity_at = latest


def _upsert_config(db: Session, key: str, value) -> None:
    """Upsert a config row."""
    from app.models import Config

    row = db.get(Config, key)
    if row:
        row.value = value
    else:
        db.add(Config(key=key, value=value))


def _create_new_tasks(
    db: Session,
    account: Account,
    project: dict,
    todos: list[dict],
    basecamp_account_id: str,
) -> int:
    """Create Task records for new todos. Returns count created."""
    created = 0
    bucket_id = str(project["id"])

    # Batch-load existing task keys for this project to avoid N+1 queries
    existing_keys = set(
        db.query(Task.basecamp_todo_id)
        .filter(Task.basecamp_bucket_id == bucket_id)
        .all()
    )
    existing_todo_ids = {row[0] for row in existing_keys}

    for todo in todos:
        raw_id = todo.get("id")
        if not raw_id:
            logger.warning("Skipping todo without id in project %s", bucket_id)
            continue
        todo_id = str(raw_id)

        if todo_id in existing_todo_ids:
            continue

        # Parse assignees and subscribers
        assignees = todo.get("assignees", [])
        assignee_name = assignees[0].get("name") if assignees else None
        all_assignee_names = _extract_names(assignees)
        subscriber_names = _extract_names(todo.get("completion_subscribers", []))

        # Parse due date
        due_date = None
        if todo.get("due_on"):
            try:
                due_date = datetime.strptime(todo["due_on"], "%Y-%m-%d").date()
            except (ValueError, TypeError):
                pass

        # Parse activity timestamp
        last_activity = None
        updated_str = todo.get("updated_at") or todo.get("created_at")
        if updated_str:
            try:
                last_activity = datetime.fromisoformat(updated_str)
            except (ValueError, TypeError):
                pass

        task = Task(
            basecamp_account_id=basecamp_account_id,
            basecamp_bucket_id=bucket_id,
            basecamp_todo_id=todo_id,
            basecamp_url=todo.get("app_url"),
            account_id=account.id,
            project_type=project.get("type"),
            title=todo.get("title") or todo.get("content"),
            assignee=assignee_name,
            assignee_names=all_assignee_names or None,
            completion_subscribers=subscriber_names or None,
            due_date=due_date,
            status="active",
            has_new_activity=True,
            comment_count=todo.get("comments_count", 0),
            last_known_activity_at=last_activity,
            last_checked_at=datetime.now(timezone.utc),
            raw_data=todo,
            analysis_status="pending",
        )
        db.add(task)
        created += 1

    return created
