from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from payments.domain.models import PaymentAttempt


def created():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    key = uuid4()
    return PaymentAttempt(
        id=key,
        payment_id=uuid4(),
        provider_idempotency_key=key,
        safe_retry_until=now,
        created_at=now - timedelta(days=1),
        updated_at=now - timedelta(days=1),
    )


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_first_claim_window_boundary(offset):
    attempt = created()
    now = attempt.safe_retry_until + timedelta(microseconds=offset)
    if offset < 0:
        claimed = attempt.claim(now, now + timedelta(seconds=10))
        assert claimed.status == "in_flight"
        assert claimed.id == attempt.id
    else:
        with pytest.raises(ValueError, match="cannot be claimed"):
            attempt.claim(now, now + timedelta(seconds=10))


@pytest.mark.parametrize("offset", [0, 1])
def test_expired_first_send_schedules_lookup_without_new_identity(offset):
    attempt = created()
    now = attempt.safe_retry_until + timedelta(microseconds=offset)
    deadline = now + timedelta(seconds=60)
    due = now + timedelta(seconds=1)
    unknown = attempt.expire_before_send(now, deadline, due)
    assert unknown.status == "unknown"
    assert unknown.id == unknown.provider_idempotency_key == attempt.id
    assert unknown.safe_retry_until == attempt.safe_retry_until
    assert unknown.external_id is None
    assert unknown.lease_until is None
    assert unknown.claim_version == attempt.claim_version + 1
    assert unknown.reconciliation_deadline == deadline
    assert unknown.next_check_at == due
    with pytest.raises(ValueError):
        unknown.expire_before_send(now, deadline, due)
    with pytest.raises(ValueError):
        unknown.claim(now, now + timedelta(seconds=10))


def test_unexpired_first_send_cannot_be_expired():
    attempt = created()
    now = attempt.safe_retry_until - timedelta(microseconds=1)
    with pytest.raises(ValueError):
        attempt.expire_before_send(now, now + timedelta(seconds=60), now + timedelta(seconds=1))


def test_expiring_send_window_preserves_existing_reconciliation_history():
    attempt = created()
    old_deadline = attempt.safe_retry_until - timedelta(seconds=1)
    old_due = old_deadline - timedelta(seconds=1)
    attempt = PaymentAttempt(
        **(
            attempt.model_dump()
            | dict(reconciliation_deadline=old_deadline, next_check_at=old_due)
        )
    )
    now = attempt.safe_retry_until
    unknown = attempt.expire_before_send(
        now, now + timedelta(seconds=60), now + timedelta(seconds=1)
    )
    assert unknown.reconciliation_deadline == old_deadline
    assert unknown.next_check_at == old_due
