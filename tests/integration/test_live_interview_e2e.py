"""Explicit opt-in, synthetic-data live interview workflow."""

from __future__ import annotations

import json
import logging
import math
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

logger = logging.getLogger(__name__)
TOTAL_RUNTIME_BUDGET_SECONDS = 300


class _CountingEmbeddingProvider:
    def __init__(self, provider: Any) -> None:
        self._provider = provider
        self.model = provider.model
        self.version = provider.version
        self.dimension = provider.dimension
        self.api_call_count = 0

    def embed(self, text: str) -> list[float]:
        self.api_call_count += 1
        if self.api_call_count > 40:
            raise RuntimeError("Live E2E embedding request budget of 40 exceeded.")
        vector = self._provider.embed(text)
        if len(vector) != self.dimension or any(
            not isinstance(value, int | float)
            or isinstance(value, bool)
            or not math.isfinite(value)
            for value in vector
        ):
            raise ValueError("Embedding provider returned an invalid vector.")
        return vector


def _live_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    if os.getenv("RUN_LIVE_AI_E2E") != "1":
        pytest.skip("Set RUN_LIVE_AI_E2E=1 to opt into the live interview workflow.")
    from dotenv import load_dotenv

    load_dotenv(override=False)
    required = {
        "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY"),
        "LLM_PROVIDER=openai": "openai"
        if os.getenv("LLM_PROVIDER", "").lower() == "openai"
        else None,
        "EMBEDDING_PROVIDER=openai": "openai"
        if os.getenv("EMBEDDING_PROVIDER", "").lower() == "openai"
        else None,
        "LIVE_AI_E2E_DATABASE_URL": os.getenv("LIVE_AI_E2E_DATABASE_URL"),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        pytest.fail("Missing live E2E configuration: " + ", ".join(missing))

    database_url = str(os.environ["LIVE_AI_E2E_DATABASE_URL"])
    if not database_url.startswith("sqlite:///") or "live_ai_e2e_" not in database_url.lower():
        pytest.fail(
            "LIVE_AI_E2E_DATABASE_URL must point to a dedicated SQLite file whose name "
            "contains 'live_ai_e2e_'; production and shared databases are refused."
        )
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "10")
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    monkeypatch.setenv("OPENAI_MAX_TOKENS", "900")
    monkeypatch.setenv("EMBEDDING_TIMEOUT_SECONDS", "10")
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path / "reports"))
    from config import settings

    if settings.DATABASE_URL != database_url:
        pytest.fail(
            "Live E2E settings were loaded before the isolated database was configured; "
            "run this test in a fresh pytest process."
        )


def _bounded_call(deadline: float, label: str, operation: Callable[[], Any]) -> Any:
    if time.monotonic() >= deadline:
        pytest.fail(f"Live E2E total runtime budget exceeded before {label}.")
    try:
        result = operation()
    except Exception as exc:
        pytest.fail(f"Live E2E {label} failed with {type(exc).__name__}.")
    if time.monotonic() >= deadline:
        pytest.fail(f"Live E2E total runtime budget exceeded during {label}.")
    return result


class _TrackingProvider:
    def __init__(self, provider: Any) -> None:
        self.provider_name = provider.provider_name
        self.model = provider.model
        self.prompt_version = provider.prompt_version
        self._provider = provider
        self.structured_successes = 0
        self.request_count = 0

    def generate(self, system_instruction: str, user_content: str) -> str:
        self._count_request()
        return self._provider.generate(system_instruction, user_content)

    def generate_structured(self, system_instruction: str, user_content: str, schema: type):
        self._count_request()
        result = self._provider.generate_structured(system_instruction, user_content, schema)
        self.structured_successes += 1
        return result

    def _count_request(self) -> None:
        if self.request_count >= 40:
            raise RuntimeError("Live E2E provider request budget of 40 exceeded.")
        self.request_count += 1


