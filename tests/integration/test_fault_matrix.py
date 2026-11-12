"""DB fencing composition. Process/HTTP crashes run separately via D6, never pytest."""

from datetime import timedelta
from uuid import uuid4

import pytest
from test_first_payment import pytestmark  # noqa: F401
from test_recovery import isolated, seed

from payments.domain.models import ProviderResult


@pytest.mark.parametrize(
    "status,code,expected",
    [("succeeded", None, "succeeded"), ("declined_terminal", "invalid_payment_method", "failed")],
)
async def test_stale_result_then_canonical_lookup(status, code, expected):
    # No financial HTTP proof claimed: the immutable provider outcome is a fixture.
    async with isolated() as (session, repo):
        identity, row = await seed(session, repo, "live")
        from payments.infrastructure.repository import attempt_entity

        old = attempt_entity(row)
        outcome = ProviderResult(status=status, error_code=code, external_id=uuid4())
        row.lease_until = row.updated_at - timedelta(seconds=1)
        await session.flush()
        assert await repo.recover_batch() == 1
        assert row.claim_version == old.claim_version + 1
        assert row.status == "unknown"
        assert not await repo.finish(old, outcome)
        _, owner = await repo.claim_reconciliation(identity)
        assert (
            owner.id == old.id and owner.provider_idempotency_key == old.provider_idempotency_key
        )
        assert owner.safe_retry_until == old.safe_retry_until
        assert await repo.finish(owner, outcome)
        canonical = row.status, row.external_id, row.claim_version, row.check_count
        assert not await repo.finish(old, outcome)
        assert canonical == (row.status, row.external_id, row.claim_version, row.check_count)
        payment, _ = await repo.locked(identity)
        assert payment.status == expected


async def test_fifth_expired_response_before_recovery_keeps_version_then_manual():
    async with isolated() as (session, repo):
        identity, row = await seed(session, repo, "unknown")
        row.check_count = 4  # SQL fixture; this test makes no HTTP claims.
        await session.flush()
        _, owner = await repo.claim_reconciliation(identity)
        assert row.check_count == 5
        row.lease_until = row.updated_at - timedelta(seconds=1)
        await session.flush()
        version, deadline, window = (
            row.claim_version,
            row.reconciliation_deadline,
            row.safe_retry_until,
        )
        assert not await repo.finish(
            owner, ProviderResult(status="succeeded", external_id=uuid4())
        )
        assert row.claim_version == version and not row.manual_review_required
        assert await repo.recover_batch() == 1
        assert row.manual_review_required and row.check_count == 5
        assert row.claim_version == version + 1
        assert row.reconciliation_deadline == deadline and row.safe_retry_until == window
        assert row.next_check_at is row.lease_until is None
        assert await repo.claim_reconciliation(identity) is None
        assert await repo.recover_batch() == 0
