"""Background scheduler — runs poll cycles on a configurable interval."""

import logging
from contextlib import asynccontextmanager

from apscheduler import AsyncScheduler, ConflictPolicy
from apscheduler.triggers.interval import IntervalTrigger

from app.database import SessionLocal
from app.services.basecamp import get_async_basecamp_client
from app.services.poller import (
    analysis_pass,
    change_detection_pass,
    closed_task_pass,
    discovery_pass,
)

logger = logging.getLogger(__name__)

MIN_POLL_INTERVAL = 5  # Floor to prevent runaway polling


def _upsert_config(db, key: str, value) -> None:
    """Upsert a config row."""
    from app.models import Config

    row = db.get(Config, key)
    if row:
        row.value = value
    else:
        db.add(Config(key=key, value=str(value) if value is not None else None))


# ---------------------------------------------------------------------------
# Scheduled jobs
# ---------------------------------------------------------------------------


def _compute_due_bucket_ids(db) -> set[str]:
    """Compute set of bucket IDs (project IDs) that are due for polling."""
    from app.models import Account
    from app.services.discovery import normalize_project
    from app.services.poller import _get_global_poll_interval, _is_project_due

    global_interval = _get_global_poll_interval(db)
    due: set[str] = set()

    accounts = db.query(Account).filter(Account.is_archived.is_(False)).all()
    for account in accounts:
        for project in (account.projects or []):
            project = normalize_project(project)
            pid = project.get("id")
            if pid and _is_project_due(project, global_interval):
                due.add(str(pid))

    return due


async def run_poll_cycle() -> None:
    """Run the main poll cycle: discovery -> change detection -> AI analysis.

    Each pass is isolated — a failure in one pass does not abort the cycle.
    Records a PollRun with aggregate stats.
    """
    from datetime import datetime, timezone
    from app.models import Config, PollRun

    logger.info("Poll cycle starting")
    db = SessionLocal()
    start_time = datetime.now(timezone.utc)
    poll_run = PollRun(started_at=start_time)
    aggregate_stats = {
        "discovered": 0, "changed": 0, "analyzed": 0,
        "skipped_ai": 0, "errors": 0,
    }

    try:
        db.add(poll_run)
        db.flush()

        # Mark poll as running (for dashboard status tracking)
        _upsert_config(db, "poll_running", start_time.isoformat())
        db.commit()

        client = get_async_basecamp_client(db)
        if not client:
            logger.warning("Poll cycle: no Basecamp connection, skipping")
            # Clear orphaned has_new_activity flags to prevent dashboard hang
            from app.models import Task
            orphaned = (
                db.query(Task)
                .filter(Task.status == "active", Task.has_new_activity.is_(True))
                .update({Task.has_new_activity: False}, synchronize_session="fetch")
            )
            if orphaned:
                logger.warning("Cleared %d orphaned has_new_activity flags (no Basecamp connection)", orphaned)
                db.commit()
            return

        # Compute which projects are due
        due_bucket_ids = _compute_due_bucket_ids(db)
        logger.info("Poll cycle: %d projects due for polling", len(due_bucket_ids))

        async with client:
            # Pass 1: Discovery
            try:
                stats = await discovery_pass(client, db, due_bucket_ids=due_bucket_ids)
                aggregate_stats["discovered"] = stats.get("tasks_created", 0)
                logger.info("Poll cycle — discovery: %s", stats)
            except Exception:
                logger.exception("Poll cycle: discovery pass failed")
                aggregate_stats["errors"] += 1

            # Pass 2: Change Detection
            try:
                stats = await change_detection_pass(client, db, due_bucket_ids=due_bucket_ids)
                aggregate_stats["changed"] = stats.get("changed", 0)
                logger.info("Poll cycle — change detection: %s", stats)
            except Exception:
                logger.exception("Poll cycle: change detection pass failed")
                aggregate_stats["errors"] += 1

            # Pass 3: AI Analysis
            try:
                stats = await analysis_pass(client, db)
                aggregate_stats["analyzed"] = stats.get("tasks_analyzed", 0)
                aggregate_stats["skipped_ai"] = stats.get("skipped_ai", 0)
                aggregate_stats["errors"] += stats.get("errors", 0)
                logger.info("Poll cycle — analysis: %s", stats)
            except Exception:
                logger.exception("Poll cycle: analysis pass failed")
                aggregate_stats["errors"] += 1

        # Record last poll timestamp
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        row = db.get(Config, "last_poll_at")
        if row:
            row.value = now_iso
        else:
            db.add(Config(key="last_poll_at", value=now_iso))

        # Finalize poll run record
        poll_run.completed_at = now
        poll_run.duration_seconds = round((now - start_time).total_seconds(), 1)
        poll_run.stats = aggregate_stats

        # Prune old runs (keep last 50)
        old_runs = (
            db.query(PollRun)
            .order_by(PollRun.started_at.desc())
            .offset(50)
            .all()
        )
        for old in old_runs:
            db.delete(old)

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        # Always clear poll_running flag
        try:
            _upsert_config(db, "poll_running", None)
            db.commit()
        except Exception:
            logger.exception("Failed to clear poll_running flag")
            db.rollback()
        db.close()

    logger.info("Poll cycle completed")


async def run_closed_task_check() -> None:
    """Run the daily closed task maintenance pass."""
    logger.info("Closed task check starting")
    db = SessionLocal()
    try:
        client = get_async_basecamp_client(db)
        if not client:
            logger.warning("Closed task check: no Basecamp connection, skipping")
            return

        async with client:
            stats = await closed_task_pass(client, db)
            logger.info("Closed task check: %s", stats)
    except Exception:
        db.rollback()
        logger.exception("Closed task check failed")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Scheduler lifecycle
# ---------------------------------------------------------------------------


def _get_poll_interval() -> int:
    """Compute scheduler tick rate: min(global, all project overrides), floor at 5 min."""
    db = SessionLocal()
    try:
        from app.models import Account, Config
        from app.services.discovery import normalize_project

        row = db.get(Config, "poll_interval_minutes")
        global_interval = int(row.value) if row and row.value is not None else 30

        # Check all project-level overrides for a faster interval
        min_interval = global_interval
        accounts = db.query(Account).filter(Account.is_archived.is_(False)).all()
        for account in accounts:
            for project in (account.projects or []):
                project = normalize_project(project)
                pi = project.get("poll_interval_minutes")
                if pi is not None and int(pi) > 0:
                    min_interval = min(min_interval, int(pi))

        return max(min_interval, MIN_POLL_INTERVAL)
    except Exception:
        logger.exception("Failed to read poll interval from config")
        return 30
    finally:
        db.close()


@asynccontextmanager
async def scheduler_lifespan():
    """Context manager that creates, configures, and runs the scheduler.

    Usage in FastAPI lifespan:
        async with scheduler_lifespan() as scheduler:
            yield
    """
    poll_interval = _get_poll_interval()

    async with AsyncScheduler() as scheduler:
        await scheduler.add_schedule(
            run_poll_cycle,
            IntervalTrigger(minutes=poll_interval),
            id="poll_cycle",
            conflict_policy=ConflictPolicy.replace,
        )
        await scheduler.add_schedule(
            run_closed_task_check,
            IntervalTrigger(hours=24),
            id="closed_task_check",
            conflict_policy=ConflictPolicy.replace,
        )
        await scheduler.start_in_background()
        logger.info(
            "Scheduler started: poll every %d min, closed check every 24h",
            poll_interval,
        )
        yield scheduler