class _MemoryRedis:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    def set(self, key: str, value: Any, **options: Any) -> bool:
        if options.get("nx") and key in self.values:
            return False
        self.values[key] = (value, options.get("ex"))
        return True

    def eval(self, *args: Any) -> int:
        if len(args) < 4 or not isinstance(args[0], str) or args[1] != 1:
            return 0
        key, owner = args[2], args[3]
        current = self.values.get(key)
        if current is None or current[0] != owner:
            return 0
        del self.values[key]
        return 1


def _apply_migrations(root: Path, deadline: float) -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(str(root / "alembic.ini"))
    _bounded_call(deadline, "database migrations", lambda: command.upgrade(config, "head"))


def _index_corpus_and_retrieve(
    deadline: float, monkeypatch: pytest.MonkeyPatch
) -> tuple[_CountingEmbeddingProvider, int, Any]:
    import rag.service as rag_service_module
    from config import settings
    from rag.corpus_loader import load_demo_corpus
    from rag.service import RAGQuestionService, SQLAlchemyVectorStore, configured_embedding_provider

    if settings.LLM_PROVIDER.lower() != "openai" or settings.EMBEDDING_PROVIDER.lower() != "openai":
        pytest.fail("Live E2E must use the configured OpenAI LLM and embedding providers.")
    configured_provider = _bounded_call(
        deadline, "embedding provider configuration", configured_embedding_provider
    )
    if configured_provider is None:
        pytest.fail("OPENAI_API_KEY is required for live embedding verification.")
    provider = _CountingEmbeddingProvider(configured_provider)
    monkeypatch.setattr(
        rag_service_module, "configured_embedding_provider", lambda: provider
    )

    documents = load_demo_corpus()
    store = SQLAlchemyVectorStore()
    indexed = _bounded_call(
        deadline,
        "demo corpus ingestion",
        lambda: store.upsert(
            documents,
            provider,
            replace_source=True,
            global_approval_reference=(
                "repository:rag/corpus/demo_v1.json#demo-v1"
            ),
        ),
    )
    if indexed != len(documents):
        pytest.fail("Demo corpus ingestion did not persist semantic vectors for every chunk.")

    rag = RAGQuestionService()
    retrieval = _bounded_call(
        deadline,
        "semantic demo-corpus retrieval",
        lambda: rag.retrieve_context(
            "AI Engineer",
            "python",
            ["bounded concurrency and cancellation"],
            question="Keep simultaneous remote requests responsive.",
            job_id="ai_engineer",
        ),
    )
    if retrieval.dense_count == 0 or not retrieval.chunks:
        pytest.fail("Live semantic retrieval returned no dense demo-corpus candidates.")
    return provider, indexed, retrieval


