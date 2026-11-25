"""Role-based response contracts for interview state.

The persisted interview state is an internal working document: it carries the
adaptive graph's decisions, evaluation internals, rubric snapshots and the
retrieval provenance (``retrieved_sources`` with document/chunk ids) recorded for
evidence tracing. Candidates must only ever receive an explicit allowlist of
fields; organization staff keep the full state for review and support.

The view is selected server-side from the authenticated ``TenantContext`` role.
It is never client-selectable. Any role that is not explicitly staff receives the
candidate view (fail closed).
"""

from __future__ import annotations

from typing import Any

# Roles that may see the full internal interview state. org_admin is listed for
# contract completeness; interview repositories currently deny org_admin access
# to interview rows entirely (404 / empty list), which this module does not change.
STAFF_ROLES = frozenset({"recruiter", "org_admin", "platform_admin"})

# Candidate-visible top-level fields copied verbatim from the state.
CANDIDATE_FIELDS = (
    "interview_id",  # required to submit answers / reconnect
    "status",  # active | completed; drives the candidate UI flow
    "mode",  # text | voice, echoed back from the start request
    "position",  # role the candidate is interviewing for
    "current_question",  # the question the candidate must answer next
    "created_at",
    "updated_at",
)

# Campaign identifiers the candidate already holds (they are in the campaign
# start URL and the candidate's own schedule); echoed so a client can correlate
# the interview with its scheduled slot. Only present for campaign interviews.
CANDIDATE_CAMPAIGN_FIELDS = ("campaign_id", "application_id", "schedule_id")


def is_staff_role(role: str | None) -> bool:
    return role in STAFF_ROLES


def _question_budget(state: dict[str, Any]) -> int | None:
    budget = state.get("question_budget")
    if budget is None:
        plan = state.get("interview_plan")
        if isinstance(plan, dict):
            budget = plan.get("question_budget")
    try:
        return int(budget) if budget is not None else None
    except (TypeError, ValueError):
        return None


def candidate_interview_view(state: dict[str, Any]) -> dict[str, Any]:
    """Return the candidate allowlist projection of an interview state."""
    view: dict[str, Any] = {
        field: state.get(field) for field in CANDIDATE_FIELDS
    }
    for field in CANDIDATE_CAMPAIGN_FIELDS:
        value = state.get(field)
        if value is not None:
            view[field] = value
    history = state.get("history")
    view["questions_answered"] = len(history) if isinstance(history, list) else 0
    view["question_budget"] = _question_budget(state)
    return view


def interview_view_for_role(state: dict[str, Any], role: str | None) -> dict[str, Any]:
    """Project interview state for the caller's server-resolved role."""
    if is_staff_role(role):
        return state
    return candidate_interview_view(state)
