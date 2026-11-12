"""Minimal deterministic test/demo barrier; no financial fault modes."""

from alembic import op

revision = "mock_0003"
down_revision = "mock_0002"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""ALTER TABLE fixtures ADD COLUMN barrier VARCHAR(40)
        CHECK(barrier IN ('before_charge_commit','after_charge_commit_before_response'))""")
    op.execute("""CREATE TABLE barriers (
        key UUID PRIMARY KEY,
        point VARCHAR(40) NOT NULL CHECK(point IN
            ('before_charge_commit','after_charge_commit_before_response')),
        released BOOLEAN NOT NULL DEFAULT false)""")


def downgrade():
    op.drop_table("barriers")
    op.drop_column("fixtures", "barrier")
