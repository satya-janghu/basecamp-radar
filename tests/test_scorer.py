"""Unit tests for the hybrid scoring engine (app/services/scorer.py)."""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services import scorer


def make_task(**overrides):
    now = datetime.now(timezone.utc)
    fields = {
        "title": "Publish location pages",
        "project_type": "internal",
        "comment_count": 0,
        "due_date": None,
        "last_known_activity_at": now,
        "added_at": now,
        "assignee": "Jordan K.",
        "assignee_names": ["Jordan K."],
        "completion_subscribers": [],
        "is_starred": False,
        "ai_signals": None,
        "priority_score": None,
        "priority_tier": None,
        "previous_score": None,
        "previous_tier": None,
        "score_breakdown": None,
        "ai_ball_with": None,
        "ai_current_stage": None,
        "ai_recommendation": None,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def days_ago(n):
    return datetime.now(timezone.utc) - timedelta(days=n)


@pytest.mark.parametrize(
    "score,tier",
    [(100, "critical"), (80, "critical"), (79, "high"), (60, "high"), (59, "medium"),
     (35, "medium"), (34, "low"), (15, "low"), (14, "minimal"), (0, "minimal")],
)
def test_assign_tier_boundaries(score, tier):
    assert scorer.assign_tier(score) == tier


@pytest.mark.parametrize(
    "days,points",
    [(0, 0), (3, 0), (4, 25), (5, 31), (14, 85), (44, 85), (45, 0), (59, 0), (60, -50), (120, -50)],
)
def test_staleness_curve(days, points):
    task = make_task(last_known_activity_at=days_ago(days), added_at=days_ago(days))
    breakdown = scorer.compute_deterministic_signals(task)
    assert breakdown["staleness"] == {"points": points, "days": days}


@pytest.mark.parametrize("count,points", [(0, 0), (2, 0), (3, 5), (4, 5), (5, 10), (9, 10), (10, 15), (40, 15)])
def test_comment_activity_tiers(count, points):
    breakdown = scorer.compute_deterministic_signals(make_task(comment_count=count))
    assert breakdown["comment_activity"]["points"] == points


@pytest.mark.parametrize("offset,points", [(-3, 25), (0, 20), (1, 20), (2, 15), (3, 15), (5, 10), (7, 5), (8, 0)])
def test_deadline_points(offset, points):
    today = datetime.now(scorer_tz()).date()
    breakdown = scorer.compute_deterministic_signals(make_task(due_date=today + timedelta(days=offset)))
    assert breakdown["deadline"]["points"] == points


def scorer_tz():
    from app.constants import USER_TIMEZONE
    return USER_TIMEZONE


def test_no_due_date_scores_zero():
    breakdown = scorer.compute_deterministic_signals(make_task(due_date=None))
    assert breakdown["deadline"] == {"points": 0, "due_in_days": None}


def test_zero_engagement_penalty_only_after_30_days():
    stale = make_task(comment_count=0, last_known_activity_at=days_ago(31), added_at=days_ago(31))
    fresh = make_task(comment_count=0, last_known_activity_at=days_ago(30), added_at=days_ago(30))
    assert scorer.compute_deterministic_signals(stale)["zero_engagement"]["points"] == -10
    assert scorer.compute_deterministic_signals(fresh)["zero_engagement"]["points"] == 0


def test_ai_signal_points_mapping():
    signals = {
        "is_blocked": True,
        "blocked_reason": "Waiting on GA4 access",
        "has_direct_mention": True,
        "needs_approval": True,
        "ownership_ambiguous": True,
        "ball_with": "Alex",
        "current_stage": "pending_review",
    }
    points = scorer.compute_ai_signal_points(signals, ["Alex"])
    assert points["blocker"] == {"points": 50, "reason": "Waiting on GA4 access"}
    assert points["mention"]["points"] == 45
    assert points["approval"]["points"] == 35
    assert points["ambiguous"]["points"] == 10
    assert points["ball_with"]["points"] == 20
    assert points["ball_not_with"]["points"] == 0
    assert points["stage"]["points"] == 15


def test_ball_with_someone_else_is_a_deduction():
    points = scorer.compute_ai_signal_points({"ball_with": "Client"}, ["Alex"])
    assert points["ball_with"]["points"] == 0
    assert points["ball_not_with"]["points"] == -15


def test_ball_with_matches_full_name_by_substring():
    points = scorer.compute_ai_signal_points({"ball_with": "Alex Parker"}, ["alex"])
    assert points["ball_with"]["points"] == 20


@pytest.mark.parametrize("stage,points", [("pending_client", -10), ("blocked", 5), ("completed", -20), ("in_progress", 0)])
def test_stage_points(stage, points):
    assert scorer.compute_ai_signal_points({"current_stage": stage}, ["Alex"])["stage"]["points"] == points


def test_ai_points_empty_when_no_signals():
    points = scorer.compute_ai_signal_points(None, None)
    assert sum(p["points"] for p in points.values()) == 0


def test_max_possible_score_matches_signal_ceiling():
    ai = scorer.SIGNAL_CONFIG["signals"]["ai_detected"]
    ceiling = (85 + 25 + 15 + ai["blocker"] + ai["direct_mention"] + ai["approval_request"]
               + ai["ambiguous_ownership"] + ai["ball_with_producer"] + ai["stage_pending_review"])
    assert ceiling == scorer.MAX_POSSIBLE == 300


def _det(points, multiplier=1.0):
    return {
        "staleness": {"points": points},
        "joint_boost": {"points": 0, "multiplier": multiplier},
    }


def test_final_score_normalizes_against_300():
    score, tier, breakdown = scorer.compute_final_score(_det(150), {})
    assert score == 50
    assert tier == "medium"
    assert breakdown["raw_total"] == 150


def test_joint_multiplier_applies_after_sum():
    score, _, breakdown = scorer.compute_final_score(_det(150, multiplier=1.4), {})
    assert breakdown["effective_total"] == 210.0
    assert score == 70


def test_recurring_dampener_applies_before_joint_multiplier():
    score, _, breakdown = scorer.compute_final_score(_det(100, multiplier=1.4), {}, is_recurring=True)
    assert breakdown["recurring_dampener"] == {"active": True, "multiplier": 0.7, "points": -30}
    assert breakdown["effective_total"] == 98.0
    assert score == 33


def test_negative_raw_total_is_clamped_to_zero():
    score, tier, breakdown = scorer.compute_final_score(_det(-50), {})
    assert breakdown["raw_total"] == 0
    assert (score, tier) == (0, "minimal")


def test_score_is_capped_at_100():
    score, tier, _ = scorer.compute_final_score(_det(400, multiplier=1.4), {})
    assert (score, tier) == (100, "critical")


def test_recurring_title_detection():
    assert scorer._is_recurring_task(make_task(title="[Recurring] Monthly report"))
    assert scorer._is_recurring_task(make_task(title="Rank check (recurring)"))
    assert not scorer._is_recurring_task(make_task(title="Recurring revenue audit"))


def test_star_boost_requires_qualifying_condition():
    starred = make_task(is_starred=True)
    assert scorer.compute_star_boost(starred, {}, staleness_days=2)["points"] == 0
    boost = scorer.compute_star_boost(starred, {"is_blocked": True}, staleness_days=2)
    assert boost == {"points": 60, "active": True, "reason": "blocker detected"}


def test_star_boost_staleness_window():
    starred = make_task(is_starred=True)
    assert scorer.compute_star_boost(starred, {}, staleness_days=8)["points"] == 60
    assert scorer.compute_star_boost(starred, {}, staleness_days=45)["points"] == 0


def test_unstarred_task_gets_no_boost():
    assert scorer.compute_star_boost(make_task(), {"is_blocked": True}, 10)["points"] == 0


def test_score_task_amplifies_staleness_for_producer_assignments():
    task = make_task(
        last_known_activity_at=days_ago(10), added_at=days_ago(10),
        assignee="Alex Parker", assignee_names=["Alex Parker"],
    )
    scorer.score_task(task, None, ["Alex"])
    stale = task.score_breakdown["staleness"]
    assert stale["points"] == round(61 * 1.25)
    assert stale["amplified"] is True


def test_score_task_writes_fields_and_tracks_trend():
    task = make_task(priority_score=5, priority_tier="minimal")
    signals = {"is_blocked": True, "ball_with": "Alex", "current_stage": "blocked",
               "recommendation": "Ask the client for GA4 access."}
    scorer.score_task(task, signals, ["Alex"])
    # blocker 50 + ball_with 20 + stage_blocked 5 = 75 raw -> 25
    assert task.priority_score == 25
    assert task.priority_tier == "low"
    assert task.previous_score == 5
    assert task.previous_tier == "minimal"
    assert task.ai_ball_with == "Alex"
    assert task.ai_current_stage == "blocked"
    assert task.ai_recommendation == "Ask the client for GA4 access."


def test_score_task_preserves_cached_signals_when_none_passed():
    cached = {"is_blocked": True, "ball_with": "Client"}
    task = make_task(ai_signals=cached)
    scorer.score_task(task, None, ["Alex"])
    assert task.ai_signals is cached
    assert task.score_breakdown["blocker"]["points"] == 50


def test_display_segments_include_positive_and_negative_signals():
    segments = scorer.build_display_segments({
        "staleness": {"points": 40},
        "ball_not_with": {"points": -15},
        "mention": {"points": 0},
        "raw_total": 25,
    })
    keys = {s["key"]: s for s in segments}
    assert set(keys) == {"staleness", "ball_not_with"}
    assert keys["ball_not_with"]["negative"] is True
    assert keys["staleness"]["label"] == "Staleness"


def test_display_segments_empty_breakdown():
    assert scorer.build_display_segments(None) == []
