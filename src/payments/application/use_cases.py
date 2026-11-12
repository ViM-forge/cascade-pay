from uuid import UUID, uuid4

from payments.application.ports import Provider, UoW
from payments.domain.models import Payment


async def create_payment(
    uow: UoW, merchant: UUID, key: str, amount: int, currency: str, ref: str, *, checkpoint=None
) -> Payment:
    async with uow:
        now = await uow.now()
        payment = Payment(
            id=uuid4(),
            merchant_id=merchant,
            amount_minor=amount,
            currency=currency,
            payment_method_ref=ref,
            created_at=now,
            updated_at=now,
        )
        payment = await uow.payments.create(payment, key)
        if checkpoint:
            await checkpoint("before_payment_commit", str(payment.id))
        await uow.commit()
    if checkpoint:
        await checkpoint("after_payment_commit", str(payment.id))
    return payment


async def get_payment(uow: UoW, identity: UUID, merchant: UUID) -> dict | None:
    async with uow:
        return await uow.payments.get(identity, merchant)


async def process_payment(
    uow: UoW, provider: Provider, identity: UUID, *, checkpoint=None
) -> tuple[str, UUID | None, str | None]:
    async with uow:
        await uow.payments.prepare(identity)
        await uow.commit()
    if checkpoint:
        await checkpoint("after_prepare", str(identity))
    async with uow:
        claimed = await uow.payments.claim(identity)
        await uow.commit()
    if claimed is None:
        return "noop", None, None
    payment, attempt = claimed
    if checkpoint:
        await checkpoint("after_claim", str(identity))
    result = await provider.charge(payment, attempt)
    if checkpoint:
        await checkpoint("before_result_commit", str(identity))
    async with uow:
        saved = await uow.payments.finish(attempt, result)
        await uow.commit()
    if checkpoint:
        await checkpoint("after_result_commit", str(identity))
    return result.status if saved else "stale", attempt.id, attempt.provider


async def reconcile_payment(
    uow: UoW, provider: Provider, identity: UUID, *, checkpoint=None
) -> tuple[str, UUID | None, str | None]:
    async with uow:
        claimed = await uow.payments.claim_reconciliation(identity)
        await uow.commit()
    if claimed is None:
        return "noop", None, None
    payment, attempt = claimed
    if checkpoint:
        await checkpoint("after_claim", str(identity))
    result = await provider.lookup(payment, attempt)
    if result.status == "not_found":
        # Commit/close this short recheck before the possible second HTTP call.
        async with uow:
            allowed = await uow.payments.can_retry(attempt)
            await uow.commit()
        if allowed:
            result = await provider.charge(payment, attempt)
    if checkpoint:
        await checkpoint("before_result_commit", str(identity))
    async with uow:
        saved = await uow.payments.finish(attempt, result)
        await uow.commit()
    if checkpoint:
        await checkpoint("after_result_commit", str(identity))
    return result.status if saved else "stale", attempt.id, attempt.provider
