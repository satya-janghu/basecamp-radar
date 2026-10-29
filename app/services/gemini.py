"""Gemini AI client for content signal detection."""

import json
import logging
import re
import threading
import time
from collections import deque

from google import genai
from google.genai import types

from app.config import settings

logger = logging.getLogger(__name__)

# Safe fallback when AI analysis fails — all signals negative
DEFAULT_SIGNALS: dict = {
    "is_blocked": False,
    "blocked_reason": None,
    "has_direct_mention": False,
    "needs_approval": False,
    "ownership_ambiguous": False,
    "ball_with": "unknown",
    "current_stage": "in_progress",
    "recommendation": "AI analysis unavailable — review manually.",
    "urgency_factors": [],
}

VALID_STAGES = {
    "new", "in_progress", "pending_review", "pending_client", "blocked", "completed"
}

# JSON schema for structured output
_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "is_blocked": {"type": "BOOLEAN"},
        "blocked_reason": {"type": "STRING", "nullable": True},
        "has_direct_mention": {"type": "BOOLEAN"},
        "needs_approval": {"type": "BOOLEAN"},
        "ownership_ambiguous": {"type": "BOOLEAN"},
        "ball_with": {"type": "STRING"},
        "current_stage": {"type": "STRING"},
        "recommendation": {"type": "STRING"},
        "urgency_factors": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": [
        "is_blocked", "has_direct_mention", "needs_approval",
        "ownership_ambiguous", "ball_with", "current_stage",
        "recommendation", "urgency_factors",
    ],
}


def format_comments(comments: list[dict], max_recent: int = 15) -> str:
    """Format comment thread for the prompt.

    Includes the first comment + last `max_recent` comments.
    If there are more than max_recent+1, inserts an omission notice.
    Strips HTML tags from comment content.
    """
    if not comments:
        return "No comments on this task."

    total = len(comments)
    lines: list[str] = []

    if total <= max_recent + 1:
        # Show all comments
        for i, c in enumerate(comments, 1):
            lines.append(_format_single_comment(c, i))
    else:
        # First comment
        lines.append(_format_single_comment(comments[0], 1))
        omitted = total - max_recent - 1
        lines.append(f"\n... {omitted} earlier comments omitted ...\n")
        # Last max_recent comments
        for i, c in enumerate(comments[-max_recent:], total - max_recent + 1):
            lines.append(_format_single_comment(c, i))

    return "\n".join(lines)


def _format_single_comment(comment: dict, index: int) -> str:
    """Format a single comment, stripping HTML."""
    creator = comment.get("creator", {}).get("name", "Unknown")
    created = comment.get("created_at", "unknown date")
    content = _strip_html(comment.get("content", ""))
    return f"[{index}] {creator} ({created}):\n{content}"


def _strip_html(html: str) -> str:
    """Remove HTML tags from Basecamp comment content."""
    if not html:
        return ""
    clean = re.sub(r"<[^>]+>", "", html)
    # Collapse multiple whitespace but preserve newlines
    clean = re.sub(r"[ \t]+", " ", clean)
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    return clean.strip()


