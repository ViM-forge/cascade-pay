from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class MockSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", hide_input_in_errors=True)
    database_url: SecretStr
    mock_service_credential: SecretStr
    app_profile: str = "production"


def load_settings():
    return MockSettings()
