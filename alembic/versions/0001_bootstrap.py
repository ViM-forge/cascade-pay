"""Payment storage; no payment execution yet."""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE payments (
        id UUID PRIMARY KEY,
        merchant_id UUID NOT NULL,
        amount_minor BIGINT NOT NULL CHECK (amount_minor > 0),
        currency VARCHAR(3) NOT NULL CHECK (currency = 'USD'),
        payment_method_ref VARCHAR(255) NOT NULL
            CHECK (length(payment_method_ref) BETWEEN 1 AND 255),
        status VARCHAR(16) NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending','processing','unknown','succeeded','failed')),
        idempotency_key VARCHAR(128) NOT NULL CHECK (idempotency_key ~ '^[!-~]{1,128}$'),
        request_hash CHAR(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
        routing_snapshot JSONB NOT NULL CHECK (routing_snapshot = '["A","B"]'::jsonb),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_payment_merchant_key UNIQUE (merchant_id, idempotency_key)
    )
    """)
    op.execute("""
    CREATE TABLE payment_attempts (
        id UUID PRIMARY KEY,
        payment_id UUID NOT NULL REFERENCES payments(id),
        sequence SMALLINT NOT NULL CHECK (sequence IN (1,2)),
        provider VARCHAR(1) NOT NULL CHECK (provider IN ('A','B')),
        provider_idempotency_key UUID NOT NULL CHECK (provider_idempotency_key = id),
        external_id UUID,
        status VARCHAR(16) NOT NULL DEFAULT 'created'
            CHECK (status IN ('created','in_flight','succeeded','declined','unknown')),
        error_code VARCHAR(64),
        lease_until TIMESTAMPTZ,
        claim_version BIGINT NOT NULL DEFAULT 0 CHECK (claim_version >= 0),
        check_count SMALLINT NOT NULL DEFAULT 0 CHECK (check_count BETWEEN 0 AND 5),
        reconciliation_deadline TIMESTAMPTZ,
        manual_review_required BOOLEAN NOT NULL DEFAULT false,
        safe_retry_until TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '24 hours'),
        next_check_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_attempt_sequence UNIQUE (payment_id, sequence),
        CONSTRAINT uq_attempt_provider_key UNIQUE (provider, provider_idempotency_key)
    )
    """)
    op.execute("""
    CREATE TABLE outbox (
        id UUID PRIMARY KEY,
        event_type VARCHAR(32) NOT NULL
            CHECK (event_type IN ('process_payment','reconcile_payment')),
        payment_id UUID NOT NULL REFERENCES payments(id),
        payload JSONB NOT NULL,
        published_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CHECK (payload = jsonb_build_object('payment_id', payment_id::text))
    )
    """)
    op.execute("CREATE INDEX ix_payment_status_updated ON payments(status, updated_at)")
    op.execute("CREATE INDEX ix_attempt_payment ON payment_attempts(payment_id)")
    op.execute("""
    CREATE UNIQUE INDEX uq_attempt_active ON payment_attempts(payment_id)
    WHERE status IN ('created','in_flight','unknown')
    """)
    op.execute("""
    CREATE INDEX ix_attempt_due ON payment_attempts(next_check_at)
    WHERE status = 'unknown' AND NOT manual_review_required
    """)
    op.execute("""
    CREATE INDEX ix_attempt_lease ON payment_attempts(lease_until)
    WHERE status = 'in_flight'
    """)
    op.execute("CREATE INDEX ix_outbox_payment ON outbox(payment_id)")
    op.execute("""
    CREATE INDEX ix_outbox_unpublished ON outbox(created_at) WHERE published_at IS NULL
    """)


def downgrade():
    op.drop_table("outbox")
    op.drop_table("payment_attempts")
    op.drop_table("payments")
