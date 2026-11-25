"""Offline behaviour of the bounded lease-renewal helper (no database)."""

from __future__ import annotations

import threading
import time

import pytest

from config import settings
from services.lease_renewal import THREAD_NAME_PREFIX, LeaseRenewal, renewal_interval_seconds


def _alive_renewal_threads() -> list[str]:
    return [t.name for t in threading.enumerate() if t.name.startswith(THREAD_NAME_PREFIX)]


def test_renews_periodically_while_task_runs_and_stops_on_exit():
    calls = []
    with LeaseRenewal(lambda: calls.append(1) or True, work_type="t", work_id="a", interval_seconds=0.05) as renewal:
        time.sleep(0.4)
    count = len(calls)
    time.sleep(0.2)
    assert count >= 3
    assert len(calls) == count  # nothing renews after the task finished
    assert renewal.renewals == count and not renewal.lost
    assert _alive_renewal_threads() == []


def test_ownership_loss_stops_renewal_and_is_reported():
    calls = []

    def renew():
        calls.append(1)
        return len(calls) < 2

    with LeaseRenewal(renew, work_type="t", work_id="b", interval_seconds=0.05) as renewal:
        time.sleep(0.4)
    assert len(calls) == 2
    assert renewal.lost and renewal.stopped_reason == "ownership_lost"


def test_transient_failures_are_retried_then_give_up_after_limit():
    attempts = []

    def renew():
        attempts.append(1)
        raise ConnectionError("synthetic database outage")

    with LeaseRenewal(
        renew, work_type="t", work_id="c", interval_seconds=0.03, max_consecutive_failures=3
    ) as renewal:
        time.sleep(0.4)
    assert len(attempts) == 3
    assert renewal.lost and renewal.stopped_reason == "renewal_failures"


def test_one_failure_then_success_keeps_renewing():
    outcomes = iter([ConnectionError("blip"), True, True, True, True, True, True, True, True])

    def renew():
        outcome = next(outcomes, True)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    with LeaseRenewal(renew, work_type="t", work_id="d", interval_seconds=0.03) as renewal:
        time.sleep(0.3)
    assert not renewal.lost and renewal.renewals >= 2


def test_exception_inside_task_still_stops_the_thread():
    with pytest.raises(RuntimeError):
        with LeaseRenewal(lambda: True, work_type="t", work_id="e", interval_seconds=0.05):
            raise RuntimeError("task failed")
    assert _alive_renewal_threads() == []


def test_interval_defaults_to_a_third_and_is_clamped_below_half(monkeypatch):
    monkeypatch.setattr(settings, "CAMPAIGN_WORKER_LEASE_RENEWAL_SECONDS", None)
    assert renewal_interval_seconds(120) == pytest.approx(40)
    monkeypatch.setattr(settings, "CAMPAIGN_WORKER_LEASE_RENEWAL_SECONDS", 500)
    assert renewal_interval_seconds(120) == pytest.approx(60)
    monkeypatch.setattr(settings, "CAMPAIGN_WORKER_LEASE_RENEWAL_SECONDS", 10)
    assert renewal_interval_seconds(120) == pytest.approx(10)
