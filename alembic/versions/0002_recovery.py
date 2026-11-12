"""Recovery candidate and exhausted reconciliation indexes."""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE INDEX ix_attempt_created ON payment_attempts(payment_id)
                WHERE status = 'created'""")
    op.execute("""CREATE INDEX ix_attempt_budget ON payment_attempts
                (check_count, reconciliation_deadline, lease_until)
                WHERE status = 'unknown' AND NOT manual_review_required""")


def downgrade():
    op.drop_index("ix_attempt_budget")
    op.drop_index("ix_attempt_created")
