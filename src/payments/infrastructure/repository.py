import logging
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert

from payments.application.ports import Conflict
from payments.domain.models import Payment, PaymentAttempt, request_hash
from payments.infrastructure.orm import AttemptRow, OutboxRow, PaymentRow


def payment_entity(row):
    return Payment(**{k: getattr(row, k) for k in Payment.model_fields})


def attempt_entity(row):
    return PaymentAttempt(**{k: getattr(row, k) for k in PaymentAttempt.model_fields})


def store_attempt(row, entity):
    # Explicit entity/ORM boundary; callers hold payment then attempt row locks.
    for name, value in entity.model_dump().items():
        setattr(row, name, value)


class PaymentRepository:
    def __init__(self, session, settings):
        self.session, self.settings = session, settings

    def enqueue(self, event_type, payment_id, now):
        self.session.add(
            OutboxRow(
                id=uuid4(),
                event_type=event_type,
                payment_id=payment_id,
                payload={"payment_id": str(payment_id)},
                created_at=now,
                updated_at=now,
            )
        )

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
            self.enqueue("process_payment", payment.id, payment.created_at)
            return payment
        # READ COMMITTED: DO NOTHING may wait for a concurrent winner. The next
        # statement gets a fresh snapshot; no failed INSERT/aborted transaction.
        row = await self.session.scalar(
            select(PaymentRow).where(
                PaymentRow.merchant_id == payment.merchant_id, PaymentRow.idempotency_key == key
            )
        )
        if row.request_hash != values["request_hash"] or (
            row.amount_minor,
            row.currency,
            row.payment_method_ref,
        ) != (
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

    async def recover_batch(self, batch=20):
        """DB-only scheduling. Lock payments before attempts, as all worker paths do."""
        now = await self.session.scalar(select(func.clock_timestamp()))
        active = (
            select(AttemptRow.id)
            .where(
                AttemptRow.payment_id == PaymentRow.id,
                AttemptRow.status.in_(["created", "in_flight", "unknown"]),
            )
            .exists()
        )
        ready = (
            select(AttemptRow.id)
            .where(
                AttemptRow.payment_id == PaymentRow.id,
                or_(
                    AttemptRow.status == "created",
                    and_(AttemptRow.status == "in_flight", AttemptRow.lease_until <= now),
                    and_(
                        AttemptRow.status == "unknown",
                        AttemptRow.manual_review_required.is_(False),
                        or_(AttemptRow.lease_until.is_(None), AttemptRow.lease_until <= now),
                        or_(
                            AttemptRow.next_check_at <= now,
                            AttemptRow.check_count >= self.settings.reconciliation_max_checks,
                            AttemptRow.reconciliation_deadline <= now,
                        ),
                    ),
                ),
            )
            .exists()
        )
        rows = (
            await self.session.scalars(
                select(PaymentRow)
                .where(
                    PaymentRow.status.in_(["pending", "processing", "unknown"]),
                    PaymentRow.updated_at
                    <= now - timedelta(seconds=self.settings.recovery_interval_seconds),
                    or_(ready, and_(PaymentRow.status.in_(["pending", "processing"]), ~active)),
                )
                .order_by(PaymentRow.updated_at, PaymentRow.id)
                .limit(batch)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for p in rows:
            _, a = await self.locked(p.id)
            if a is None:
                await self.prepare(p.id)
            elif a.status in {"succeeded", "declined"}:
                # Repair only from durable history, using the same result policy.
                payment, next_provider = payment_entity(p).recover_history(attempt_entity(a), now)
                await self.save_payment_result(p, a, payment, next_provider, now)
            elif a.status == "in_flight":
                store_attempt(
                    a,
                    attempt_entity(a).expire_in_flight(
                        now, now + timedelta(seconds=self.settings.reconciliation_deadline_seconds)
                    ),
                )
                entity = payment_entity(p).transition("unknown", now)
                p.status = entity.status
            # Re-read after prepare/repair, preserving the immutable saved identity.
            await self.session.flush()
            _, a = await self.locked(p.id)
            event = None
            if a.status == "created":
                event = "process_payment"
            elif a.status == "unknown":
                action = attempt_entity(a).reconciliation_action(
                    now, self.settings.reconciliation_max_checks
                )
                if action == "manual":
                    store_attempt(
                        a,
                        attempt_entity(a).stop_reconciliation(
                            now, self.settings.reconciliation_max_checks
                        ),
                    )
                elif action == "claim":
                    event = "reconcile_payment"
            if event:
                unpublished = await self.session.scalar(
                    select(OutboxRow.id)
                    .where(
                        OutboxRow.payment_id == p.id,
                        OutboxRow.event_type == event,
                        OutboxRow.published_at.is_(None),
                    )
                    .limit(1)
                )
                if unpublished is None:
                    self.enqueue(event, p.id, now)
            logging.info(
                "recovery scheduled",
                extra=dict(
                    event="recovery_scheduled",
                    payment_id=str(p.id),
                    attempt_id=str(a.id),
                    result="manual" if a.manual_review_required else event or a.status,
                ),
            )
            p.updated_at = now  # Fair rotation and bounded republication frequency.
        return len(rows)

    async def prepare(self, identity):
        p, a = await self.locked(identity)
        if p is None or p.status in {"succeeded", "failed"}:
            return None
        now = await self.session.scalar(select(func.clock_timestamp()))
        if a is None:
            entity = payment_entity(p)
            if entity.status != "processing":
                entity = entity.transition("processing", now)
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
            # Recovery schedules lookup after lease expiry.
            return None
        if now >= a.safe_retry_until:
            # The time is read after both locks. A late initial delivery must
            # persist lookup work rather than send charge outside the saved window.
            expired = attempt_entity(a).expire_before_send(
                now,
                now + timedelta(seconds=self.settings.reconciliation_deadline_seconds),
                now + timedelta(seconds=1),
            )
            store_attempt(a, expired)
            if p.status != "unknown":
                entity = payment_entity(p).transition("unknown", now)
                p.status, p.updated_at = entity.status, now
            self.enqueue("reconcile_payment", p.id, now)
            return None
        claimed = attempt_entity(a).claim(
            now, now + timedelta(seconds=self.settings.lease_seconds)
        )
        store_attempt(a, claimed)
        return payment_entity(p), claimed

    async def claim_reconciliation(self, identity):
        p, a = await self.locked(identity)
        if p is None or p.status != "unknown" or a is None:
            return None
        now = await self.session.scalar(select(func.clock_timestamp()))
        entity = attempt_entity(a)
        action = entity.reconciliation_action(now, self.settings.reconciliation_max_checks)
        if action == "noop":
            return None
        if action == "manual":
            store_attempt(
                a, entity.stop_reconciliation(now, self.settings.reconciliation_max_checks)
            )
            p.updated_at = now
            return None
        claimed = entity.claim_reconciliation(
            now,
            now + timedelta(seconds=self.settings.lease_seconds),
            self.settings.reconciliation_max_checks,
        )
        store_attempt(a, claimed)
        return payment_entity(p), claimed

    async def can_retry(self, attempt):
        p, a = await self.locked(attempt.payment_id)
        now = await self.session.scalar(select(func.clock_timestamp()))
        return (
            p is not None
            and p.status == "unknown"
            and a is not None
            and a.id == attempt.id
            and attempt_entity(a).may_retry_charge(attempt.claim_version, now)
        )

    async def finish(self, attempt, result):
        p, a = await self.locked(attempt.payment_id)
        now = await self.session.scalar(select(func.clock_timestamp()))
        if (
            a is None
            or a.id != attempt.id
            or a.status != attempt.status
            or not attempt_entity(a).owns_result(attempt.claim_version, now)
        ):
            return False
        payment, next_provider = payment_entity(p).apply_result(attempt_entity(a), result, now)
        finished = attempt_entity(a).finish(
            attempt.claim_version,
            now,
            result,
            now + timedelta(seconds=self.settings.reconciliation_deadline_seconds),
            now + timedelta(seconds=1),
            self.settings.reconciliation_max_checks,
        )
        store_attempt(a, finished)
        # Flush the terminal A before inserting B: partial UNIQUE forbids two active attempts.
        await self.session.flush()
        await self.save_payment_result(p, a, payment, next_provider, now)
        if finished.status == "unknown" and not finished.manual_review_required:
            self.enqueue("reconcile_payment", p.id, now)
        return True

    async def save_payment_result(self, p, a, payment, next_provider, now):
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
            self.enqueue("process_payment", p.id, now)
        p.status, p.updated_at = payment.status, now


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