def _build_prompt(task_data: dict, producer_names: list[str]) -> str:
    """Build the analysis prompt. Self-contained — no external brain context."""
    primary = producer_names[0] if producer_names else "Producer"
    names_str = ", ".join(producer_names) if producer_names else "Producer"

    # Build enriched people context from multi-assignee + subscriber data
    all_assignees = task_data.get("all_assignees") or []
    if not all_assignees and task_data.get("assignee"):
        all_assignees = [task_data["assignee"]]
    assignees_str = ", ".join(all_assignees) if all_assignees else "Unassigned"

    subscribers = task_data.get("completion_subscribers") or []
    subscribers_str = ", ".join(subscribers) if subscribers else "None"

    # Identify which of our team members appear in assignees/subscribers
    producer_set = {n.lower().strip() for n in producer_names}
    team_in_assignees = [
        a for a in all_assignees
        if any(p in a.lower().strip() for p in producer_set)
    ]
    team_in_subscribers = [
        s for s in subscribers
        if any(p in s.lower().strip() for p in producer_set)
    ]
    team_assignees_str = ", ".join(team_in_assignees) if team_in_assignees else "None"
    team_subscribers_str = ", ".join(team_in_subscribers) if team_in_subscribers else "None"

    return f"""You are analyzing a Basecamp task thread for a Digital Producer ({primary}) \
managing client campaigns at a digital marketing agency.

== CONTEXT ==
Task: {task_data.get("title", "Untitled")}
Project: {task_data.get("project_name", "Unknown")} ({task_data.get("project_type", "Unknown")})
Client Account: {task_data.get("account_name", "Unknown")}
Assignee(s): {assignees_str}
Completion Subscribers (notified when done): {subscribers_str}
Our Team in Assignees: {team_assignees_str}
Our Team in Subscribers: {team_subscribers_str}
Due Date: {task_data.get("due_date") or "none"}
Days Since Last Comment: {task_data.get("days_since_last_comment", "unknown")}
Total Comments: {task_data.get("comment_count", 0)}

== COMMENT THREAD ==
{task_data.get("formatted_comments", "No comments.")}

== PROJECT TYPE SIGNIFICANCE ==
- "Joint" projects are shared with the client — tasks are client-facing, higher urgency, and visible to client stakeholders.
- "Internal" projects are agency-only — tasks are internal coordination and execution, not visible to the client.

== INSTRUCTIONS ==
Analyze this task thread and determine:
- Whether the task is blocked, who should act next, and what stage it's in
- Whether any of these people are directly mentioned or addressed: {names_str}
- Whether someone is requesting review/approval from the producer
- Whether ownership of the next action is unclear
- Consider both assignees and completion subscribers when determining ball_with. Subscribers are stakeholders who are notified on completion, not doers.

For ball_with, return ONLY the first name (e.g. "Alex" not "Alex Parker").

Return ONLY a JSON object with these fields:

{{
  "is_blocked": <boolean — is this task stuck waiting on something?>,
  "blocked_reason": <string or null — what is blocking it?>,
  "has_direct_mention": <boolean — does the thread mention or address ANY of: {names_str}?>,
  "needs_approval": <boolean — is someone requesting review, approval, or sign-off from the DP?>,
  "ownership_ambiguous": <boolean — is it unclear who should act next?>,
  "ball_with": <string — first name only of who should act next>,
  "current_stage": <string — one of: new, in_progress, pending_review, pending_client, blocked, completed>,
  "recommendation": <string — specific, actionable next step for the DP>,
  "urgency_factors": [<string — factors that make this more or less urgent>]
}}"""


def _validate_response(result: dict) -> dict:
    """Ensure all expected fields exist with correct types.

    Returns a cleaned dict, filling defaults for missing/invalid fields.
    """
    validated = dict(DEFAULT_SIGNALS)

    if isinstance(result.get("is_blocked"), bool):
        validated["is_blocked"] = result["is_blocked"]
    if isinstance(result.get("blocked_reason"), str):
        validated["blocked_reason"] = result["blocked_reason"]
    elif not result.get("is_blocked"):
        validated["blocked_reason"] = None
    if isinstance(result.get("has_direct_mention"), bool):
        validated["has_direct_mention"] = result["has_direct_mention"]
    if isinstance(result.get("needs_approval"), bool):
        validated["needs_approval"] = result["needs_approval"]
    if isinstance(result.get("ownership_ambiguous"), bool):
        validated["ownership_ambiguous"] = result["ownership_ambiguous"]
    if isinstance(result.get("ball_with"), str):
        validated["ball_with"] = result["ball_with"]
    if isinstance(result.get("current_stage"), str):
        stage = result["current_stage"].lower().strip()
        if stage in VALID_STAGES:
            validated["current_stage"] = stage
    if isinstance(result.get("recommendation"), str):
        validated["recommendation"] = result["recommendation"]
    if isinstance(result.get("urgency_factors"), list):
        validated["urgency_factors"] = [
            str(f) for f in result["urgency_factors"] if f
        ]

    return validated


