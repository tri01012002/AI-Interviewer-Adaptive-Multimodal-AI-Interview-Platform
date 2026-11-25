"""Bounded background renewal of database work leases.

A worker that claims a row with ``lease_token`` + ``lease_expires_at`` keeps
that lease alive while a long step (an LLM call, an SMTP send) runs on the
current thread. Renewal is a conditional UPDATE supplied by the caller that
must only succeed while the caller still owns an *unexpired* lease, so it can
never revive work that another worker may already be reclaiming.

Renewal is best-effort: if it fails or ownership is lost, it stops. The final
commit stays fenced by the lease token, so a stale worker still cannot
overwrite the new owner's result. This does not make execution exactly-once.

Each attempt is counted in ``worker_lease_renewals_total{kind,outcome}``
(ok / lost / failed); the work id is never a metric label.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from types import TracebackType

from config import settings
from utils import metrics

logger = logging.getLogger(__name__)

THREAD_NAME_PREFIX = "lease-renewal"


def renewal_interval_seconds(lease_seconds: float | None = None) -> float:
    """Renew at most every lease/3 seconds and never later than lease/2."""
    lease = float(lease_seconds or settings.CAMPAIGN_WORKER_LEASE_SECONDS)
    configured = settings.CAMPAIGN_WORKER_LEASE_RENEWAL_SECONDS
    interval = float(configured) if configured else lease / 3
    return max(0.05, min(interval, lease / 2))


class LeaseRenewal:
    """Context manager running one daemon renewal thread for one claimed item."""

    def __init__(
        self,
        renew: Callable[[], bool],
        *,
        work_type: str,
        work_id: str,
        interval_seconds: float | None = None,
        max_consecutive_failures: int = 3,
    ) -> None:
        self._renew = renew
        self.work_type = work_type
        self.work_id = work_id
        self.interval_seconds = (
            renewal_interval_seconds() if interval_seconds is None else interval_seconds
        )
        self.max_consecutive_failures = max_consecutive_failures
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.renewals = 0
        self.lost = False
        self.stopped_reason: str | None = None

    def __enter__(self) -> LeaseRenewal:
        self._thread = threading.Thread(
            target=self._run,
            name=f"{THREAD_NAME_PREFIX}:{self.work_type}:{self.work_id}",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            # A renewal UPDATE is a single short statement; bound the wait anyway.
            thread.join(timeout=self.interval_seconds + 10)

    def _count(self, outcome: str) -> None:
        try:
            metrics.inc("worker_lease_renewals_total", {"kind": self.work_type, "outcome": outcome})
        except Exception:  # pragma: no cover - metrics must never stop renewal
            logger.debug("lease renewal metric failed")

    def _run(self) -> None:
        failures = 0
        while not self._stop.wait(self.interval_seconds):
            try:
                owned = self._renew()
            except Exception as exc:  # database outage, pool exhaustion, ...
                failures += 1
                self._count("failed")
                logger.warning(
                    "Lease renewal failed",
                    extra={
                        "work_type": self.work_type,
                        "work_id": self.work_id,
                        "failure_type": type(exc).__name__,
                        "consecutive_failures": failures,
                    },
                )
                if failures >= self.max_consecutive_failures:
                    self.lost = True
                    self.stopped_reason = "renewal_failures"
                    return
                continue
            if not owned:
                self._count("lost")
                self.lost = True
                self.stopped_reason = "ownership_lost"
                logger.warning(
                    "Lease ownership lost; result will be fenced",
                    extra={"work_type": self.work_type, "work_id": self.work_id},
                )
                return
            failures = 0
            self.renewals += 1
            self._count("ok")
        self.stopped_reason = self.stopped_reason or "stopped"
