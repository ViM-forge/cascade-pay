import asyncio
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict

from payments.domain.models import Amount, MethodRef


class ChargeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: UUID
    external_id: UUID
    amount_minor: Amount
    currency: str
    method_ref: MethodRef
    status: str
    error_code: str | None


class HttpProvider:
    def __init__(self, settings):
        self.settings = settings

    async def charge(self, payment, attempt):
        try:
            async with asyncio.timeout(self.settings.http_timeout_seconds):
                async with httpx.AsyncClient(timeout=self.settings.http_timeout_seconds) as client:
                    response = await client.post(
                        self.settings.provider_a_url + "/charges",
                        headers={
                            "Authorization": "Bearer "
                            + self.settings.mock_a_service_credential.get_secret_value()
                        },
                        json=dict(
                            key=str(attempt.provider_idempotency_key),
                            amount_minor=payment.amount_minor,
                            currency=payment.currency,
                            method_ref=payment.payment_method_ref,
                        ),
                    )
                    response.raise_for_status()
                    result = ChargeResponse.model_validate(response.json())
                    if (
                        result.key == attempt.id
                        and result.amount_minor == payment.amount_minor
                        and result.currency == payment.currency
                        and result.method_ref == payment.payment_method_ref
                        and result.status == "succeeded"
                        and result.error_code is None
                    ):
                        return result.external_id
        except (httpx.HTTPError, ValueError, TimeoutError):
            pass
        return None
