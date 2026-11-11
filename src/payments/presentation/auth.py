from uuid import UUID

import jwt
from fastapi import Request


class Unauthorized(Exception):
    pass


def merchant(request: Request) -> UUID:
    headers = request.headers.getlist("authorization")
    if len(headers) != 1:
        raise Unauthorized
    parts = headers[0].split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        raise Unauthorized
    settings = request.app.state.settings
    try:
        claims = jwt.decode(
            parts[1],
            settings.jwt_secret.get_secret_value(),
            algorithms=["HS256"],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            leeway=0,
            options={"require": ["sub", "exp", "iat", "iss", "aud"], "strict_aud": True},
        )
        if type(claims["exp"]) is not int or type(claims["iat"]) is not int:
            raise ValueError
        if not 0 < claims["exp"] - claims["iat"] <= 900:
            raise ValueError
        identity = UUID(claims["sub"])
        if identity not in settings.merchant_ids:
            raise ValueError
        return identity
    except (jwt.InvalidTokenError, ValueError, TypeError):
        raise Unauthorized from None