def _create_interview(
    run_id: str, provider: Any, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, dict[str, str], dict[str, str], str, str, Any, str]:
    from sqlalchemy import select

    import services.interview_service as interview_service_module
    from services.database import (
        CandidateRecord,
        InterviewQuestionRecord,
        InterviewRecord,
        OrganizationMembershipRecord,
        OrganizationRecord,
        SessionLocal,
    )
    from services.interview_service import InterviewService
    from services.user_store import UserStore

    recruiter_email = f"live-recruiter-{run_id}@example.invalid"
    candidate_email = f"live-candidate-{run_id}@example.invalid"
    recruiter = UserStore.create(recruiter_email, uuid4().hex, role="recruiter")
    candidate_user = UserStore.create(candidate_email, uuid4().hex, role="candidate")
    organization_id = str(uuid4())
    candidate_id = str(uuid4())
    with SessionLocal.begin() as session:
        session.add(
            OrganizationRecord(
                id=organization_id,
                name=f"Synthetic live organization {run_id}",
                created_by_user_id=recruiter["id"],
            )
        )
        session.flush()
        session.add_all(
            [
                OrganizationMembershipRecord(
                    organization_id=organization_id,
                    user_id=recruiter["id"],
                    role="recruiter",
                ),
                OrganizationMembershipRecord(
                    organization_id=organization_id,
                    user_id=candidate_user["id"],
                    role="candidate",
                ),
                CandidateRecord(
                    id=candidate_id,
                    name="Synthetic Demo Candidate",
                    email=candidate_email,
                    organization_id=organization_id,
                    owner_user_id=recruiter["id"],
                    user_id=candidate_user["id"],
                ),
            ]
        )

    tracking_provider = _TrackingProvider(provider)
    monkeypatch.setattr(
        interview_service_module,
        "provider_from_settings",
        lambda _settings: tracking_provider,
    )

    service = InterviewService(redis_client=_MemoryRedis())
    state = service.start_interview(
        candidate_id,
        "AI Engineer",
        owner_user_id=recruiter["id"],
        owner_role="recruiter",
        organization_id=organization_id,
    )
    interview_id = state["interview_id"]

    with SessionLocal.begin() as session:
        record = session.get(InterviewRecord, interview_id)
        if record is None:
            pytest.fail("New interview was not persisted.")
        persisted = json.loads(record.state_json)
        persisted["current_competency"] = "python"
        persisted["interview_plan"]["competencies"] = ["python", "distributed_systems"]
        persisted["interview_plan"]["question_budget"] = 2
        persisted["question_budget"] = 2
        record.state_json = json.dumps(persisted)
        first_question = session.execute(
            select(InterviewQuestionRecord).where(
                InterviewQuestionRecord.interview_id == interview_id
            )
        ).scalar_one()
        first_question.competency = "python"
    return (
        service,
        recruiter,
        candidate_user,
        interview_id,
        state["current_question"],
        tracking_provider,
        organization_id,
    )


def _submit_two_answers(
    service: Any,
    candidate: dict[str, str],
    interview_id: str,
    initial_question: str,
    tracking_provider: Any,
    run_id: str,
    deadline: float,
    organization_id: str,
) -> None:
    answers = (
        "I built a Python service that used asyncio to keep outbound requests responsive, "
        "bounded concurrency to protect the dependency, and propagated cancellation.",
        "For a payment API, I stored an idempotency key with the transaction so a retry "
        "could not apply the write twice; I measured duplicate writes under concurrent retries.",
    )
    previous_question = initial_question
    for index, answer in enumerate(answers, start=1):
        structured_before = tracking_provider.structured_successes
        state = _bounded_call(
            deadline,
            f"evaluated answer {index}",
            lambda answer=answer, index=index: service.submit_answer(
                interview_id,
                answer,
                f"live-turn-{run_id}-{index}",
                candidate["id"],
                "candidate",
                organization_id,
            ),
        )
        replay = _bounded_call(
            deadline,
            f"idempotent answer replay {index}",
            lambda answer=answer, index=index: service.submit_answer(
                interview_id,
                answer,
                f"live-turn-{run_id}-{index}",
                candidate["id"],
                "candidate",
                organization_id,
            ),
        )
        if replay.get("history") != state.get("history"):
            pytest.fail(f"Answer replay {index} changed the persisted transcript.")
        from services.interview_service import InterviewConflictError

        with pytest.raises(InterviewConflictError):
            service.submit_answer(
                interview_id,
                answer + " This different answer must not replace the original turn.",
                f"live-turn-{run_id}-{index}",
                candidate["id"],
                "candidate",
                organization_id,
            )
        if state.get("evaluation", {}).get("evaluator_type") != "llm":
            pytest.fail(f"Answer {index} was not evaluated by the configured live LLM.")
        if index == 1:
            if tracking_provider.structured_successes - structured_before < 2:
                pytest.fail(
                    "The first turn did not successfully use the live model for evaluation "
                    "and adaptive question generation."
                )
            if state.get("current_question") == previous_question:
                pytest.fail("The adaptive graph repeated the initial interview question.")
            if any(
                "question generation fallback" in error
                for error in state.get("errors", [])
            ):
                pytest.fail("The adaptive question used the local fallback, not the live model.")
        previous_question = state.get("current_question", previous_question)
        expected_status = "active" if index == 1 else "completed"
        if state.get("status") != expected_status:
            pytest.fail(f"Interview status after answer {index} was not {expected_status}.")


