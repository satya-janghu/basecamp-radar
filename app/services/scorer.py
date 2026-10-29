"""Deterministic scoring engine + AI signal point mapping + modifiers.

Scoring model (v4 — downvote pattern analysis):
  - Deterministic signals: continuous staleness, graduated deadline/activity,
    zero-engagement penalty (-10 when 0 comments + >30d stale)
  - AI signals: blocker, mention, approval, ambiguous, ball_with, ball_not_with,
    stage (incl. completed = -20)
  - Modifiers: star boost (+60 raw)
  - Staleness amplifier: 1.25x when task assignee matches a producer name
  - Recurring task dampener: 0.7x on raw_total before joint multiplier
  - Joint project: 1.4x multiplier (amplifier, not flat bonus)
  - Pipeline: raw_total -> clamp(0) -> recurring(0.7x) -> joint(1.4x) -> normalize
  - Max raw = 300 (base signals), normalized to 0-100
  - 5 Tiers: Critical >= 80 | High 60-79 | Medium 35-59 | Low 15-34 | Minimal 0-14
"""

import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Signal configuration
# ---------------------------------------------------------------------------

SIGNAL_CONFIG = {
    "signals": {
        "deterministic": {
            "staleness": "continuous: min(85, 25 + (days-4)*6) for 4-44d; 0 for 45-59d (decay); -50 for 60d+ (demerit); 1.25x when assigned to producer",
            "comment_activity": [
                {"min_comments": 3, "max_comments": 4, "points": 5},
                {"min_comments": 5, "max_comments": 9, "points": 10},
                {"min_comments": 10, "max_comments": None, "points": 15},
            ],
            "deadline": [
                {"condition": "overdue", "points": 25},
                {"condition": "0-1 days", "points": 20},
                {"condition": "2-3 days", "points": 15},
                {"condition": "4-5 days", "points": 10},
                {"condition": "6-7 days", "points": 5},
            ],
        },
        "ai_detected": {
            "blocker": 50,
            "direct_mention": 45,
            "approval_request": 35,
            "ambiguous_ownership": 10,
            "ball_with_producer": 20,
            "ball_not_with_producer": -15,
            "stage_pending_review": 15,
            "stage_pending_client": -10,
            "stage_blocked": 5,
            "stage_completed": -20,
        },
    },
    "joint_multiplier": 1.4,
    # Max raw = staleness(85) + deadline(25) + activity(15)
    #         + blocker(50) + mention(45) + approval(35) + ambiguous(10)
    #         + ball_with(20) + stage_review(15)
    #         = 300 base
    # Star boost (60) is a modifier excluded from MAX_POSSIBLE — it can
    # push above the base range. Cap at 100 handles overflow.
    "max_possible_score": 300,
    "tiers": {"critical": 80, "high": 60, "medium": 35, "low": 15, "minimal": 0},
}

STAR_BOOST_POINTS = 60
ASSIGNED_STALENESS_MULTIPLIER = 1.25
STALENESS_DECAY_THRESHOLD = 45
STALENESS_DEMERIT_THRESHOLD = 60
STALENESS_DEMERIT_POINTS = -50
RECURRING_DAMPENER = 0.7
RECURRING_PATTERN = re.compile(r"[\[\(]recurring[\]\)]", re.IGNORECASE)
ZERO_ENGAGEMENT_PENALTY = -10
ZERO_ENGAGEMENT_STALENESS_THRESHOLD = 30
JOINT_MULTIPLIER = SIGNAL_CONFIG["joint_multiplier"]
MAX_POSSIBLE = SIGNAL_CONFIG["max_possible_score"]


def assign_tier(score: int) -> str:
    """Map a 0-100 score to a priority tier (critical/high/medium/low/minimal)."""
    tiers = SIGNAL_CONFIG["tiers"]
    if score >= tiers["critical"]:
        return "critical"
    elif score >= tiers["high"]:
        return "high"
    elif score >= tiers["medium"]:
        return "medium"
    elif score >= tiers["low"]:
        return "low"
    return "minimal"


# ---------------------------------------------------------------------------
# Deterministic signals
# ---------------------------------------------------------------------------


