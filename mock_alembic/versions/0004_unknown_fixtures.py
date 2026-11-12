"""Pending operations and durable lookup fault counters; preserve existing finals."""

from alembic import op

revision = "mock_0004"
down_revision = "mock_0003"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE operations DROP CONSTRAINT operations_status_check")
    op.execute("""ALTER TABLE operations ADD CONSTRAINT operations_status_check CHECK (
        (status IN ('succeeded','pending') AND error_code IS NULL) OR
        (status='declined_retryable' AND error_code IS NOT DISTINCT FROM 'issuer_unavailable') OR
        (status='declined_terminal'
         AND error_code IS NOT DISTINCT FROM 'invalid_payment_method') OR
        (status='not_charged_final' AND error_code IS NOT DISTINCT FROM 'not_charged_final'))""")
    op.execute("ALTER TABLE fixtures DROP CONSTRAINT fixtures_charge_mode_check")
    op.execute("""ALTER TABLE fixtures ADD CONSTRAINT fixtures_charge_mode_check CHECK (
        charge_mode IN ('success','declined_retryable','declined_terminal','not_charged_final',
        'pending','charge_then_disconnect','timeout_before_result','malformed_response','http_5xx'))""")
    op.execute("""ALTER TABLE fixtures ADD COLUMN lookup_mode VARCHAR(24) NOT NULL DEFAULT 'normal'
        CHECK (lookup_mode IN ('normal','temporary_not_found','unavailable'))""")
    op.execute("""ALTER TABLE fixtures ADD COLUMN lookup_not_found_count INTEGER NOT NULL DEFAULT 0
        CHECK (lookup_not_found_count >= 0)""")


def downgrade():
    # Refuse rollback while new data exists rather than discard operations/fixtures.
    op.execute("ALTER TABLE operations DROP CONSTRAINT operations_status_check")
    op.execute("""ALTER TABLE operations ADD CONSTRAINT operations_status_check CHECK (
        (status='succeeded' AND error_code IS NULL) OR
        (status='declined_retryable' AND error_code IS NOT DISTINCT FROM 'issuer_unavailable') OR
        (status='declined_terminal'
         AND error_code IS NOT DISTINCT FROM 'invalid_payment_method') OR
        (status='not_charged_final' AND error_code IS NOT DISTINCT FROM 'not_charged_final'))""")
    op.execute("ALTER TABLE fixtures DROP CONSTRAINT fixtures_charge_mode_check")
    op.execute("""ALTER TABLE fixtures ADD CONSTRAINT fixtures_charge_mode_check CHECK (
        charge_mode IN ('success','declined_retryable','declined_terminal',
        'not_charged_final'))""")
    op.drop_column("fixtures", "lookup_not_found_count")
    op.drop_column("fixtures", "lookup_mode")
