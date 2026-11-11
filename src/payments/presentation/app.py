import asyncio
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from payments.application.ports import Conflict
from payments.application.use_cases import create_payment, get_payment
from payments.infrastructure.db import create_engine, session_factory
from payments.infrastructure.logging import configure_logging
from payments.infrastructure.repository import SqlUoW
from payments.infrastructure.settings import load_settings
from payments.presentation.auth import Unauthorized, merchant
from payments.presentation.dto import CreatePayment


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = load_settings()
    engine = create_engine(settings)
    redis = Redis.from_url(
        settings.redis_url.get_secret_value(), socket_connect_timeout=2, socket_timeout=2
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessions = session_factory(engine)
    app.state.redis = redis
    try:
        yield
    finally:
        await redis.aclose()
        await engine.dispose()


app = FastAPI(title="Cascade Payments", lifespan=lifespan)


@app.get("/health/live")
async def live():
    return {"status": "ok"}


@app.get("/health/ready")
async def ready():
    try:
        async with asyncio.timeout(3):
            async with app.state.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            await app.state.redis.ping()
    except Exception:
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return {"status": "ok"}


def error(code, status, headers=None, **extra):
    return JSONResponse({"error": {"code": code, **extra}}, status_code=status, headers=headers)


@app.exception_handler(Unauthorized)
async def unauthorized(request, exc):
    return error("unauthorized", 401, {"WWW-Authenticate": "Bearer"})


@app.exception_handler(RequestValidationError)
async def invalid(request, exc):
    fields = sorted({str(e["loc"][-1]) for e in exc.errors()})
    return error("validation_error", 422, fields=fields)


@app.exception_handler(SQLAlchemyError)
async def unavailable(request, exc):
    return error("unavailable", 503)


@app.post("/payments", status_code=202)
async def create(
    body: CreatePayment, request: Request, identity: Annotated[UUID, Depends(merchant)]
):
    keys = request.headers.getlist("idempotency-key")
    if (
        len(keys) != 1
        or not 1 <= len(keys[0]) <= 128
        or any(not 0x21 <= ord(c) <= 0x7E for c in keys[0])
    ):
        return error("validation_error", 422, fields=["Idempotency-Key"])
    try:
        payment = await create_payment(
            SqlUoW(request.app.state.sessions, request.app.state.settings),
            identity,
            keys[0],
            body.amount_minor,
            body.currency,
            body.payment_method_ref,
        )
    except Conflict:
        return error("idempotency_conflict", 409)
    return JSONResponse(
        {"id": str(payment.id), "status": payment.status},
        status_code=202,
        headers={"Location": f"/payments/{payment.id}"},
    )


@app.get("/payments/{payment_id}")
async def get(payment_id: UUID, request: Request, identity: Annotated[UUID, Depends(merchant)]):
    result = await get_payment(
        SqlUoW(request.app.state.sessions, request.app.state.settings), payment_id, identity
    )
    return result if result else error("payment_not_found", 404)
