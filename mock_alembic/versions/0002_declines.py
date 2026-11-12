"""Durable final declines and fixture selection; preserve existing operations."""

from alembic import op

revision = "mock_0002"
down_revision = "mock_0001"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE operations DROP CONSTRAINT operations_status_check")
    op.execute("""ALTER TABLE operations ADD CONSTRAINT operations_status_check CHECK (
        (status='succeeded' AND error_code IS NULL) OR
        (status='declined_retryable' AND error_code IS NOT DISTINCT FROM 'issuer_unavailable') OR
        (status='declined_terminal'
         AND error_code IS NOT DISTINCT FROM 'invalid_payment_method') OR
        (status='not_charged_final' AND error_code IS NOT DISTINCT FROM 'not_charged_final'))""")
    op.execute("ALTER TABLE fixtures DROP CONSTRAINT fixtures_charge_mode_check")
    op.execute("ALTER TABLE fixtures ALTER COLUMN charge_mode TYPE VARCHAR(32)")
    op.execute("""ALTER TABLE fixtures ADD CONSTRAINT fixtures_charge_mode_check
        CHECK(charge_mode IN ('success','declined_retryable','declined_terminal',
        'not_charged_final'))""")


def downgrade():
    # Refuse to discard durable refusals: rollback is possible only without these records.
    op.execute("ALTER TABLE operations DROP CONSTRAINT operations_status_check")
    op.execute("ALTER TABLE operations ADD CHECK(status='succeeded')")
    op.execute("ALTER TABLE fixtures DROP CONSTRAINT fixtures_charge_mode_check")
    op.execute("ALTER TABLE fixtures ADD CHECK(charge_mode='success')")
    op.execute("ALTER TABLE fixtures ALTER COLUMN charge_mode TYPE VARCHAR(16)")
