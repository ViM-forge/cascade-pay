from uuid import uuid4

import pytest
from pydantic import ValidationError

from payments.infrastructure.settings import Settings


def config():
    return dict(
        database_url="postgresql+asyncpg://user:password@localhost/payments",
        redis_url="redis://localhost:6379/0",
        jwt_secret="s" * 40,
        mock_service_credential="m" * 40,
        merchant_ids=(uuid4(), uuid4()),
    )


def test_load_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("JWT_ISSUER", raising=False)
    (tmp_path / ".env").write_text("JWT_ISSUER=local-test\n")
    assert Settings(_env_file=".env", **config()).jwt_issuer == "local-test"


@pytest.mark.parametrize(
    "patch",
    [
        {"http_timeout_seconds": 5},
        {"lease_seconds": 0},
        {"recovery_interval_seconds": 10},
        {"safe_retry_seconds": 86401},
        {"reconciliation_max_checks": 6},
        {"reconciliation_deadline_seconds": float("inf")},
        {"jwt_ttl_seconds": 901},
        {"database_url": "sqlite:///test"},
        {"redis_url": "http://localhost"},
        {"jwt_secret": "placeholder"},
        {"mock_service_credential": ""},
        {"provider_a_url": "ftp://localhost"},
    ],
)
def test_invalid_config(patch):
    with pytest.raises(ValidationError):
        Settings(**(config() | patch))


def test_missing_secret_and_safe_error(monkeypatch):
    for key in ("JWT_SECRET", "MOCK_SERVICE_CREDENTIAL"):
        monkeypatch.delenv(key, raising=False)
    values = config()
    values.pop("jwt_secret")
    with pytest.raises(ValidationError):
        Settings(**values)
    secret = "super-private-value"
    with pytest.raises(ValidationError) as error:
        Settings(**(config() | {"database_url": secret}))
    assert secret not in str(error.value)
