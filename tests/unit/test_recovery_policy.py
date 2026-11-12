from datetime import timedelta

import pytest
from test_reconciliation_limits import NOW, unknown

from payments.infrastructure.fault_control import barrier


def test_expiry_preserves_identity_window_budget_and_deadline():
    original = unknown(
        status="in_flight", claim_version=7, lease_until=NOW, check_count=3, next_check_at=NOW
    )
    recovered = original.expire_in_flight(NOW, NOW + timedelta(hours=1))
    assert recovered.status == "unknown" and recovered.claim_version == 8
    assert recovered.lease_until is None
    for field in (
        "id",
        "provider_idempotency_key",
        "safe_retry_until",
        "check_count",
        "reconciliation_deadline",
        "next_check_at",
    ):
        assert getattr(recovered, field) == getattr(original, field)
    assert not recovered.owns_result(7, NOW)
    with pytest.raises(ValueError):
        original.expire_in_flight(NOW - timedelta(microseconds=1), NOW)


@pytest.mark.parametrize(
    "profile,directory", [(None, True), ("production", True), ("test", False)]
)
async def test_barriers_require_explicit_test_profile_and_directory(
    monkeypatch, tmp_path, profile, directory
):
    monkeypatch.delenv("APP_PROFILE", raising=False)
    monkeypatch.delenv("TEST_BARRIER_DIR", raising=False)
    if profile:
        monkeypatch.setenv("APP_PROFILE", profile)
    if directory:
        monkeypatch.setenv("TEST_BARRIER_DIR", str(tmp_path))
    (tmp_path / "after_claim.example.arm").touch()
    await barrier("after_claim", "example")
    assert not (tmp_path / "after_claim.example.reached").exists()