class GeminiRateLimiter:
    """Thread-safe sliding window rate limiter for Gemini API."""

    def __init__(self, per_minute: int = 1000, per_day: int = 10000):
        self._per_minute = per_minute
        self._per_day = per_day
        self._minute_window: deque[float] = deque()
        self._day_window: deque[float] = deque()
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        """Remove expired entries from both windows. Must hold lock."""
        minute_cutoff = now - 60.0
        while self._minute_window and self._minute_window[0] < minute_cutoff:
            self._minute_window.popleft()
        day_cutoff = now - 86400.0
        while self._day_window and self._day_window[0] < day_cutoff:
            self._day_window.popleft()

    def wait(self) -> None:
        """Block until a slot is available, then record the request."""
        while True:
            with self._lock:
                now = time.monotonic()
                self._prune(now)

                # Check daily limit first (longer wait)
                if len(self._day_window) >= self._per_day:
                    sleep_until = self._day_window[0] + 86400.0
                    sleep_secs = max(0.0, sleep_until - now)
                    logger.warning(
                        "Gemini daily limit (%d) reached — sleeping %.1fs",
                        self._per_day, sleep_secs,
                    )
                else:
                    sleep_secs = 0.0

                if sleep_secs == 0.0 and len(self._minute_window) >= self._per_minute:
                    sleep_until = self._minute_window[0] + 60.0
                    sleep_secs = max(0.0, sleep_until - now)
                    logger.info(
                        "Gemini per-minute limit (%d) reached — sleeping %.1fs",
                        self._per_minute, sleep_secs,
                    )

                if sleep_secs == 0.0:
                    # Slot available — record and return
                    self._minute_window.append(now)
                    self._day_window.append(now)
                    return

            # Sleep outside the lock
            time.sleep(sleep_secs)

    @property
    def stats(self) -> dict:
        """Current usage for observability."""
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            return {
                "minute_used": len(self._minute_window),
                "minute_limit": self._per_minute,
                "day_used": len(self._day_window),
                "day_limit": self._per_day,
            }


# Module-level singleton — persists across get_gemini_client() calls
_gemini_rate_limiter = GeminiRateLimiter(per_minute=1000, per_day=10000)


def get_gemini_rate_stats() -> dict:
    """Return current Gemini rate limit usage."""
    return _gemini_rate_limiter.stats


DEFAULT_MODEL = "gemini-3-flash-preview"


class GeminiClient:
    """Client for Gemini AI content analysis."""

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL):
        self._client = genai.Client(api_key=api_key)
        self._model = model

    def analyze_task(
        self, task_data: dict, producer_names: list[str]
    ) -> tuple[dict, bool]:
        """Analyze a task and return (signals, success).

        Never raises. Returns (DEFAULT_SIGNALS, False) on any failure so the
        caller can distinguish "AI found nothing" from "AI failed."
        """
        try:
            _gemini_rate_limiter.wait()
            prompt = _build_prompt(task_data, producer_names)
            response = self._client.models.generate_content(
                model=self._model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=_RESPONSE_SCHEMA,
                    temperature=0.1,
                ),
            )

            if not response.text:
                logger.warning("Gemini returned empty response")
                return dict(DEFAULT_SIGNALS), False

            result = json.loads(response.text)
            return _validate_response(result), True

        except Exception:
            logger.exception("Gemini analysis failed")
            return dict(DEFAULT_SIGNALS), False


# Module-level singleton — reuses HTTP connection pool across all calls
_gemini_client: GeminiClient | None = None
_gemini_client_initialized = False


def get_gemini_client() -> GeminiClient | None:
    """Get a cached GeminiClient singleton if API key is configured, else None."""
    global _gemini_client, _gemini_client_initialized
    if not _gemini_client_initialized:
        _gemini_client_initialized = True
        if settings.GEMINI_API_KEY:
            _gemini_client = GeminiClient(api_key=settings.GEMINI_API_KEY)
            logger.info("GeminiClient initialized (singleton)")
    return _gemini_client
