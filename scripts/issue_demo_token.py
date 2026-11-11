import argparse
import time

import jwt

from payments.infrastructure.settings import load_settings


def issue(settings, merchant=0):
    now = int(time.time())
    return jwt.encode(
        dict(
            sub=str(settings.merchant_ids[merchant]),
            iat=now,
            exp=now + settings.jwt_ttl_seconds,
            iss=settings.jwt_issuer,
            aud=settings.jwt_audience,
        ),
        settings.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Issue a short-lived demo JWT locally")
    parser.add_argument("--merchant", type=int, choices=[1, 2], default=1)
    args = parser.parse_args()
    print(issue(load_settings(), args.merchant - 1))
