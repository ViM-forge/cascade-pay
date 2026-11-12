from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from payments.application.ports import Conflict
from payments.domain.models import Payment, PaymentAttempt, cascade_decision, request_hash
from payments.infrastructure.orm import AttemptRow, OutboxRow, PaymentRow


def payment_entity(row):
    return Payment(**{k: getattr(row, k) for k in Payment.model_fields})


def attempt_entity(row):
    return PaymentAttempt(**{k: getattr(row, k) for k in PaymentAttempt.model_fields})


class PaymentRepository:
    def __init__(self, session, settings):
        self.session, self.settings = session, settings

    async def create(self, payment, key):
        values = payment.model_dump() | dict(
            routing_snapshot=list(payment.routing_snapshot),
            idempotency_key=key,
            request_hash=request_hash(
                payment.amount_minor, payment.currency, payment.payment_method_ref
            ),
        )
        inserted = await self.session.scalar(
            insert(PaymentRow)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["merchant_id", "idempotency_key"])
            .returning(PaymentRow.id)
        )
        if inserted:
            self.session.add(
                OutboxRow(
                    id=uuid4(),
                    event_type="process_payment",
                    payment_id=payment.id,
                    payload={"payment_id": str(payment.id)},
                    created_at=payment.created_at,
                    updated_at=payment.created_at,
                )
            )
            return payment
        row = await self.session.scalar(
            select(PaymentRow).where(
                PaymentRow.merchant_id == payment.merchant_id, PaymentRow.idempotency_key == key
            )
        )
        if (row.amount_minor, row.currency, row.payment_method_ref) != (
            payment.amount_minor,
            payment.currency,
            payment.payment_method_ref,
        ):
            raise Conflict
        return payment_entity(row)

    async def get(self, identity, merchant):
        row = await self.session.scalar(
            select(PaymentRow).where(PaymentRow.id == identity, PaymentRow.merchant_id == merchant)
        )
        if row is None:
            return None
        attempts = (
            await self.session.scalars(
                select(AttemptRow)
                .where(AttemptRow.payment_id == identity)
                .order_by(AttemptRow.sequence)
            )
        ).all()
        fields = [
            "id",
            "sequence",
            "provider",
            "status",
            "external_id",
            "error_code",
            "check_count",
            "manual_review_required",
            "created_at",
            "updated_at",
        ]
        return {
            **{
                k: getattr(row, k)
                for k in ["id", "status", "amount_minor", "currency", "created_at", "updated_at"]
            },
            "manual_review_required": any(a.manual_review_required for a in attempts),
            "attempts": [{k: getattr(a, k) for k in fields} for a in attempts],
        }

    async def locked(self, identity):
        payment = await self.session.scalar(
            select(PaymentRow).where(PaymentRow.id == identity).with_for_update()
        )
        attempt = (
            await self.session.scalar(
                select(AttemptRow)
                .where(AttemptRow.payment_id == identity)
                .order_by(AttemptRow.sequence.desc())
                .limit(1)
                .with_for_update()
            )
            if payment
            else None
        )
        return payment, attempt

    async def prepare(self, identity):
        p, a = await self.locked(identity)
        if p is None or p.status in {"succeeded", "failed"}:
            return None
        now = await self.session.scalar(select(func.clock_timestamp()))
        if a is None:
            entity = payment_entity(p).transition("processing", now)
            p.status, p.updated_at = entity.status, now
            key = uuid4()
            a = AttemptRow(
                id=key,
                payment_id=p.id,
                sequence=1,
                provider=p.routing_snapshot[0],
                provider_idempotency_key=key,
                status="created",
                claim_version=0,
                safe_retry_until=now + timedelta(seconds=self.settings.safe_retry_seconds),
                created_at=now,
                updated_at=now,
            )
            self.session.add(a)
            await self.session.flush()
        # The caller commits the immutable attempt identity before taking a claim.

    async def claim(self, identity):
        p, a = await self.locked(identity)
        if p is None or p.status in {"succeeded", "failed"} or a is None:
            return None
        now = await self.session.scalar(select(func.clock_timestamp()))
        if a.status != "created":
            # No HTTP for terminal, active lease or unknown/expired in-flight.
            # Recovery/reconciliation will be introduced separately.
            return None
        claimed = attempt_entity(a).claim(
            now, now + timedelta(seconds=self.settings.lease_seconds)
        )
        for k in ["status", "claim_version", "lease_until", "updated_at"]:
            setattr(a, k, getattr(claimed, k))
        return payment_entity(p), claimed

    async def finish(self, attempt, result):
        p, a = await self.locked(attempt.payment_id)
        now = await self.session.scalar(select(func.clock_timestamp()))
        if (
            a is None
            or a.id != attempt.id
            or a.status != "in_flight"
            or a.claim_version != attempt.claim_version
            or a.lease_until <= now
        ):
            return False
        status, next_provider = cascade_decision(payment_entity(p), attempt_entity(a), result)
        finished = attempt_entity(a).finish(
            attempt.claim_version,
            now,
            result,
            now + timedelta(seconds=self.settings.reconciliation_deadline_seconds),
            now + timedelta(seconds=1),
        )
        for k in [
            "status",
            "external_id",
            "error_code",
            "lease_until",
            "updated_at",
            "reconciliation_deadline",
            "next_check_at",
        ]:
            setattr(a, k, getattr(finished, k))
        # Flush the terminal A before inserting B: partial UNIQUE forbids two active attempts.
        await self.session.flush()
        if next_provider:
            key = uuid4()
            self.session.add(
                AttemptRow(
                    id=key,
                    payment_id=p.id,
                    sequence=a.sequence + 1,
                    provider=next_provider,
                    provider_idempotency_key=key,
                    status="created",
                    claim_version=0,
                    safe_retry_until=now + timedelta(seconds=self.settings.safe_retry_seconds),
                    created_at=now,
                    updated_at=now,
                )
            )
            self.session.add(
                OutboxRow(
                    id=uuid4(),
                    event_type="process_payment",
                    payment_id=p.id,
                    payload={"payment_id": str(p.id)},
                    created_at=now,
                    updated_at=now,
                )
            )
        entity = payment_entity(p)
        if entity.status != status:
            entity = entity.transition(status, now)
        p.status, p.updated_at = entity.status, now
        return True


class SqlUoW:
    def __init__(self, sessions, settings):
        self.sessions, self.settings = sessions, settings

    async def __aenter__(self):
        self.session = self.sessions()
        self.payments = PaymentRepository(self.session, self.settings)
        return self

    async def __aexit__(self, *args):
        await self.session.close()

    async def now(self):
        return await self.session.scalar(select(func.clock_timestamp()))

    async def commit(self):
        await self.session.commit()
