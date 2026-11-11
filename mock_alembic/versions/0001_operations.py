"""Independent provider operations, ledger, call counters and success fixtures."""

from alembic import op

revision = "mock_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE operations (
        key UUID PRIMARY KEY, external_id UUID NOT NULL UNIQUE,
        amount_minor BIGINT NOT NULL CHECK(amount_minor > 0),
        currency VARCHAR(3) NOT NULL CHECK(currency = 'USD'),
        method_ref VARCHAR(255) NOT NULL CHECK(length(method_ref) BETWEEN 1 AND 255),
        request_hash CHAR(64) NOT NULL,
        status VARCHAR(32) NOT NULL CHECK(status = 'succeeded'),
        error_code VARCHAR(64), created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
    op.execute("""CREATE TABLE ledger (
        id UUID PRIMARY KEY, operation_id UUID NOT NULL UNIQUE REFERENCES operations(external_id),
        amount_minor BIGINT NOT NULL CHECK(amount_minor > 0),
        currency VARCHAR(3) NOT NULL CHECK(currency = 'USD'),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
    op.execute("""CREATE TABLE calls (
        id BIGSERIAL PRIMARY KEY, key UUID NOT NULL, method_ref VARCHAR(255),
        kind VARCHAR(8) NOT NULL CHECK(kind IN ('create','lookup')))""")
    op.execute("CREATE INDEX ix_calls_key ON calls(key)")
    op.execute("CREATE INDEX ix_calls_ref ON calls(method_ref)")
    op.execute("""CREATE TABLE fixtures (method_ref VARCHAR(255) PRIMARY KEY,
        charge_mode VARCHAR(16) NOT NULL CHECK(charge_mode = 'success'))""")


def downgrade():
    for table in ["fixtures", "calls", "ledger", "operations"]:
        op.drop_table(table)
