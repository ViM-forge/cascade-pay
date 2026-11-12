import asyncio
from typing import Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, field_validator

from payments.domain.models import Amount, MethodRef, ProviderResult


class ChargeResponse(BaseModel):
    """External operation envelope; internal unknown/not_found are never HTTP 200 states."""

    model_config = ConfigDict(extra="forbid")
    key: UUID
    external_id: UUID
    amount_minor: Amount
    currency: str
    method_ref: MethodRef
    status: Literal[
        "succeeded", "declined_retryable", "declined_terminal", "not_charged_final", "pending"
    ]
    error_code: str | None

    @field_validator("key", "external_id", mode="before")
    @classmethod
    def canonical_uuid(cls, value):
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError("expected canonical UUID")
        return value


class HttpProvider:
    """Validate charge/lookup against the saved operation under a wall-clock timeout.

    Unexpected envelopes, HTTP codes and transport failures become unknown with
    no external ID. Only an exact lookup 404 envelope becomes not_found. No
    automatic HTTP retries: application policy controls every same-key charge.
    """

    def __init__(self, settings):
        self.settings = settings

    async def charge(self, payment, attempt):
        return await self._request(payment, attempt, lookup=False)

    async def lookup(self, payment, attempt):
        return await self._request(payment, attempt, lookup=True)

    async def _request(self, payment, attempt, *, lookup):
        # Shared external envelope validation; not_found is a lookup-only 404.
        try:
            async with asyncio.timeout(self.settings.http_timeout_seconds):
                async with httpx.AsyncClient(timeout=self.settings.http_timeout_seconds) as client:
                    url = getattr(self.settings, f"provider_{attempt.provider.lower()}_url")
                    headers = {
                        "Authorization": "Bearer "
                        + getattr(
                            self.settings, f"mock_{attempt.provider.lower()}_service_credential"
                        ).get_secret_value()
                    }
                    if lookup:
                        response = await client.get(
                            url + "/charges/by-key/" + str(attempt.provider_idempotency_key),
                            headers=headers,
                        )
                    else:
                        response = await client.post(
                            url + "/charges",
                            headers=headers,
                            json=dict(
                                key=str(attempt.provider_idempotency_key),
                                amount_minor=payment.amount_minor,
                                currency=payment.currency,
                                method_ref=payment.payment_method_ref,
                            ),
                        )
                    if lookup and response.status_code == 404:
                        body = response.json()
                        if body == {
                            "key": str(attempt.provider_idempotency_key),
                            "status": "not_found",
                        }:
                            return ProviderResult(status="not_found")
                        return ProviderResult(status="unknown")
                    if response.status_code != 200:
                        return ProviderResult(status="unknown")
                    result = ChargeResponse.model_validate(response.json())
                    if (
                        result.key == attempt.provider_idempotency_key
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
