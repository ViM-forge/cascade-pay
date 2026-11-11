import re

from pydantic import BaseModel, ConfigDict, field_validator

from payments.domain.models import Amount, MethodRef


class CreatePayment(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    amount_minor: Amount
    currency: str
    payment_method_ref: MethodRef

    @field_validator("currency", mode="before")
    @classmethod
    def currency_usd(cls, value):
        if not isinstance(value, str) or not re.fullmatch("[a-zA-Z]{3}", value):
            raise ValueError("invalid currency")
        if value.upper() != "USD":
            raise ValueError("unsupported currency")
        return value.upper()
