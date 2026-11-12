"""Controlled-clock reconciliation policy, independently of DB/HTTP timing."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from payments.domain.models import Payment, PaymentAttempt, ProviderResult, cascade_decision

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def unknown(**changes):
    key = uuid4()
    values = dict(
        id=key,
        payment_id=uuid4(),
        provider_idempotency_key=key,
        status="unknown",
        safe_retry_until=NOW + timedelta(days=1),
        reconciliation_deadline=NOW + timedelta(seconds=60),
        next_check_at=NOW + timedelta(seconds=1),
        created_at=NOW,
        updated_at=NOW,
    )
    return PaymentAttempt(**(values | changes))


def test_initial_due_and_deadline_are_bounded():
    a = unknown(status="created", reconciliation_deadline=None, next_check_at=None)
    claimed = a.claim(NOW, NOW + timedelta(seconds=10))
    deadline = NOW + timedelta(milliseconds=500)
    done = claimed.finish(
        1, NOW, ProviderResult(status="unknown"), deadline, NOW + timedelta(seconds=1)
    )
    assert done.next_check_at == deadline
    assert done.check_count == 0
    expired = a.expire_before_send(
        a.safe_retry_until,
        a.safe_retry_until + timedelta(milliseconds=500),
        a.safe_retry_until + timedelta(seconds=1),
    )
    assert expired.next_check_at == expired.reconciliation_deadline


@pytest.mark.parametrize("count,delay", [(1, 2), (2, 4), (3, 8), (4, 16)])
def test_backoff_from_result_preserves_deadline_and_count(count, delay):
    a = unknown(check_count=count - 1, next_check_at=NOW)
    claimed = a.claim_reconciliation(NOW, NOW + timedelta(seconds=10))
    result_at = NOW + timedelta(seconds=1)
    done = claimed.finish(
        claimed.claim_version,
        result_at,
        ProviderResult(status="unknown"),
        NOW + timedelta(days=1),
        NOW,
    )
    assert done.status == "unknown"
    assert done.check_count == count
    assert done.next_check_at == result_at + timedelta(seconds=delay)
    assert done.reconciliation_deadline == a.reconciliation_deadline
    assert done.safe_retry_until == a.safe_retry_until
    assert done.lease_until is None


def test_backoff_never_exceeds_deadline():
    a = unknown(
        check_count=3, next_check_at=NOW, reconciliation_deadline=NOW + timedelta(seconds=2)
    )
    claimed = a.claim_reconciliation(NOW, NOW + timedelta(seconds=10))
    done = claimed.finish(
        claimed.claim_version,
        NOW + timedelta(seconds=1),
        ProviderResult(status="unknown"),
        NOW + timedelta(days=1),
        NOW,
    )
    assert done.next_check_at == a.reconciliation_deadline


@pytest.mark.parametrize(
    "changes,seconds,action",
    [
        ({}, 0, "noop"),
        ({}, 1, "claim"),
        ({"check_count": 5}, 0, "manual"),
        ({"reconciliation_deadline": NOW}, 0, "manual"),
        ({"manual_review_required": True, "check_count": 5}, 100, "noop"),
        ({"lease_until": NOW + timedelta(seconds=10), "check_count": 5}, 5, "noop"),
        ({"lease_until": NOW + timedelta(seconds=10), "reconciliation_deadline": NOW}, 5, "noop"),
        ({"lease_until": NOW + timedelta(seconds=10), "check_count": 5}, 10, "manual"),
        ({"lease_until": NOW + timedelta(seconds=1)}, 1, "claim"),
        ({"next_check_at": NOW + timedelta(days=1), "check_count": 5}, 0, "manual"),
    ],
)
def test_claim_order_and_boundaries(changes, seconds, action):
    a = unknown(**changes)
    assert a.reconciliation_action(NOW + timedelta(seconds=seconds)) == action


@pytest.mark.parametrize("terminal", ["succeeded", "declined"])
def test_terminal_is_noop_before_budget(terminal):
    a = unknown(status=terminal, external_id=uuid4(), check_count=5)
    assert a.reconciliation_action(NOW + timedelta(days=1)) == "noop"


def test_exhausted_claim_invalidates_previous_owner_and_stays_unknown():
    a = unknown(check_count=5, claim_version=4, lease_until=NOW)
    done = a.stop_reconciliation(NOW)
    assert done.status == "unknown"
    assert done.manual_review_required
    assert done.next_check_at is done.lease_until is None
    assert done.claim_version == 5
    assert done.check_count == 5
    assert done.reconciliation_deadline == a.reconciliation_deadline
    assert done.reconciliation_action(NOW + timedelta(days=100)) == "noop"
    with pytest.raises(ValueError):
        done.claim_reconciliation(NOW + timedelta(days=100), NOW + timedelta(days=101))


@pytest.mark.parametrize(
    "status,code",
    [
        ("succeeded", None),
        ("declined_retryable", "issuer_unavailable"),
        ("declined_terminal", "invalid_payment_method"),
        ("not_charged_final", "not_charged_final"),
    ],
)
def test_fifth_live_owner_can_finish_after_deadline(status, code):
    a = unknown(
        check_count=4, next_check_at=NOW, reconciliation_deadline=NOW + timedelta(seconds=1)
    )
    claimed = a.claim_reconciliation(NOW, NOW + timedelta(seconds=10))
    result = ProviderResult(status=status, error_code=code, external_id=uuid4())
    done = claimed.finish(
        claimed.claim_version, NOW + timedelta(seconds=2), result, NOW + timedelta(days=1), NOW
    )
    assert done.status == result.attempt_status
    assert done.check_count == 5
    assert done.reconciliation_deadline == a.reconciliation_deadline
    assert done.next_check_at is done.lease_until is None
    assert not done.manual_review_required


@pytest.mark.parametrize("status", ["pending", "unknown", "not_found"])
def test_fifth_unresolved_result_stops_without_failure(status):
    a = unknown(check_count=4, next_check_at=NOW)
    claimed = a.claim_reconciliation(NOW, NOW + timedelta(seconds=10))
    result = ProviderResult(status=status, external_id=uuid4() if status == "pending" else None)
    done = claimed.finish(claimed.claim_version, NOW, result, NOW, NOW)
    assert done.status == "unknown" and done.manual_review_required
    assert done.check_count == 5 and done.next_check_at is None


@pytest.mark.parametrize("offset,allowed", [(-1, True), (0, False), (1, False)])
def test_safe_retry_boundary(offset, allowed):
    a = unknown(next_check_at=NOW, safe_retry_until=NOW + timedelta(seconds=5))
    claimed = a.claim_reconciliation(NOW, NOW + timedelta(seconds=10))
    now = a.safe_retry_until + timedelta(microseconds=offset)
    assert claimed.may_retry_charge(claimed.claim_version, now) is allowed


@pytest.mark.parametrize("version,seconds", [(0, 0), (2, 0), (1, 10), (1, 11)])
def test_foreign_or_expired_response_cannot_be_saved(version, seconds):
    a = unknown(next_check_at=NOW).claim_reconciliation(NOW, NOW + timedelta(seconds=10))
    with pytest.raises(ValueError):
        a.finish(
            version,
            NOW + timedelta(seconds=seconds),
            ProviderResult(status="succeeded", external_id=uuid4()),
            NOW,
            NOW,
        )


@pytest.mark.parametrize("sequence", [1, 2])
@pytest.mark.parametrize(
    "status,code,expected",
    [
        ("succeeded", None, "succeeded"),
        ("declined_retryable", "issuer_unavailable", "processing"),
        ("not_charged_final", "not_charged_final", "processing"),
        ("declined_terminal", "invalid_payment_method", "failed"),
        ("pending", None, "unknown"),
        ("unknown", None, "unknown"),
        ("not_found", None, "unknown"),
    ],
)
def test_same_payment_policy_resolves_unknown(sequence, status, code, expected):
    p = Payment(
        id=uuid4(),
        merchant_id=uuid4(),
        amount_minor=1,
        currency="USD",
        payment_method_ref="limits",
        status="unknown",
        created_at=NOW,
        updated_at=NOW,
    )
    a = unknown(payment_id=p.id, sequence=sequence, provider=p.routing_snapshot[sequence - 1])
    r = ProviderResult(status=status, error_code=code, external_id=uuid4())
    if sequence == 2 and expected == "processing":
        expected = "failed"
    updated, next_provider = p.apply_result(a, r, NOW)
    assert updated.status == expected
    assert cascade_decision(p, a, r) == (expected, next_provider)
    assert next_provider == ("B" if expected == "processing" else None)
    if status in {"unknown", "pending", "not_found"}:
        with pytest.raises(ValueError):
            p.transition("failed", NOW)
