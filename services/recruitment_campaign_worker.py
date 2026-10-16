"""Database-polled dispatcher for durable recruitment campaign executions."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import UTC, datetime, timedelta
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from config import settings
from integrations.llm_providers.provider import LLMProvider
from services.candidate_application_service import (
    ApplicationResponse,
    CandidateApplicationService,
)
from services.candidate_availability_service import CandidateAvailabilityService
from services.candidate_communication_service import (
    CandidateCommunicationService,
    CommunicationResponse,
)
from services.database import (
    CAMPAIGN_WORKER_CONTROL_ID,
    CandidateCommunicationRecord,
    RecruitmentApplicationRecord,
    RecruitmentCampaignExecutionRecord,
    RecruitmentCampaignWorkerControlRecord,
    SessionLocal,
)
from services.interview_scheduling_service import InterviewSchedulingService
from services.recruitment_campaign_orchestrator import (
    CampaignAdvancementResponse,
    CampaignExecutionResponse,
    RecruitmentCampaignOrchestrator,
)

logger = logging.getLogger(__name__)
CONTROL_ID = CAMPAIGN_WORKER_CONTROL_ID


def _control_record(session) -> RecruitmentCampaignWorkerControlRecord:
    control = session.get(RecruitmentCampaignWorkerControlRecord, CONTROL_ID)
    if control is None:
        control = RecruitmentCampaignWorkerControlRecord(
            id=CONTROL_ID,
            paused=False,
            updated_at=datetime.now(UTC),
        )
        session.add(control)
        session.flush()
    return control


def set_worker_paused(paused: bool) -> None:
    with SessionLocal.begin() as session:
        control = _control_record(session)
        control.paused = paused
        control.updated_at = datetime.now(UTC)


def worker_status() -> dict[str, object]:
    now = datetime.now(UTC)
    with SessionLocal() as session:
        control = session.get(RecruitmentCampaignWorkerControlRecord, CONTROL_ID)
        counts = dict(
            session.execute(
                select(
                    RecruitmentCampaignExecutionRecord.status,
                    func.count(RecruitmentCampaignExecutionRecord.id),
                ).group_by(RecruitmentCampaignExecutionRecord.status)
            ).all()
        )
        application_counts = dict(
            session.execute(
                select(
                    RecruitmentApplicationRecord.status,
                    func.count(RecruitmentApplicationRecord.id),
                ).group_by(RecruitmentApplicationRecord.status)
            ).all()
        )
        heartbeat = None if control is None else control.heartbeat_at
        if heartbeat is not None and heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=UTC)
        stale_after = timedelta(
            seconds=max(settings.CAMPAIGN_WORKER_POLL_SECONDS * 3, 10)
        )
        healthy = heartbeat is not None and now - heartbeat <= stale_after
        return {
            "healthy": healthy,
            "ready": healthy and not (False if control is None else control.paused),
            "paused": False if control is None else control.paused,
            "heartbeat_stale": not healthy,
            "heartbeat_at": (
                None if heartbeat is None else heartbeat.isoformat()
            ),
            "last_error": None if control is None else control.last_error,
            "execution_counts": counts,
            "application_counts": application_counts,
            "communication_counts": dict(
                session.execute(
                    select(
                        CandidateCommunicationRecord.status,
                        func.count(CandidateCommunicationRecord.id),
                    ).group_by(CandidateCommunicationRecord.status)
                ).all()
            ),
            "retryable_due": session.execute(
                select(func.count(RecruitmentCampaignExecutionRecord.id)).where(
                    RecruitmentCampaignExecutionRecord.status == "retryable_failure",
                    RecruitmentCampaignExecutionRecord.available_at <= now,
                )
            ).scalar_one(),
            "expired_leases": session.execute(
                select(func.count(RecruitmentCampaignExecutionRecord.id)).where(
                    RecruitmentCampaignExecutionRecord.status == "running",
                    RecruitmentCampaignExecutionRecord.lease_expires_at <= now,
                )
            ).scalar_one(),
            "application_retryable_due": session.execute(
                select(func.count(RecruitmentApplicationRecord.id)).where(
                    RecruitmentApplicationRecord.status == "retryable_failure",
                    RecruitmentApplicationRecord.available_at <= now,
                )
            ).scalar_one(),
            "application_expired_leases": session.execute(
                select(func.count(RecruitmentApplicationRecord.id)).where(
                    RecruitmentApplicationRecord.status == "running",
                    RecruitmentApplicationRecord.lease_expires_at <= now,
                )
            ).scalar_one(),
            "communication_retryable_due": session.execute(
                select(func.count(CandidateCommunicationRecord.id)).where(
                    CandidateCommunicationRecord.status == "retryable_failure",
                    CandidateCommunicationRecord.available_at <= now,
                )
            ).scalar_one(),
            "communication_expired_leases": session.execute(
                select(func.count(CandidateCommunicationRecord.id)).where(
                    CandidateCommunicationRecord.status == "sending",
                    CandidateCommunicationRecord.lease_expires_at <= now,
                )
            ).scalar_one(),
        }


class RecruitmentCampaignWorker:
    def __init__(
        self,
        *,
        concurrency: int | None = None,
        poll_seconds: float | None = None,
        stop_event: threading.Event | None = None,
        provider: LLMProvider | None = None,
    ) -> None:
        self.concurrency = (
            settings.CAMPAIGN_WORKER_CONCURRENCY
            if concurrency is None
            else concurrency
        )
        self.poll_seconds = (
            settings.CAMPAIGN_WORKER_POLL_SECONDS
            if poll_seconds is None
            else poll_seconds
        )
        if not 1 <= self.concurrency <= 32:
            raise ValueError("Worker concurrency must be between 1 and 32")
        if not 0 < self.poll_seconds <= 60:
            raise ValueError("Worker poll interval must be greater than 0 and at most 60 seconds")
        self.stop_event = stop_event or threading.Event()
        self.provider = provider

    def dispatch_once(
        self,
    ) -> list[
        CampaignExecutionResponse
        | ApplicationResponse
        | CommunicationResponse
        | CampaignAdvancementResponse
    ]:
        """Run one item from each available recruitment workflow."""
        with SessionLocal() as session:
            control = session.get(RecruitmentCampaignWorkerControlRecord, CONTROL_ID)
            if control is not None and control.paused:
                return []
        results: list[
            CampaignExecutionResponse
            | ApplicationResponse
            | CommunicationResponse
            | CampaignAdvancementResponse
        ] = list(
            RecruitmentCampaignOrchestrator.dispatch_once(
                limit=1,
                provider=self.provider,
            )
        )
        application_ids = CandidateApplicationService.discover_eligible(1)
        if application_ids:
            application_result = CandidateApplicationService.process_dispatched(
                application_ids[0]
            )
            if application_result is not None:
                results.append(application_result)
        communication_ids = CandidateCommunicationService.discover_eligible(1)
        if communication_ids:
            communication_result = CandidateCommunicationService.process_dispatched(
                communication_ids[0]
            )
            if communication_result is not None:
                results.append(communication_result)
        results.extend(
            RecruitmentCampaignOrchestrator.advance_eligible_applications(1)
        )
        CandidateAvailabilityService.expire_due(limit=100)
        InterviewSchedulingService.expire_due(limit=100)
        return results

    def run(self) -> None:
        active: dict[Future[object], tuple[str, str]] = {}
        logger.info(
            "Recruitment campaign worker starting",
            extra={
                "concurrency": self.concurrency,
                "poll_seconds": self.poll_seconds,
            },
        )
        with ThreadPoolExecutor(
            max_workers=self.concurrency,
            thread_name_prefix="campaign-worker",
        ) as executor:
            while not self.stop_event.is_set():
                try:
                    paused = self._heartbeat()
                    self._collect_completed(active)
                    if not paused:
                        CandidateAvailabilityService.expire_due(limit=100)
                        InterviewSchedulingService.expire_due(limit=100)
                        capacity = self.concurrency - len(active)
                        if capacity:
                            execution_ids = RecruitmentCampaignOrchestrator.discover_eligible(
                                capacity
                            )
                            for execution_id in execution_ids:
                                future = executor.submit(
                                    RecruitmentCampaignOrchestrator.run_dispatched,
                                    execution_id,
                                    provider=self.provider,
                                )
                                active[future] = ("campaign_execution", execution_id)
                                logger.info(
                                    "Recruitment campaign execution dispatched",
                                    extra={"execution_id": execution_id},
                                )
                            capacity = self.concurrency - len(active)
                            for application_id in CandidateApplicationService.discover_eligible(
                                capacity
                            ):
                                future = executor.submit(
                                    CandidateApplicationService.process_dispatched,
                                    application_id,
                                )
                                active[future] = ("candidate_application", application_id)
                                logger.info(
                                    "Candidate application processing dispatched",
                                    extra={"application_id": application_id},
                                )
                            capacity = self.concurrency - len(active)
                            for communication_id in CandidateCommunicationService.discover_eligible(
                                capacity
                            ):
                                future = executor.submit(
                                    CandidateCommunicationService.process_dispatched,
                                    communication_id,
                                )
                                active[future] = (
                                    "candidate_communication",
                                    communication_id,
                                )
                                logger.info(
                                    "Candidate communication dispatched",
                                    extra={"communication_id": communication_id},
                                )
                            capacity = self.concurrency - len(active)
                            if capacity:
                                RecruitmentCampaignOrchestrator.advance_eligible_applications(
                                    capacity
                                )
                except SQLAlchemyError as exc:
                    self._record_error(exc)
                except Exception as exc:
                    logger.error(
                        "Recruitment campaign worker loop failed",
                        extra={"failure_type": type(exc).__name__},
                    )
                    self._record_error(exc)

                if active:
                    completed, _ = wait(
                        active,
                        timeout=self.poll_seconds,
                        return_when=FIRST_COMPLETED,
                    )
                    self._log_completed(active, completed)
                else:
                    self.stop_event.wait(self.poll_seconds)

            logger.info(
                "Recruitment campaign worker stopping; waiting for active executions",
                extra={"active_executions": len(active)},
            )
            for future in active:
                try:
                    future.result()
                except Exception as exc:
                    work_type, work_id = active[future]
                    logger.error(
                        "Recruitment dispatcher work item failed during shutdown",
                        extra={
                            "work_type": work_type,
                            "work_id": work_id,
                            "failure_type": type(exc).__name__,
                        },
                    )
                    self._record_error(exc)
        logger.info("Recruitment campaign worker stopped")

    @staticmethod
    def _collect_completed(
        active: dict[Future[object], tuple[str, str]],
    ) -> None:
        completed = {future for future in active if future.done()}
        RecruitmentCampaignWorker._log_completed(active, completed)

    @staticmethod
    def _log_completed(
        active: dict[Future[object], tuple[str, str]],
        completed: set[Future[object]],
    ) -> None:
        for future in completed:
            work_type, work_id = active.pop(future)
            try:
                result = future.result()
                logger.info(
                    "Recruitment dispatcher work item finished",
                    extra={
                        "work_type": work_type,
                        "work_id": work_id,
                        "status": (
                            None
                            if result is None
                            else getattr(result, "status", None)
                        ),
                    },
                )
            except Exception as exc:
                logger.error(
                    "Recruitment dispatcher work item raised",
                    extra={
                        "work_type": work_type,
                        "work_id": work_id,
                        "failure_type": type(exc).__name__,
                    },
                )
                RecruitmentCampaignWorker._record_error(exc)

    def _heartbeat(self) -> bool:
        now = datetime.now(UTC)
        with SessionLocal.begin() as session:
            control = _control_record(session)
            control.heartbeat_at = now
            control.last_error = None
            control.updated_at = now
            return control.paused

    @staticmethod
    def _record_error(exc: Exception) -> None:
        logger.error(
            "Recruitment campaign dispatcher could not access required state",
            extra={"failure_type": type(exc).__name__},
        )
        try:
            with SessionLocal.begin() as session:
                control = _control_record(session)
                control.last_error = f"Dispatcher operation failed: {type(exc).__name__}"
                control.updated_at = datetime.now(UTC)
        except SQLAlchemyError as persist_exc:
            logger.error(
                "Could not persist recruitment worker error status",
                extra={"failure_type": type(persist_exc).__name__},
            )


def _run_command() -> None:
    stop_event = threading.Event()

    def request_stop(signum, frame) -> None:
        logger.info("Recruitment worker received shutdown signal", extra={"signal": signum})
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)
    RecruitmentCampaignWorker(stop_event=stop_event).run()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "pause", "resume", "status"):
        subparsers.add_parser(command)
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            _run_command()
        elif args.command == "pause":
            set_worker_paused(True)
            print("Recruitment campaign worker paused.")
        elif args.command == "resume":
            set_worker_paused(False)
            print("Recruitment campaign worker resumed.")
        else:
            state = worker_status()
            print(json.dumps(state, default=str, sort_keys=True))
            return 0 if state["ready"] else 1
    except SQLAlchemyError as exc:
        logger.error(
            "Recruitment campaign worker command could not access the database",
            extra={"failure_type": type(exc).__name__},
        )
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=settings.LOG_LEVEL.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    raise SystemExit(main())
