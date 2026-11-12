from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_domain import payment

from payments.domain.models import PaymentAttempt, ProviderResult, cascade_decision


def active(sequence=1):
    p = payment()
    p = p.transition("processing", p.created_at)
    key = uuid4()
    a = PaymentAttempt(
        id=key,
        payment_id=p.id,
        sequence=sequence,
        provider=p.routing_snapshot[sequence - 1],
        provider_idempotency_key=key,
        created_at=p.created_at,
        updated_at=p.created_at,
        safe_retry_until=p.created_at + timedelta(days=1),
    ).claim(p.created_at, p.created_at + timedelta(seconds=10))
    return p, a


@pytest.mark.parametrize(
    "status,code,expected,next_provider",
    [
        ("succeeded", None, "succeeded", None),
        ("declined_retryable", "issuer_unavailable", "processing", "B"),
        ("declined_terminal", "invalid_payment_method", "failed", None),
        ("not_charged_final", "not_charged_final", "processing", "B"),
        ("pending", None, "unknown", None),
        ("unknown", None, "unknown", None),
        ("not_found", None, "unknown", None),
    ],
)
@pytest.mark.parametrize("sequence", [1, 2])
def test_policy(status, code, expected, next_provider, sequence):
    p, a = active(sequence)
    r = ProviderResult(status=status, error_code=code, external_id=uuid4())
    if sequence == 2 and next_provider:
        expected, next_provider = "failed", None
    assert cascade_decision(p, a, r) == (expected, next_provider)
    done = a.finish(1, p.created_at, r, p.created_at, p.created_at)
    assert done.status == r.attempt_status
    assert done.error_code == code
    with pytest.raises(ValueError):
        done.finish(1, p.created_at, r, p.created_at, p.created_at)


@pytest.mark.parametrize("status", ["pending", "unknown", "succeeded", "failed"])
def test_inactive_payment_cannot_create_next(status):
    p, a = active()
    p = type(p)(**(p.model_dump() | {"status": status}))
    with pytest.raises(ValueError):
        cascade_decision(
            p,
            a,
            ProviderResult(
                status="declined_retryable", error_code="issuer_unavailable", external_id=uuid4()
            ),
        )


@pytest.mark.parametrize("status", ["created", "unknown", "succeeded", "declined"])
def test_inactive_attempt_cannot_create_next(status):
    p, a = active()
    a = type(a)(**(a.model_dump() | {"status": status, "external_id": uuid4()}))
    with pytest.raises(ValueError):
        cascade_decision(
            p,
            a,
            ProviderResult(
                status="not_charged_final", error_code="not_charged_final", external_id=uuid4()
            ),
        )


@pytest.mark.parametrize(
    "status,code",
    [
        ("declined_retryable", None),
        ("declined_terminal", "issuer_unavailable"),
        ("succeeded", "not_charged_final"),
        ("not_charged_final", None),
        ("not_found", "not_charged_final"),
        ("garbage", None),
    ],
)
def test_invalid_result(status, code):
    with pytest.raises(ValidationError):
        ProviderResult(status=status, error_code=code, external_id=uuid4())


@pytest.mark.parametrize("version,seconds", [(0, 0), (1, 10), (2, 0)])
def test_decline_requires_current_owner(version, seconds):
    p, a = active()
    r = ProviderResult(
        status="not_charged_final", error_code="not_charged_final", external_id=uuid4()
    )
    with pytest.raises(ValueError):
        a.finish(version, p.created_at + timedelta(seconds=seconds), r, p.created_at, p.created_at)


def test_attempt_and_route_must_belong_to_payment():
    p, a = active()
    r = ProviderResult(
        status="declined_retryable", error_code="issuer_unavailable", external_id=uuid4()
    )
    for values in [{"provider": "B"}, {"payment_id": uuid4()}]:
        other = type(a)(**(a.model_dump() | values))
        with pytest.raises(ValueError):
            cascade_decision(p, other, r)
    with pytest.raises(ValidationError):
        a.provider_idempotency_key = uuid4()
    with pytest.raises(ValidationError):
        p.amount_minor = 5


def test_saved_snapshot_round_trip_is_immutable():
    p, a = active()
    loaded = type(p).model_validate_json(p.model_dump_json())
    assert loaded.routing_snapshot == ("A", "B")
    with pytest.raises(ValidationError):
        loaded.routing_snapshot = ("B", "A")
    r = ProviderResult(
        status="declined_retryable", error_code="issuer_unavailable", external_id=uuid4()
    )
    assert cascade_decision(loaded, a, r) == ("processing", loaded.routing_snapshot[1])
