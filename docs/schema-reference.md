# Basecamp Radar: Schema Reference

Column-level schema, JSONB examples and the Gemini prompt template.
For architecture, the scoring model, features and endpoints, see [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Database Schema

### accounts

| Column | Type | Description |
|--------|------|-------------|
| id | UUID | Primary key |
| name | VARCHAR | Display name (e.g., "Acme Roofing Co.") |
| projects | JSONB | Array of project objects (see below) |
| is_archived | BOOLEAN | Hide from active views without deleting |
| created_at | TIMESTAMP(tz) | |
| updated_at | TIMESTAMP(tz) | |

**Projects JSONB structure:**

```json
[
  {
    "id": "12345678",
    "type": "joint",
    "service_type": "SEO & PPC",
    "basecamp_name": "| Acme Roofing Co. | // Joint // SEO & PPC",
    "producers": ["Jordan K.", "Sam T."],
    "poll_interval_minutes": null,
    "last_polled_at": "2026-02-08T12:00:00Z"
  },
  {
    "id": "23456789",
    "type": "internal",
    "service_type": "SEO & PPC",
    "basecamp_name": "| Acme Roofing Co. | // Internal // SEO & PPC",
    "producers": [],
    "poll_interval_minutes": 60,
    "last_polled_at": null
  }
]
```

### tasks

| Column | Type | Description |
|--------|------|-------------|
| id | UUID | Primary key |
| basecamp_account_id | VARCHAR | Basecamp account ID |
| basecamp_bucket_id | VARCHAR | Basecamp project/bucket ID |
| basecamp_todo_id | VARCHAR | Basecamp todo ID |
| basecamp_url | TEXT | Web URL for "Open in Basecamp" links |
| account_id | UUID (FK) | FK to accounts (nullable for ungrouped tasks) |
| project_type | VARCHAR | "joint" or "internal" |
| title | TEXT | Task title |
| assignee | VARCHAR | Primary assignee name |
| assignee_names | JSONB | All assignee names |
| completion_subscribers | JSONB | Names notified when the to-do is completed |
| due_date | DATE | |
| status | VARCHAR | active, closed, archived |
| closed_at | TIMESTAMP(tz) | When completed on Basecamp |
| last_known_activity_at | TIMESTAMP(tz) | Latest comment timestamp |
| last_checked_at | TIMESTAMP(tz) | Last polled |
| last_analyzed_at | TIMESTAMP(tz) | Last AI analysis |
| has_new_activity | BOOLEAN | Flag: needs re-analysis |
| comment_count | INTEGER | |
| raw_data | JSONB | Full Basecamp todo API response |
| raw_comments | JSONB | Last 20 comment objects |
| priority_score | INTEGER | 0-100, computed by hybrid scoring |
| priority_tier | VARCHAR | critical, high, medium, low, minimal |
| previous_score | INTEGER | Score before last re-scoring (trend) |
| previous_tier | VARCHAR | Tier before last re-scoring |
| score_breakdown | JSONB | Per-signal point breakdown (see example below) |
| last_analyzed_comments_hash | VARCHAR | SHA-256 for smart AI skip |
| ai_signals | JSONB | AI-detected content signals |
| ai_recommendation | TEXT | Suggested next action |
| ai_ball_with | VARCHAR | Who should act next |
| ai_current_stage | VARCHAR | Workflow stage |
| analysis_status | VARCHAR | pending, partial, complete, error |
| analysis_error_count | INTEGER | Consecutive failures (max 3 retries) |
| is_starred | BOOLEAN | User-starred for priority boost |
| dismissed_at | TIMESTAMP(tz) | When dismissed (hides until restored) |
| snoozed_until | TIMESTAMP(tz) | Hidden until this datetime |
| vote | SMALLINT | 1=upvote, -1=downvote, NULL=none |
| voted_at | TIMESTAMP(tz) | When vote was cast |
| vote_context | JSONB | Scoring snapshot at vote time |
| added_at | TIMESTAMP(tz) | When added to Radar |
| updated_at | TIMESTAMP(tz) | |

**Unique constraint:** `(basecamp_bucket_id, basecamp_todo_id)`
**Indexes:** `status`, `priority_tier`, composite `(account_id, status, priority_tier)`. Standalone `account_id` and `last_checked_at` indexes dropped (migration `2c12be573b1b`).

### config

| Column | Type | Description |
|--------|------|-------------|
| key | VARCHAR | Primary key |
| value | JSONB | Setting value |
| updated_at | TIMESTAMP(tz) | |

### poll_runs

| Column | Type | Description |
|--------|------|-------------|
| id | UUID | Primary key |
| started_at | TIMESTAMP(tz) | When the poll cycle started |
| completed_at | TIMESTAMP(tz) | When the poll cycle completed |
| duration_seconds | FLOAT | Total duration |
| stats | JSONB | Aggregate stats (tasks discovered, analyzed, errors) |

Kept last 50 entries.

---

## Task Lifecycle

```
              +---------------------------+
              |    (reopened on Basecamp)  |
              v                           |
        +----------+     +--------+      |
        |  ACTIVE  |---->| CLOSED |------+
        +----------+     +--------+
                              |
                              | (auto, 3 months)
                              v
                         +----------+
                         | ARCHIVED |
                         +----------+
```

---

## AI Analysis Output Schema

Gemini returns structured JSON per task:

```json
{
  "is_blocked": false,
  "blocked_reason": null,
  "has_direct_mention": true,
  "needs_approval": false,
  "ownership_ambiguous": false,
  "ball_with": "Developer (Sam T.)",
  "current_stage": "in_progress",
  "recommendation": "Follow up with Sam T. on the schema implementation.",
  "urgency_factors": ["Developer went silent after partial implementation"]
}
```

---

## Gemini Prompt Template

Source: `app/services/gemini.py` `_build_prompt()`. The response is additionally constrained by a JSON schema (`_RESPONSE_SCHEMA`) and validated by `_validate_response()`.

```
You are analyzing a Basecamp task thread for a Digital Producer ({primary_producer})
managing client campaigns at a digital marketing agency.

== CONTEXT ==
Task: {title}
Project: {project_name} ({project_type})
Client Account: {account_name}
Assignee(s): {all_assignees}
Completion Subscribers (notified when done): {completion_subscribers}
Our Team in Assignees: {producers_among_assignees}
Our Team in Subscribers: {producers_among_subscribers}
Due Date: {due_date or "none"}
Days Since Last Comment: {days_since_last_comment}
Total Comments: {comment_count}

== COMMENT THREAD ==
{formatted_comments}

== PROJECT TYPE SIGNIFICANCE ==
- "Joint" projects are shared with the client — tasks are client-facing, higher urgency, and visible to client stakeholders.
- "Internal" projects are agency-only — tasks are internal coordination and execution, not visible to the client.

== INSTRUCTIONS ==
Analyze this task thread and determine:
- Whether the task is blocked, who should act next, and what stage it's in
- Whether any of these people are directly mentioned or addressed: {producer_names}
- Whether someone is requesting review/approval from the producer
- Whether ownership of the next action is unclear
- Consider both assignees and completion subscribers when determining ball_with. Subscribers are stakeholders who are notified on completion, not doers.

For ball_with, return ONLY the first name (e.g. "Alex" not "Alex Parker").

Return ONLY a JSON object with these fields:
{
  "is_blocked": <boolean — is this task stuck waiting on something?>,
  "blocked_reason": <string or null — what is blocking it?>,
  "has_direct_mention": <boolean — does the thread mention or address ANY of: {producer_names}?>,
  "needs_approval": <boolean — is someone requesting review, approval, or sign-off from the DP?>,
  "ownership_ambiguous": <boolean — is it unclear who should act next?>,
  "ball_with": <string — first name only of who should act next>,
  "current_stage": <string — one of: new, in_progress, pending_review, pending_client, blocked, completed>,
  "recommendation": <string — specific, actionable next step for the DP>,
  "urgency_factors": [<string — factors that make this more or less urgent>]
}
```

---

## Score Breakdown JSONB Example

Stored in `tasks.score_breakdown`. Values follow scoring model v4 (see [ARCHITECTURE.md](ARCHITECTURE.md#hybrid-scoring-model-v4) for the formula).

```json
{
  "staleness": { "points": 55, "days": 9 },
  "joint_boost": { "points": 0, "is_joint": true, "multiplier": 1.4 },
  "comment_activity": { "points": 10, "count": 7 },
  "deadline": { "points": 15, "due_in_days": 3 },
  "blocker": { "points": 50, "reason": "Waiting on client credentials" },
  "mention": { "points": 0 },
  "approval": { "points": 0 },
  "ambiguous": { "points": 0 },
  "zero_engagement": { "points": 0, "days": 9 },
  "ball_with": { "points": 20, "value": "Alex" },
  "ball_not_with": { "points": 0, "value": "Alex" },
  "stage": { "points": 15, "value": "pending_review" },
  "star_boost": { "points": 60, "active": true, "reason": "blocker detected" },
  "raw_total": 225,
  "recurring_dampener": { "active": false, "multiplier": 1.0, "points": 0 },
  "joint_multiplier": 1.4,
  "effective_total": 315.0,
  "normalized_score": 100,
  "tier": "critical"
}
```

---

## Vote Context JSONB Example

Stored in `tasks.vote_context` when a vote is cast or task is dismissed.

```json
{
  "score": 72,
  "tier": "high",
  "staleness_days": 6,
  "signals": { "is_blocked": true, "has_direct_mention": false },
  "ball_with": "Sam",
  "stage": "blocked",
  "project_type": "joint",
  "account_name": "Acme Roofing Co.",
  "score_breakdown": { "..." : "..." },
  "source": "dismiss"
}
```

