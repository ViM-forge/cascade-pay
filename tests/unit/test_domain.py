from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from payments.domain.models import Payment, PaymentAttempt, ProviderResult
from payments.presentation.dto import CreatePayment


def payment():
    now = datetime.now(UTC)
    return Payment(
        id=uuid4(),
        merchant_id=uuid4(),
        amount_minor=100,
        currency="USD",
        payment_method_ref="demo",
        created_at=now,
        updated_at=now,
    )


def test_terminal_cannot_be_overwritten():
    p = payment()
    now = p.created_at
    p = p.transition("processing", now).transition("succeeded", now)
    with pytest.raises(ValueError):
        p.transition("unknown", now)
    with pytest.raises(ValidationError):
        p.status = "processing"


def test_attempt_ownership():
    p = payment()
    now = p.created_at
    identity = uuid4()
    a = PaymentAttempt(
        id=identity,
        payment_id=p.id,
        provider_idempotency_key=identity,
        safe_retry_until=now + timedelta(days=1),
        created_at=now,
        updated_at=now,
    )
    a = a.claim(now, now + timedelta(seconds=10))
    for version, clock in [(0, now), (1, now + timedelta(seconds=10))]:
        with pytest.raises(ValueError):
            a.finish(
                version,
                clock,
                ProviderResult(status="succeeded", external_id=uuid4()),
                clock,
                clock,
            )
    a = a.finish(1, now, ProviderResult(status="succeeded", external_id=uuid4()), now, now)
    assert a.status == "succeeded"
    with pytest.raises(ValueError):
        a.claim(now, now + timedelta(seconds=10))


@pytest.mark.parametrize("value", [True, 1.0, "1", 0, -1, 9223372036854775808])
def test_strict_amount(value):
    with pytest.raises(ValidationError):
        CreatePayment(amount_minor=value, currency="USD", payment_method_ref="demo")


@pytest.mark.parametrize("ref", ["", "x y", "\u200b", "\ud800", "a" * 256])
def test_strict_ref(ref):
    with pytest.raises(ValidationError):
        CreatePayment(amount_minor=1, currency="USD", payment_method_ref=ref)


def test_unknown_blocks_new_claim_and_false_failure():
    p = payment()
    now = p.created_at
    p = p.transition("processing", now).transition("unknown", now)
    with pytest.raises(ValueError):
        p.transition("failed", now)
    identity = uuid4()
    a = PaymentAttempt(
        id=identity,
        payment_id=p.id,
        provider_idempotency_key=identity,
        safe_retry_until=now + timedelta(days=1),
        created_at=now,
        updated_at=now,
    )
    a = a.claim(now, now + timedelta(seconds=10)).finish(
        1,
        now,
        ProviderResult(status="unknown"),
        now + timedelta(seconds=60),
        now + timedelta(seconds=1),
    )
    assert a.status == "unknown" and a.next_check_at == now + timedelta(seconds=1)
    with pytest.raises(ValueError):
        a.claim(now, now + timedelta(seconds=10))


def test_identity_not_mutable_snapshot_equality():
    p = payment()
    changed = p.transition("processing", p.created_at)
    assert p == changed and hash(p) == hash(changed)
    with pytest.raises(ValidationError):
        Payment(**(p.model_dump() | {"routing_snapshot": ("B", "A")}))


def test_canonical_hash_unicode_and_boundaries():
    import hashlib

    from payments.domain.models import request_hash

    canonical = '{"amount_minor":9223372036854775807,"currency":"USD","payment_method_ref":"é"}'
    assert (
        request_hash(9223372036854775807, "USD", "é")
        == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    )
    assert request_hash(1, "USD", "é") != request_hash(1, "USD", "e\u0301")
