"""Unit tests for Gemini prompt construction and response validation (no API calls)."""

from app.services import gemini


def _comment(i, name="Jordan K."):
    return {"creator": {"name": name}, "created_at": f"2026-01-{i:02d}T10:00:00Z", "content": f"<p>Update {i}</p>"}


def test_format_comments_empty():
    assert gemini.format_comments([]) == "No comments on this task."


def test_format_comments_strips_html_and_numbers_entries():
    text = gemini.format_comments([_comment(1), _comment(2, "Sam T.")])
    assert "[1] Jordan K. (2026-01-01T10:00:00Z):\nUpdate 1" in text
    assert "[2] Sam T." in text
    assert "<p>" not in text


def test_format_comments_keeps_first_and_last_fifteen():
    comments = [_comment(i) for i in range(1, 21)]
    text = gemini.format_comments(comments)
    assert "[1] " in text
    assert "... 4 earlier comments omitted ..." in text
    assert "[2] " not in text and "[5] " not in text
    assert "[6] " in text and "[20] " in text


def test_validate_response_fills_defaults_and_rejects_bad_types():
    result = gemini._validate_response({
        "is_blocked": "yes",
        "has_direct_mention": True,
        "current_stage": "Pending_Review ",
        "urgency_factors": ["Client silent 9 days", "", None],
    })
    assert result["is_blocked"] is False
    assert result["has_direct_mention"] is True
    assert result["current_stage"] == "pending_review"
    assert result["urgency_factors"] == ["Client silent 9 days"]
    assert result["ball_with"] == "unknown"


def test_validate_response_ignores_unknown_stage():
    assert gemini._validate_response({"current_stage": "on_hold"})["current_stage"] == "in_progress"


def test_build_prompt_includes_context_and_team_matches():
    prompt = gemini._build_prompt(
        {
            "title": "Fix crawl errors",
            "project_name": "| Acme Roofing Co. | // Joint // SEO",
            "project_type": "Joint",
            "account_name": "Acme Roofing Co.",
            "all_assignees": ["Alex Parker", "Jordan K."],
            "completion_subscribers": ["Sam T."],
            "formatted_comments": "[1] Jordan K.: done?",
        },
        ["Alex", "Sam"],
    )
    assert "Digital Producer (Alex)" in prompt
    assert "Our Team in Assignees: Alex Parker" in prompt
    assert "Our Team in Subscribers: Sam T." in prompt
    assert "ANY of: Alex, Sam" in prompt
    assert "Client Account: Acme Roofing Co." in prompt


def test_rate_limiter_records_requests():
    limiter = gemini.GeminiRateLimiter(per_minute=5, per_day=10)
    for _ in range(3):
        limiter.wait()
    assert limiter.stats == {"minute_used": 3, "minute_limit": 5, "day_used": 3, "day_limit": 10}


def test_client_is_disabled_without_api_key(monkeypatch):
    monkeypatch.setattr(gemini, "_gemini_client", None)
    monkeypatch.setattr(gemini, "_gemini_client_initialized", False)
    monkeypatch.setattr(gemini.settings, "GEMINI_API_KEY", "")
    assert gemini.get_gemini_client() is None