def compute_deterministic_signals(task) -> dict:
    """Compute deterministic signal breakdown from task metadata.

    Returns dict with per-signal breakdown:
        {staleness: {points, days}, joint_boost: {points, is_joint, multiplier},
         comment_activity: {points, count}, deadline: {points, due_in_days}}
    """
    breakdown = {}

    # --- Staleness (continuous formula) ---
    staleness_days = compute_staleness_days(task)
    if staleness_days >= STALENESS_DEMERIT_THRESHOLD:
        staleness_points = STALENESS_DEMERIT_POINTS
    elif staleness_days >= STALENESS_DECAY_THRESHOLD:
        staleness_points = 0
    elif staleness_days >= 4:
        staleness_points = min(85, 25 + (staleness_days - 4) * 6)
    else:
        staleness_points = 0
    breakdown["staleness"] = {"points": staleness_points, "days": staleness_days}

    # --- Joint project (recorded as metadata; applied as multiplier in compute_final_score) ---
    is_joint = (task.project_type or "").lower() == "joint"
    breakdown["joint_boost"] = {
        "points": 0,  # No additive points; multiplier is applied in compute_final_score
        "is_joint": is_joint,
        "multiplier": JOINT_MULTIPLIER if is_joint else 1.0,
    }

    # --- Comment activity (graduated) ---
    count = task.comment_count or 0
    if count >= 10:
        activity_points = 15
    elif count >= 5:
        activity_points = 10
    elif count >= 3:
        activity_points = 5
    else:
        activity_points = 0
    breakdown["comment_activity"] = {"points": activity_points, "count": count}

    # --- Deadline (graduated; overdue scores highest) ---
    due_in_days = _compute_due_in_days(task)
    deadline_points = 0
    if due_in_days is not None:
        if due_in_days < 0:
            deadline_points = 25  # Overdue
        elif due_in_days <= 1:
            deadline_points = 20
        elif due_in_days <= 3:
            deadline_points = 15
        elif due_in_days <= 5:
            deadline_points = 10
        elif due_in_days <= 7:
            deadline_points = 5
    breakdown["deadline"] = {"points": deadline_points, "due_in_days": due_in_days}

    # --- Zero-engagement penalty (-10 when 0 comments AND >30d stale) ---
    zero_engagement_points = 0
    if (task.comment_count or 0) == 0 and staleness_days > ZERO_ENGAGEMENT_STALENESS_THRESHOLD:
        zero_engagement_points = ZERO_ENGAGEMENT_PENALTY
    breakdown["zero_engagement"] = {"points": zero_engagement_points, "days": staleness_days}

    return breakdown


def compute_staleness_days(task) -> int:
    """Days since last activity: max(last_known_activity_at, added_at)."""
    now = datetime.now(timezone.utc)
    reference = task.last_known_activity_at or task.added_at
    if reference is None:
        return 0
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    delta = now - reference
    return max(0, delta.days)


def _compute_due_in_days(task) -> int | None:
    """Days until due_date. Returns None if no due date.

    Uses the configured USER_TIMEZONE (server runs in UTC).
    """
    if not task.due_date:
        return None
    from app.constants import USER_TIMEZONE
    today = datetime.now(USER_TIMEZONE).date()
    return (task.due_date - today).days


def _is_recurring_task(task) -> bool:
    """Check if task title contains [Recurring] or (Recurring) (case-insensitive)."""
    title = getattr(task, "title", "") or ""
    return bool(RECURRING_PATTERN.search(title))


# ---------------------------------------------------------------------------
# AI signals
# ---------------------------------------------------------------------------


def compute_ai_signal_points(ai_signals: dict | None, producer_names: list[str] | None = None) -> dict:
    """Map AI signals to point values, including ball_with and current_stage.

    Returns dict with per-signal breakdown:
        {blocker: {points, reason}, mention: {points},
         approval: {points}, ambiguous: {points},
         ball_with: {points, value}, stage: {points, value}}
    """
    ai_config = SIGNAL_CONFIG["signals"]["ai_detected"]
    signals = ai_signals or {}

    blocker_points = ai_config["blocker"] if signals.get("is_blocked") else 0
    mention_points = ai_config["direct_mention"] if signals.get("has_direct_mention") else 0
    approval_points = ai_config["approval_request"] if signals.get("needs_approval") else 0
    ambiguous_points = ai_config["ambiguous_ownership"] if signals.get("ownership_ambiguous") else 0

    # ball_with scoring: +20 when ball is with a producer (the team needs to act)
    # -15 when ball is with someone else (not producer's responsibility)
    # Use substring match: Gemini may return a first name or a full name
    ball_with = signals.get("ball_with", "")
    ball_with_points = 0
    ball_not_with_points = 0
    if ball_with and producer_names:
        ball_lower = ball_with.lower().strip()
        if any(name.lower().strip() in ball_lower for name in producer_names):
            ball_with_points = ai_config["ball_with_producer"]
        else:
            ball_not_with_points = ai_config["ball_not_with_producer"]

    # current_stage scoring
    stage = signals.get("current_stage", "")
    stage_points = 0
    if stage:
        stage_lower = stage.lower().strip()
        if stage_lower == "pending_review":
            stage_points = ai_config["stage_pending_review"]
        elif stage_lower == "pending_client":
            stage_points = ai_config["stage_pending_client"]
        elif stage_lower == "blocked":
            stage_points = ai_config["stage_blocked"]
        elif stage_lower == "completed":
            stage_points = ai_config["stage_completed"]

    return {
        "blocker": {"points": blocker_points, "reason": signals.get("blocked_reason")},
        "mention": {"points": mention_points},
        "approval": {"points": approval_points},
        "ambiguous": {"points": ambiguous_points},
        "ball_with": {"points": ball_with_points, "value": ball_with or None},
        "ball_not_with": {"points": ball_not_with_points, "value": ball_with or None},
        "stage": {"points": stage_points, "value": stage or None},
    }


