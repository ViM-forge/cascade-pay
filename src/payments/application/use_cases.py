from uuid import UUID, uuid4

from payments.application.ports import Provider, UoW
from payments.domain.models import Payment


async def create_payment(
    uow: UoW, merchant: UUID, key: str, amount: int, currency: str, ref: str
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
        await uow.commit()
        return payment


async def get_payment(uow: UoW, identity: UUID, merchant: UUID) -> dict | None:
    async with uow:
        return await uow.payments.get(identity, merchant)


async def process_payment(uow: UoW, provider: Provider, identity: UUID) -> str:
    async with uow:
        await uow.payments.prepare(identity)
        await uow.commit()
    async with uow:
        claimed = await uow.payments.claim(identity)
        await uow.commit()
    if claimed is None:
        return "noop"
    payment, attempt = claimed
    external_id = await provider.charge(payment, attempt)
    async with uow:
        saved = await uow.payments.finish(attempt, external_id)
        await uow.commit()
    return ("succeeded" if external_id else "unknown") if saved else "stale"