def _assert_resource_access(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    interview_id: str,
    recruiter_email: str,
    candidate_email: str,
    organization_id: str,
) -> dict[str, Any]:
    from fastapi.testclient import TestClient

    from apps.api import main as api_main
    from apps.api.main import app
    from services.auth_service import issue_token
    from services.database import OrganizationMembershipRecord, SessionLocal
    from services.report_service import ReportService
    from services.user_store import UserStore

    class RedisClientStub:
        _client = None

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(api_main, "RedisClient", RedisClientStub)
    monkeypatch.setattr(
        "apps.api.v1.routes.interview.report_service",
        ReportService(output_dir=str(tmp_path / "reports")),
    )
    recruiter_token = issue_token(recruiter_email, role="recruiter")
    candidate_token = issue_token(candidate_email, role="candidate")
    outsider_recruiter = UserStore.create(
        f"outsider-recruiter-{uuid4().hex}@example.invalid",
        uuid4().hex,
        role="candidate",
    )
    outsider_candidate = UserStore.create(
        f"outsider-candidate-{uuid4().hex}@example.invalid",
        uuid4().hex,
        role="candidate",
    )
    outsider_recruiter_token = issue_token(outsider_recruiter["email"], role="recruiter")
    outsider_candidate_token = issue_token(outsider_candidate["email"], role="candidate")
    with SessionLocal.begin() as session:
        session.add_all(
            [
                OrganizationMembershipRecord(
                    organization_id=organization_id,
                    user_id=outsider_recruiter["id"],
                    role="recruiter",
                ),
                OrganizationMembershipRecord(
                    organization_id=organization_id,
                    user_id=outsider_candidate["id"],
                    role="candidate",
                ),
            ]
        )

    def scoped_headers(token: str) -> dict[str, str]:
        return {
            "Authorization": "Bearer " + token,
            "X-Organization-ID": organization_id,
        }

    with TestClient(app) as client:
        recruiter_headers = scoped_headers(recruiter_token)
        candidate_headers = scoped_headers(candidate_token)
        assert client.get(
            f"/api/v1/interview/{interview_id}", headers=candidate_headers
        ).status_code == 200
        assert client.post(
            f"/api/v1/interview/{interview_id}/report", headers=recruiter_headers
        ).status_code == 200
        assert client.post(
            f"/api/v1/interview/{interview_id}/report", headers=candidate_headers
        ).status_code == 200
        for response in (
            client.get(
                f"/api/v1/interview/{interview_id}",
                headers=scoped_headers(outsider_recruiter_token),
            ),
            client.get(
                f"/api/v1/interview/{interview_id}",
                headers=scoped_headers(outsider_candidate_token),
            ),
            client.post(
                f"/api/v1/interview/{interview_id}/report",
                headers=scoped_headers(outsider_recruiter_token),
            ),
            client.post(
                f"/api/v1/interview/{interview_id}/report?format=txt",
                headers=scoped_headers(outsider_recruiter_token),
            ),
        ):
            assert response.status_code == 404
        return client.post(
            f"/api/v1/interview/{interview_id}/report",
            headers=recruiter_headers,
        ).json()["report"]


def _retrieve_report(
    monkeypatch: pytest.MonkeyPatch, interview_id: str, recruiter_email: str
) -> dict[str, Any]:
    from fastapi.testclient import TestClient

    from apps.api import main as api_main
    from apps.api.main import app
    from services.auth_service import issue_token

    class RedisClientStub:
        _client = None

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(api_main, "RedisClient", RedisClientStub)
    token = issue_token(recruiter_email, role="recruiter")
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/interview/{interview_id}/report",
            headers={"Authorization": f"Bearer {token}"},
        )
    if response.status_code != 200:
        pytest.fail(f"Authorized report API returned HTTP {response.status_code}.")
    return response.json()["report"]