# ---------------------------------------------------------------------------
# Final score computation
# ---------------------------------------------------------------------------


def compute_final_score(
    det_signals: dict,
    ai_points: dict,
    modifiers: dict | None = None,
    is_recurring: bool = False,
) -> tuple[int, str, dict]:
    """Combine deterministic + AI signals + modifiers into final score.

    Pipeline: raw_total -> clamp(0) -> recurring dampener (0.7x) -> joint multiplier (1.4x) -> normalize
    Normalization uses MAX_POSSIBLE (non-joint ceiling) as denominator.
    Star boost is in the raw sum via modifiers.

    Returns (score, tier, full_breakdown).
    """
    # Sum all additive signal points (joint_boost.points is always 0)
    raw_total = sum(s["points"] for s in det_signals.values())
    raw_total += sum(s["points"] for s in ai_points.values())
    if modifiers:
        raw_total += sum(s["points"] for s in modifiers.values())

    # Clamp negative so raw doesn't go below 0
    raw_total = max(0, raw_total)

    # Apply recurring dampener before joint multiplier
    recurring_multiplier = RECURRING_DAMPENER if is_recurring else 1.0
    raw_total_dampened = round(raw_total * recurring_multiplier)

    # Apply joint multiplier
    joint_info = det_signals.get("joint_boost", {})
    multiplier = joint_info.get("multiplier", 1.0)
    effective_total = raw_total_dampened * multiplier

    normalized = round((effective_total / MAX_POSSIBLE) * 100) if MAX_POSSIBLE > 0 else 0
    normalized = min(normalized, 100)

    tier = assign_tier(normalized)

    breakdown = {**det_signals, **ai_points, **(modifiers or {})}
    breakdown["raw_total"] = raw_total
    breakdown["recurring_dampener"] = {
        "active": is_recurring,
        "multiplier": recurring_multiplier,
        "points": raw_total_dampened - raw_total,
    }
    breakdown["joint_multiplier"] = multiplier
    breakdown["effective_total"] = round(effective_total, 1)
    breakdown["normalized_score"] = normalized
    breakdown["tier"] = tier

    return normalized, tier, breakdown


# ---------------------------------------------------------------------------
# Star boost
# ---------------------------------------------------------------------------


def compute_star_boost(task, ai_signals: dict | None, staleness_days: int) -> dict:
    """Compute conditional star boost.

    Returns +60 raw points when task.is_starred AND any qualifying condition:
    - AI: is_blocked, has_direct_mention, needs_approval, ownership_ambiguous
    - AI: current_stage == "pending_review"
    - Deterministic: staleness > 7 days

    Returns dict: {points, active, reason}
    """
    if not getattr(task, "is_starred", False):
        return {"points": 0, "active": False, "reason": None}

    signals = ai_signals or {}
    reasons = []

    if signals.get("is_blocked"):
        reasons.append("blocker detected")
    if signals.get("has_direct_mention"):
        reasons.append("direct mention")
    if signals.get("needs_approval"):
        reasons.append("approval needed")
    if signals.get("ownership_ambiguous"):
        reasons.append("ambiguous ownership")
    if signals.get("current_stage") == "pending_review":
        reasons.append("pending review")
    if 7 < staleness_days < STALENESS_DECAY_THRESHOLD:
        reasons.append(f"stale {staleness_days}d")

    if reasons:
        return {
            "points": STAR_BOOST_POINTS,
            "active": True,
            "reason": ", ".join(reasons),
        }

    return {"points": 0, "active": False, "reason": "starred but no qualifying condition"}


# ---------------------------------------------------------------------------
# Top-level scorer
# ---------------------------------------------------------------------------


