"""Application service for interview lifecycle operations."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from agents.interview_agent.graph import InterviewAgentCore
from config import settings
from evaluation.service import (
    DeterministicEvaluator,
    EvaluationContext,
    EvaluationService,
    LLMEvaluator,
    Rubric,
)
from infrastructure.distributed_lock import (
    DistributedLock,
    LockAcquisitionTimeout,
    RedisUnavailableError,
)
from infrastructure.redis_store import RedisClient, RedisStore
from integrations.llm_providers.provider import LLMProviderError, provider_from_settings
from rag.service import RAGQuestionService
from services.database import (
    CandidateRecord,
    InterviewRecord,
    InterviewScheduleRecord,
    RecruitmentApplicationRecord,
    RecruitmentCampaignHiringOutcomeRecord,
    RecruitmentCampaignRecord,
    SessionLocal,
)
from services.recruitment_campaign_service import RecruitmentCampaignService
from services.repositories import (
    EvaluationRepository,
    InterviewRepository,
    QuestionRepository,
    TurnRepository,
)
from services.turn_state import TurnStatus


class InterviewNotFoundError(LookupError):
    """Raised when an interview does not exist."""


class InterviewForbiddenError(PermissionError):
    """Raised when a user is authenticated but cannot perform an operation."""


class InterviewConflictError(RuntimeError):
    """Raised when a turn ID is reused with a different answer payload."""


class CampaignInterviewNotReadyError(RuntimeError):
    """Raised when a campaign interview is not eligible to start."""


class InterviewProcessingError(RuntimeError):
    """Raised when another request is still processing this interview."""


class InterviewUnavailableError(RuntimeError):
    """Raised when safe distributed processing cannot be guaranteed."""


class InterviewEvaluationError(RuntimeError):
    """Raised when an answer cannot be evaluated and should be retried."""


class InterviewService:
    """Coordinates interview decisions and persistence within explicit transactions."""

    def __init__(
        self,
        agent: InterviewAgentCore | None = None,
        evaluator: EvaluationService | None = None,
        rag: RAGQuestionService | None = None,
        interview_repository: InterviewRepository | None = None,
        turn_repository: TurnRepository | None = None,
        evaluation_repository: EvaluationRepository | None = None,
        redis_client: Any | None = None,
    ) -> None:
        self.rag = rag or RAGQuestionService()
        llm_provider = provider_from_settings(settings)
        self.agent = agent or InterviewAgentCore(
            llm_provider=llm_provider,
            context_retriever=self._retrieve_graph_context,
        )
        if evaluator is not None:
            self.evaluator = evaluator
        elif llm_provider is not None:
            self.evaluator = EvaluationService(LLMEvaluator(llm_provider))
        else:
            self.evaluator = EvaluationService(DeterministicEvaluator())
        self.interviews = interview_repository or InterviewRepository()
        self.turns = turn_repository or TurnRepository()
        self.evaluations = evaluation_repository or EvaluationRepository()
        self.questions = QuestionRepository()
        self.redis_client = redis_client or (RedisClient(settings.REDIS_URL, namespace="interview") if settings.REDIS_URL else None)
        self.redis_store = RedisStore(self.redis_client, namespace="interview") if self.redis_client is not None else None

    def _get_processing_lock(self, interview_id: str) -> DistributedLock | None:
        if self.redis_client is None:
            return None
        lock_name = self.redis_store._key(f"processing:{interview_id}") if self.redis_store is not None else f"interview:processing:{interview_id}"
        lock = DistributedLock(
            self.redis_client,
            lock_name,
            ttl_seconds=settings.REDIS_LOCK_TTL_SECONDS,
            acquire_timeout_seconds=settings.REDIS_LOCK_ACQUIRE_TIMEOUT_SECONDS,
        )
        if not lock.acquire():
            return None
        return lock

    def _release_processing_lock(self, interview_id: str, lock: DistributedLock | None) -> None:
        if lock is None:
            return
        lock.release()

    def start_interview(
        self,
        candidate_id: str,
        position: str,
        mode: str = "text",
        owner_user_id: str | None = None,
        owner_role: str = "candidate",
        organization_id: str | None = None,
    ) -> dict[str, Any]:
        if not organization_id or owner_role in {"org_admin", "platform_admin"}:
            raise InterviewNotFoundError(candidate_id)
        with SessionLocal() as session:
            candidate = session.execute(
                select(CandidateRecord).where(
                    CandidateRecord.id == candidate_id,
                    CandidateRecord.organization_id == organization_id,
                )
            ).scalar_one_or_none()
            if not self.interviews.can_manage_candidate(
                session, candidate_id, owner_user_id or "", owner_role, organization_id
            ) or candidate is None:
                raise InterviewNotFoundError(candidate_id)
        state = self.agent.start_interview(candidate_id=candidate_id, position=position)
        interview_id = str(uuid4())
        now = datetime.now(UTC).isoformat()
        state.update({
            "interview_id": interview_id,
            "mode": mode,
            "organization_id": organization_id,
            "created_at": now,
            "updated_at": now,
        })
        with SessionLocal.begin() as session:
            candidate = session.execute(
                select(CandidateRecord).where(
                    CandidateRecord.id == candidate_id,
                    CandidateRecord.organization_id == organization_id,
                )
            ).scalar_one_or_none()
            if not self.interviews.can_manage_candidate(
                session, candidate_id, owner_user_id or "", owner_role, organization_id
            ) or candidate is None:
                raise InterviewNotFoundError(candidate_id)
            self.interviews.create(
                session, state, candidate.owner_user_id, organization_id
            )
        return state

    def start_campaign_interview(
        self,
        campaign_id: str,
        application_id: str,
        schedule_id: str,
        mode: str = "text",
        owner_user_id: str | None = None,
        owner_role: str = "candidate",
        organization_id: str | None = None,
    ) -> dict[str, Any]:
        if (
            not organization_id
            or owner_role != "candidate"
            or not owner_user_id
        ):
            raise InterviewNotFoundError(application_id)

        try:
            with SessionLocal.begin() as session:
                candidate, campaign = self._campaign_interview_target(
                    session,
                    campaign_id=campaign_id,
                    application_id=application_id,
                    schedule_id=schedule_id,
                    owner_user_id=owner_user_id,
                    organization_id=organization_id,
                )
                existing = session.execute(
                    select(InterviewRecord).where(
                        InterviewRecord.organization_id == organization_id,
                        InterviewRecord.campaign_id == campaign_id,
                        InterviewRecord.application_id == application_id,
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    if existing.schedule_id != schedule_id:
                        raise CampaignInterviewNotReadyError(
                            "Application already has an interview linked to another schedule"
                        )
                    return self._state(existing)
                # Checked after the replay above: a recorded outcome blocks
                # creating a new interview, not returning the existing one.
                if self._has_recorded_outcome(
                    session,
                    campaign_id=campaign_id,
                    application_id=application_id,
                    organization_id=organization_id,
                ):
                    raise CampaignInterviewNotReadyError(
                        "Application already has a recorded hiring outcome"
                    )
                if RecruitmentCampaignService.target_is_achieved(
                    session, campaign
                ):
                    raise CampaignInterviewNotReadyError(
                        "Campaign hiring target has already been achieved"
                    )
                state = self._campaign_interview_state(
                    candidate_id=candidate.id,
                    campaign=campaign,
                    application_id=application_id,
                    schedule_id=schedule_id,
                    mode=mode,
                    organization_id=organization_id,
                )
                self.interviews.create(
                    session, state, candidate.owner_user_id, organization_id
                )
                return state
        except IntegrityError as exc:
            with SessionLocal() as session:
                existing = session.execute(
                    select(InterviewRecord).where(
                        InterviewRecord.organization_id == organization_id,
                        InterviewRecord.campaign_id == campaign_id,
                        InterviewRecord.application_id == application_id,
                    )
                ).scalar_one_or_none()
                if existing is None:
                    raise
                if existing.schedule_id != schedule_id:
                    raise CampaignInterviewNotReadyError(
                        "Application already has an interview linked to another schedule"
                    ) from exc
                return self._state(existing)

    def _campaign_interview_target(
        self,
        session,
        *,
        campaign_id: str,
        application_id: str,
        schedule_id: str,
        owner_user_id: str,
        organization_id: str,
    ) -> tuple[CandidateRecord, RecruitmentCampaignRecord]:
        application = session.execute(
            select(RecruitmentApplicationRecord).where(
                RecruitmentApplicationRecord.id == application_id,
                RecruitmentApplicationRecord.campaign_id == campaign_id,
                RecruitmentApplicationRecord.organization_id == organization_id,
            )
        ).scalar_one_or_none()
        campaign = session.execute(
            select(RecruitmentCampaignRecord).where(
                RecruitmentCampaignRecord.id == campaign_id,
                RecruitmentCampaignRecord.organization_id == organization_id,
            )
        ).scalar_one_or_none()
        if application is None or campaign is None:
            raise InterviewNotFoundError(application_id)
        if not RecruitmentCampaignService.approval_is_current(campaign):
            raise CampaignInterviewNotReadyError("Campaign approval is no longer current")
        if application.status != "needs_review" or application.review_status != "reviewed":
            raise CampaignInterviewNotReadyError(
                "Application screening must be reviewed before interviewing"
            )
        candidate = session.execute(
            select(CandidateRecord).where(
                CandidateRecord.id == application.candidate_id,
                CandidateRecord.organization_id == organization_id,
                CandidateRecord.user_id == owner_user_id,
            )
        ).scalar_one_or_none()
        if candidate is None or not self.interviews.can_manage_candidate(
            session,
            application.candidate_id,
            owner_user_id,
            "candidate",
            organization_id,
        ):
            raise InterviewNotFoundError(application_id)
        schedule = session.execute(
            select(InterviewScheduleRecord.id).where(
                InterviewScheduleRecord.id == schedule_id,
                InterviewScheduleRecord.organization_id == organization_id,
                InterviewScheduleRecord.campaign_id == campaign_id,
                InterviewScheduleRecord.application_id == application_id,
                InterviewScheduleRecord.candidate_id == candidate.id,
                InterviewScheduleRecord.status == "confirmed_internal",
            )
        ).scalar_one_or_none()
        if schedule is None:
            raise CampaignInterviewNotReadyError(
                "A confirmed internal schedule is required"
            )
        return candidate, campaign

    @staticmethod
    def _has_recorded_outcome(
        session,
        *,
        campaign_id: str,
        application_id: str,
        organization_id: str,
    ) -> bool:
        return (
            session.execute(
                select(RecruitmentCampaignHiringOutcomeRecord.id).where(
                    RecruitmentCampaignHiringOutcomeRecord.organization_id
                    == organization_id,
                    RecruitmentCampaignHiringOutcomeRecord.campaign_id == campaign_id,
                    RecruitmentCampaignHiringOutcomeRecord.application_id
                    == application_id,
                )
            ).scalar_one_or_none()
            is not None
        )

    def _campaign_interview_state(
        self,
        *,
        candidate_id: str,
        campaign: RecruitmentCampaignRecord,
        application_id: str,
        schedule_id: str,
        mode: str,
        organization_id: str,
    ) -> dict[str, Any]:
        objective = RecruitmentCampaignService._objective(campaign)
        position = " ".join(
            part for part in (objective.seniority, objective.requested_role) if part
        )
        if not position:
            raise CampaignInterviewNotReadyError(
                "Approved campaign objective does not specify a role"
            )
        rubric_snapshot = json.loads(campaign.competency_rubric_json or "[]")
        competencies = list(
            dict.fromkeys(
                criterion["competency"]
                for criterion in rubric_snapshot
                if isinstance(criterion, dict)
                and isinstance(criterion.get("competency"), str)
                and criterion["competency"].strip()
            )
        )
        if not competencies:
            raise CampaignInterviewNotReadyError(
                "Approved campaign rubric does not specify interview competencies"
            )
        state = self.agent.start_interview(
            candidate_id=candidate_id,
            position=position,
            competencies=competencies,
        )
        now = datetime.now(UTC).isoformat()
        state.update(
            {
                "interview_id": str(uuid4()),
                "mode": mode,
                "organization_id": organization_id,
                "created_at": now,
                "updated_at": now,
                "campaign_id": campaign.id,
                "application_id": application_id,
                "schedule_id": schedule_id,
                "objective_version": campaign.objective_version,
                "plan_version": campaign.plan_version,
                "rubric_snapshot": rubric_snapshot,
            }
        )
        return state

    def get_interview(
        self,
        interview_id: str,
        user_id: str | None = None,
        role: str = "candidate",
        organization_id: str | None = None,
    ) -> dict[str, Any] | None:
        if not organization_id:
            return None
        with SessionLocal.begin() as session:
            interview = self.interviews.get_authorized(
                session, interview_id, user_id or "", role, organization_id
            )
            if interview is None:
                return None
            return self._state(interview)

    def list_interviews(
        self,
        user_id: str | None = None,
        role: str = "candidate",
        organization_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if not organization_id:
            return []
        with SessionLocal.begin() as session:
            interviews = self.interviews.list_authorized(
                session, user_id or "", role, organization_id
            )
            return [self._state(interview) for interview in interviews]

    def submit_answer(
        self,
        interview_id: str,
        answer: str,
        turn_id: str,
        user_id: str | None = None,
        role: str = "candidate",
        organization_id: str | None = None,
    ) -> dict[str, Any]:
        if role in {"recruiter", "org_admin", "platform_admin"}:
            raise InterviewForbiddenError("This organization membership has read-only interview access")
        if not organization_id:
            raise InterviewNotFoundError(interview_id)
        lock = self._acquire_submission_lock(
            interview_id, answer, turn_id, user_id, role, organization_id
        )
        try:
            replay = self._prepare_turn(
                interview_id, answer, turn_id, user_id, role, organization_id
            )
            if replay is not None:
                return replay
            return self._process_turn(
                interview_id, answer, turn_id, user_id, role, organization_id
            )
        finally:
            self._release_processing_lock(interview_id, lock)

    def _acquire_submission_lock(
        self,
        interview_id: str,
        answer: str,
        turn_id: str,
        user_id: str | None,
        role: str,
        organization_id: str,
    ) -> DistributedLock:
        with SessionLocal() as session:
            if self.interviews.get_authorized(
                session, interview_id, user_id or "", role, organization_id
            ) is None:
                raise InterviewNotFoundError(interview_id)
        try:
            lock = self._get_processing_lock(interview_id)
        except LockAcquisitionTimeout as exc:
            with SessionLocal() as session:
                existing_turn = self.turns.get_by_client_id(session, interview_id, turn_id)
                if existing_turn is not None and existing_turn.candidate_answer != answer:
                    raise InterviewConflictError(
                        "turn_id was already used with a different answer"
                    ) from exc
            raise InterviewProcessingError(
                "Interview is still processing; retry the same turn_id and answer."
            ) from exc
        except RedisUnavailableError as exc:
            raise InterviewUnavailableError(
                "Cannot safely process interview while distributed locking is unavailable"
            ) from exc
        if lock is None:
            raise InterviewUnavailableError(
                "Cannot safely process interview without distributed locking"
            )
        return lock

    def _prepare_turn(
        self,
        interview_id: str,
        answer: str,
        turn_id: str,
        user_id: str | None,
        role: str,
        organization_id: str,
    ) -> dict[str, Any] | None:
        try:
            with SessionLocal.begin() as session:
                interview = self.interviews.get_authorized(
                    session, interview_id, user_id or "", role, organization_id
                )
                if interview is None:
                    raise InterviewNotFoundError(interview_id)
                existing_turn = self.turns.get_by_client_id(session, interview_id, turn_id)
                if existing_turn is not None:
                    if not self._claim_existing_turn(existing_turn, answer):
                        return self._state(interview)
                else:
                    state = self._state(interview)
                    if state.get("status") == "completed":
                        raise InterviewConflictError("Interview has already been completed")
                    self.turns.create_processing(session, interview_id, turn_id, answer)
        except IntegrityError as exc:
            with SessionLocal() as session:
                interview = self.interviews.get_authorized(
                    session, interview_id, user_id or "", role, organization_id
                )
                if interview is None:
                    raise InterviewNotFoundError(interview_id) from exc
                existing_turn = self.turns.get_by_client_id(session, interview_id, turn_id)
                if existing_turn is not None and existing_turn.candidate_answer != answer:
                    raise InterviewConflictError(
                        "turn_id was already used with a different answer"
                    ) from exc
                return self._state(interview)
        return None

    def _claim_existing_turn(self, turn: Any, answer: str) -> bool:
        if turn.candidate_answer != answer:
            raise InterviewConflictError("turn_id was already used with a different answer")
        if turn.status == TurnStatus.FAILED_RETRYABLE:
            self.turns.transition(turn, TurnStatus.PROCESSING)
            return True
        if self.turns.reclaim_if_stale(turn, settings.TURN_PROCESSING_LEASE_SECONDS):
            return True
        if turn.status == TurnStatus.PROCESSING:
            raise InterviewProcessingError(
                "Interview is still processing; retry the same turn_id and answer."
            )
        if turn.status == TurnStatus.COMPLETED:
            return False
        if turn.status == TurnStatus.RECEIVED:
            raise InterviewProcessingError(
                "Interview is still processing; retry the same turn_id and answer."
            )
        raise InterviewConflictError("turn has failed permanently and cannot be replayed")

    def _process_turn(
        self,
        interview_id: str,
        answer: str,
        turn_id: str,
        user_id: str | None,
        role: str,
        organization_id: str,
    ) -> dict[str, Any]:
        try:
            with SessionLocal.begin() as session:
                interview = self.interviews.get_authorized(
                    session, interview_id, user_id or "", role, organization_id
                )
                if interview is None:
                    raise InterviewNotFoundError(interview_id)
                persisted_turn = self.turns.get_by_client_id(session, interview_id, turn_id)
                if persisted_turn is None:
                    raise InterviewNotFoundError(interview_id)
                state = self._state(interview)
                if self.evaluations.has_assessment(session, interview_id, persisted_turn.id):
                    if persisted_turn.status == TurnStatus.PROCESSING:
                        self.turns.transition(persisted_turn, TurnStatus.COMPLETED)
                    return state
                asked_questions = self.questions.list_for_interview(session, interview_id)
                asked_question = asked_questions[-1] if asked_questions else None
                question_text = (
                    asked_question.question_text
                    if asked_question is not None
                    else state.get("current_question", "")
                )
                target_competency = (
                    (asked_question.competency if asked_question is not None else None)
                    or state.get("current_competency")
                    or "general"
                )
                campaign_criteria = [
                    criterion
                    for criterion in state.get("rubric_snapshot", [])
                    if isinstance(criterion, dict)
                    and criterion.get("competency") == target_competency
                ]
                rubric = Rubric(
                    competency=target_competency,
                    campaign_criteria=campaign_criteria,
                )
                evaluation_context = EvaluationContext(
                    position=str(state.get("position", "")),
                    prior_answers=[
                        str(item.get("answer", ""))
                        for item in state.get("history", [])[-5:]
                        if item.get("answer")
                    ],
                    competency_state=state.get("skills", {}),
                    identified_gaps=state.get("identified_gaps", []),
                )
                try:
                    result = self.evaluator.evaluate_result(
                        question_text,
                        answer,
                        target_competency,
                        rubric,
                        evaluation_context,
                    )
                except LLMProviderError as exc:
                    raise InterviewEvaluationError(
                        "Answer evaluation is unavailable; retry this turn."
                    ) from exc
                persisted_turn.question_id = asked_question.id if asked_question is not None else None
                graph_state = dict(state)
                graph_state["questions_asked"] = [item.question_text for item in asked_questions]
                updated_state = self.agent.handle_answer(
                    graph_state,
                    answer,
                    turn_id,
                    evaluation=result,
                    rubric=rubric,
                    asked_questions=graph_state["questions_asked"],
                )
                updated_state["follow_up_questions"] = self.rag.retrieve_relevant_questions(
                    updated_state.get("position", ""), updated_state.get("skills", {})
                )
                evaluation = self.evaluator.summarize(
                    result,
                    updated_state.get("position", ""),
                    updated_state.get("skills", {}),
                )
                question_id = asked_question.id if asked_question is not None else None
                self.evaluations.save_result(session, interview_id, persisted_turn, question_id, result)
                updated_state["evaluation"] = evaluation
                updated_state["overall_score"] = evaluation["overall_score"]
                updated_state["recommended_next_action"] = updated_state["next_action"]
                updated_state["updated_at"] = datetime.now(UTC).isoformat()
                self.interviews.save_state(session, interview, updated_state, persisted_turn)
                return updated_state
        except Exception:
            with SessionLocal.begin() as session:
                interview = self.interviews.get_authorized(
                    session, interview_id, user_id or "", role, organization_id
                )
                if interview is not None:
                    failed_turn = self.turns.get_by_client_id(
                        session, interview_id, turn_id
                    )
                    if failed_turn is not None and failed_turn.status == TurnStatus.PROCESSING:
                        self.turns.transition(
                            failed_turn, TurnStatus.FAILED_RETRYABLE
                        )
            raise

    def _retrieve_graph_context(self, state: dict[str, Any]) -> str:
        result = self.rag.retrieve_context(
            str(state.get("position", "")),
            state.get("current_competency"),
            state.get("identified_gaps", []),
            question=state.get("current_question"),
            answer_evidence=[str(state.get("candidate_answer", ""))],
            previous_questions=state.get("asked_questions", []),
            difficulty=state.get("difficulty"),
            job_id=state.get("job_id") or state.get("interview_template_id"),
            organization_id=state.get("organization_id"),
        )
        if result.fallback_reason and result.fallback_reason.startswith("retrieval_failed:"):
            raise RuntimeError("Interview question context retrieval failed")
        return self.rag.format_context(result)

    @staticmethod
    def _state(interview: Any) -> dict[str, Any]:
        state = json.loads(interview.state_json)
        state.update({
            "interview_id": interview.id,
            "candidate_id": interview.candidate_id,
            "organization_id": interview.organization_id,
            "position": interview.position,
            "mode": interview.mode,
            "current_question": interview.current_question,
        })
        return state