def _assert_persisted_results(interview_id: str, report: dict[str, Any]) -> tuple[int, int]:
    from sqlalchemy import select

    from services.database import (
        InterviewAssessmentRecord,
        InterviewQuestionRecord,
        InterviewTurnRecord,
        SessionLocal,
    )

    with SessionLocal() as session:
        turns = list(
            session.execute(
                select(InterviewTurnRecord).where(
                    InterviewTurnRecord.interview_id == interview_id
                )
            ).scalars()
        )
        assessments = list(
            session.execute(
                select(InterviewAssessmentRecord).where(
                    InterviewAssessmentRecord.interview_id == interview_id
                )
            ).scalars()
        )
        questions = list(
            session.execute(
                select(InterviewQuestionRecord).where(
                    InterviewQuestionRecord.interview_id == interview_id
                )
            ).scalars()
        )
    if len(turns) != 2 or any(turn.status != "completed" for turn in turns):
        pytest.fail("Persisted transcript does not contain two completed synthetic turns.")
    if len(assessments) != 2 or not report["supporting_evidence"]:
        pytest.fail("Persisted evaluations or supporting evidence are incomplete.")
    if len(questions) != 2:
        pytest.fail("Idempotent retries created a duplicate interview question.")
    if not report["is_final"] or report["evaluation_status"] != "complete":
        pytest.fail("Report did not reflect a final, fully evaluated interview.")
    return len(turns), len(assessments)


@pytest.mark.live
@pytest.mark.e2e
@pytest.mark.timeout(TOTAL_RUNTIME_BUDGET_SECONDS + 60)
def test_live_interview_reaches_report_api_with_persisted_ai_results(monkeypatch, tmp_path):
    _live_environment(monkeypatch, tmp_path)
    started = time.monotonic()
    deadline = started + TOTAL_RUNTIME_BUDGET_SECONDS
    root = Path(__file__).resolve().parents[2]
    _apply_migrations(root, deadline)
    provider, indexed, retrieval = _index_corpus_and_retrieve(deadline, monkeypatch)
    run_id = uuid4().hex
    (
        service,
        recruiter,
        candidate,
        interview_id,
        initial_question,
        tracking_provider,
        organization_id,
    ) = _create_interview(run_id, provider, monkeypatch)
    _submit_two_answers(
        service,
        candidate,
        interview_id,
        initial_question,
        tracking_provider,
        run_id,
        deadline,
        organization_id,
    )
    report = _bounded_call(
        deadline,
        "candidate and unauthorized-user access checks",
        lambda: _assert_resource_access(
            monkeypatch,
            tmp_path,
            interview_id,
            recruiter["email"],
            candidate["email"],
            organization_id,
        ),
    )
    report = _bounded_call(
        deadline,
        "authorized recruiter report API",
        lambda: _retrieve_report(
            monkeypatch,
            interview_id,
            recruiter["email"],
        ),
    )
    turn_count, assessment_count = _assert_persisted_results(interview_id, report)
    from config import settings

    logger.info(
        "live interview E2E passed",
        extra={
            "llm_provider": "openai",
            "llm_model": settings.OPENAI_MODEL,
            "embedding_provider": provider.__class__.__name__,
            "embedding_model": provider.model,
            "embedding_dimension": provider.dimension,
            "indexed_chunk_count": indexed,
            "dense_retrieval_candidates": retrieval.dense_count,
            "answer_turn_count": turn_count,
            "assessment_count": assessment_count,
            "llm_request_count": tracking_provider.request_count,
            "embedding_api_call_count": provider.api_call_count,
            "http_authorization_checks": "recruiter_candidate_and_unauthorized_users",
            "report_status": report["evaluation_status"],
            "runtime_seconds": round(time.monotonic() - started, 2),
            "status": "passed",
        },
    )