def score_task(task, ai_signals: dict | None, producer_names: list[str] | None = None) -> None:
    """Score a task and mutate its ORM fields in place.

    Args:
        task: Task ORM instance (will be mutated).
        ai_signals: Full AI response dict from Gemini, cached signals, or None
            for new tasks. When None, existing ai_signals on the task are
            preserved and used for AI point computation.
        producer_names: List of producer names for ball_with matching.
            If None, ball_with scoring is skipped.
    """
    effective_ai = ai_signals if ai_signals is not None else task.ai_signals

    det_signals = compute_deterministic_signals(task)
    ai_points = compute_ai_signal_points(effective_ai, producer_names)

    # Amplify staleness for tasks where a producer is an assignee or subscriber.
    # Checks all assignees + completion subscribers (not just the primary assignee).
    # Basecamp stores full names (e.g. "Alex Parker") while producer_names
    # may contain first names only (e.g. "Alex"), so use substring match.
    if producer_names:
        all_people = list(getattr(task, "assignee_names", None) or ([task.assignee] if task.assignee else []))
        all_people.extend(getattr(task, "completion_subscribers", None) or [])
        if all_people and any(
            name.lower().strip() in person.lower().strip()
            for person in all_people
            for name in producer_names
        ):
            stale = det_signals["staleness"]
            if stale["points"] > 0:
                stale["points"] = round(stale["points"] * ASSIGNED_STALENESS_MULTIPLIER)
                stale["amplified"] = True

    # Modifiers — same raw-point currency as all other signals
    staleness_days = det_signals["staleness"]["days"]
    star_boost = compute_star_boost(task, effective_ai, staleness_days)

    modifiers = {"star_boost": star_boost}
    is_recurring = _is_recurring_task(task)
    score, tier, breakdown = compute_final_score(
        det_signals, ai_points, modifiers, is_recurring=is_recurring
    )

    # Only update trend when score actually changes
    if task.priority_score is not None and score != task.priority_score:
        task.previous_score = task.priority_score
        task.previous_tier = task.priority_tier

    task.priority_score = score
    task.priority_tier = tier
    task.score_breakdown = breakdown

    # Only overwrite AI fields when new signals provided
    if ai_signals is not None:
        task.ai_signals = ai_signals
        task.ai_ball_with = ai_signals.get("ball_with")
        task.ai_current_stage = ai_signals.get("current_stage")
        task.ai_recommendation = ai_signals.get("recommendation")


# ---------------------------------------------------------------------------
# Score bar display helpers
# ---------------------------------------------------------------------------

# Mapping from breakdown key -> (bar color, dot color)
SIGNAL_COLORS = {
    "staleness": ("bg-indigo-400", "bg-indigo-300"),
    "joint_boost": ("bg-sky-500", "bg-sky-400"),
    "blocker": ("bg-rose-500", "bg-rose-400"),
    "mention": ("bg-amber-400", "bg-amber-300"),
    "approval": ("bg-amber-600", "bg-amber-500"),
    "comment_activity": ("bg-teal-400", "bg-teal-300"),
    "deadline": ("bg-rose-300", "bg-rose-200"),
    "ambiguous": ("bg-slate-400", "bg-slate-400"),
    "star_boost": ("bg-amber-300", "bg-amber-200"),
    "ball_with": ("bg-violet-400", "bg-violet-300"),
    "ball_not_with": ("bg-violet-600", "bg-violet-500"),
    "stage": ("bg-cyan-400", "bg-cyan-300"),
    "zero_engagement": ("bg-slate-500", "bg-slate-400"),
    "recurring_dampener": ("bg-orange-500", "bg-orange-400"),
}


def build_display_segments(breakdown: dict | None) -> list[dict]:
    """Build a generic list of score bar segments from a score breakdown.

    Each segment: {key, label, points, bar_color, dot_color}.
    Only includes signals with positive points.
    """
    if not breakdown:
        return []

    segments = []
    for key, (bar_color, dot_color) in SIGNAL_COLORS.items():
        info = breakdown.get(key)
        if not isinstance(info, dict):
            continue
        points = info.get("points", 0)
        if points > 0:
            segments.append({
                "key": key,
                "label": key.replace("_", " ").title(),
                "points": points,
                "bar_color": bar_color,
                "dot_color": dot_color,
            })
        elif points < 0:
            segments.append({
                "key": key,
                "label": key.replace("_", " ").title(),
                "points": points,
                "bar_color": bar_color,
                "dot_color": dot_color,
                "negative": True,
            })
    return segments
