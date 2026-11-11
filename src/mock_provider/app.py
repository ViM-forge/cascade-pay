import secrets
from contextlib import asynccontextmanager
from typing import Literal
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from mock_provider.settings import load_settings
from payments.domain.models import Amount, MethodRef, request_hash, valid_ref
from payments.infrastructure.logging import configure_logging


@asynccontextmanager
async def lifespan(app):
    configure_logging()
    app.state.settings = load_settings()
    app.state.engine = create_async_engine(
        app.state.settings.database_url.get_secret_value(), hide_parameters=True
    )
    try:
        yield
    finally:
        await app.state.engine.dispose()


app = FastAPI(title="Independent durable mock", lifespan=lifespan)


def error(code, status, **extra):
    return JSONResponse({"error": {"code": code, **extra}}, status_code=status)


@app.exception_handler(RequestValidationError)
async def invalid(request, exc):
    return error("validation_error", 422, fields=sorted({str(e["loc"][-1]) for e in exc.errors()}))


async def credential(request: Request):
    values = request.headers.getlist("authorization")
    expected = "Bearer " + request.app.state.settings.mock_service_credential.get_secret_value()
    if len(values) != 1 or not secrets.compare_digest(values[0].encode(), expected.encode()):
        from fastapi import HTTPException

        raise HTTPException(401, detail="unauthorized")


@app.exception_handler(401)
async def unauthorized(request, exc):
    return error("unauthorized", 401)


async def controls(request: Request):
    if request.app.state.settings.app_profile not in {"test", "demo"}:
        from fastapi import HTTPException

        raise HTTPException(404)
    await credential(request)


class Charge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: UUID
    amount_minor: Amount
    currency: Literal["USD"]
    method_ref: MethodRef

    @field_validator("key", mode="before")
    @classmethod
    def canonical_key(cls, value):
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError("expected canonical UUID")
        return value


class Fixture(BaseModel):
    model_config = ConfigDict(extra="forbid")
    charge_mode: Literal["success"]
    lookup_mode: Literal["normal"] = "normal"
    lookup_not_found_count: Literal[0] = 0
    barrier: None = None


def envelope(row):
    return {
        k: str(row[k]) if k in {"key", "external_id"} else row[k]
        for k in [
            "key",
            "external_id",
            "amount_minor",
            "currency",
            "method_ref",
            "status",
            "error_code",
        ]
    }


@app.get("/health/live")
@app.get("/health/ready")
async def health():
    return {"status": "ok"}


@app.post("/charges", dependencies=[Depends(credential)])
async def charge(body: Charge, request: Request):
    values = body.model_dump() | dict(
        external_id=uuid4(),
        request_hash=request_hash(body.amount_minor, body.currency, body.method_ref),
    )
    async with request.app.state.engine.begin() as c:
        await c.execute(
            text("INSERT INTO calls(key, method_ref, kind) VALUES(:key,:method_ref,'create')"),
            values,
        )
        created = (
            await c.execute(
                text("""INSERT INTO operations
            (key,external_id,amount_minor,currency,method_ref,request_hash,status)
            VALUES(:key,:external_id,:amount_minor,:currency,:method_ref,:request_hash,'succeeded')
            ON CONFLICT(key) DO NOTHING RETURNING external_id"""),
                values,
            )
        ).scalar()
        if created:
            await c.execute(
                text("""INSERT INTO ledger(id,operation_id,amount_minor,currency)
                VALUES(:id,:external_id,:amount_minor,:currency)"""),
                values | {"id": uuid4()},
            )
        row = (
            (await c.execute(text("SELECT * FROM operations WHERE key=:key"), values))
            .mappings()
            .one()
        )
        conflict = (row["amount_minor"], row["currency"], row["method_ref"]) != (
            body.amount_minor,
            body.currency,
            body.method_ref,
        )
    return error("idempotency_conflict", 409) if conflict else envelope(row)


@app.get("/charges/by-key/{key}", dependencies=[Depends(credential)])
async def lookup(key: UUID, request: Request):
    async with request.app.state.engine.begin() as c:
        row = (
            (await c.execute(text("SELECT * FROM operations WHERE key=:key"), {"key": key}))
            .mappings()
            .one_or_none()
        )
        await c.execute(
            text("INSERT INTO calls(key,method_ref,kind) VALUES(:key,:ref,'lookup')"),
            {"key": key, "ref": row["method_ref"] if row else None},
        )
    return (
        envelope(row)
        if row
        else JSONResponse({"key": str(key), "status": "not_found"}, status_code=404)
    )


@app.put("/__test/fixtures/{method_ref}", dependencies=[Depends(controls)])
async def fixture(method_ref: str, body: Fixture, request: Request):
    try:
        valid_ref(method_ref)
    except ValueError:
        return error("validation_error", 422, fields=["method_ref"])
    async with request.app.state.engine.begin() as c:
        await c.execute(
            text("""INSERT INTO fixtures VALUES(:ref,'success')
            ON CONFLICT(method_ref) DO NOTHING"""),
            {"ref": method_ref},
        )
    return body.model_dump()


@app.get("/__test/stats", dependencies=[Depends(controls)])
async def stats(request: Request, key: UUID | None = None, method_ref: str | None = None):
    if (key is None) == (method_ref is None):
        return error("validation_error", 422, fields=["key", "method_ref"])
    clause = "key=:value" if key else "method_ref=:value"
    params = {"value": key if key else method_ref}
    async with request.app.state.engine.connect() as c:
        rows = (
            (await c.execute(text("SELECT * FROM operations WHERE " + clause), params))
            .mappings()
            .all()
        )
        ledger = (
            (
                await c.execute(
                    text(
                        """SELECT count(*) AS count,
            coalesce(sum(amount_minor),0) AS amount FROM ledger
            WHERE operation_id IN (SELECT external_id FROM operations WHERE """
                        + clause
                        + ")"
                    ),
                    params,
                )
            )
            .mappings()
            .one()
        )
        calls = (
            (
                await c.execute(
                    text(
                        "SELECT kind,count(*) AS n FROM calls WHERE " + clause + " GROUP BY kind"
                    ),
                    params,
                )
            )
            .mappings()
            .all()
        )
    counts = {r["kind"]: r["n"] for r in calls}
    return dict(
        operation_count=len(rows),
        ledger_count=ledger["count"],
        ledger_amount_minor=int(ledger["amount"]),
        create_calls=counts.get("create", 0),
        lookup_calls=counts.get("lookup", 0),
        operations=[{k: str(r[k]) for k in ["key", "external_id", "status"]} for r in rows],
    )
