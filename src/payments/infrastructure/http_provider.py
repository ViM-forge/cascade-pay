import asyncio
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, field_validator

from payments.domain.models import Amount, MethodRef, ProviderResult


class ChargeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: UUID
    external_id: UUID
    amount_minor: Amount
    currency: str
    method_ref: MethodRef
    status: str
    error_code: str | None

    @field_validator("key", "external_id", mode="before")
    @classmethod
    def canonical_uuid(cls, value):
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError("expected canonical UUID")
        return value


class HttpProvider:
    def __init__(self, settings):
        self.settings = settings

    async def charge(self, payment, attempt):
        try:
            async with asyncio.timeout(self.settings.http_timeout_seconds):
                async with httpx.AsyncClient(timeout=self.settings.http_timeout_seconds) as client:
                    response = await client.post(
                        getattr(self.settings, f"provider_{attempt.provider.lower()}_url")
                        + "/charges",
                        headers={
                            "Authorization": "Bearer "
                            + getattr(
                                self.settings,
                                f"mock_{attempt.provider.lower()}_service_credential",
                            ).get_secret_value()
                        },
                        json=dict(
                            key=str(attempt.provider_idempotency_key),
                            amount_minor=payment.amount_minor,
                            currency=payment.currency,
                            method_ref=payment.payment_method_ref,
                        ),
                    )
                    if response.status_code != 200:
                        return ProviderResult(status="unknown")
                    result = ChargeResponse.model_validate(response.json())
                    if (
                        result.key == attempt.id
                        and result.amount_minor == payment.amount_minor
                        and result.currency == payment.currency
                        and result.method_ref == payment.payment_method_ref
                    ):
                        return ProviderResult(
                            status=result.status,
                            error_code=result.error_code,
                            external_id=result.external_id,
                        )
        except (httpx.HTTPError, ValueError, TimeoutError):
            pass
        return ProviderResult(status="unknown")
