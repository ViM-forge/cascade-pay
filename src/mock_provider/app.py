import asyncio
import secrets
from contextlib import asynccontextmanager
from typing import Literal
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
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
    charge_mode: Literal[
        "success",
        "declined_retryable",
        "declined_terminal",
        "not_charged_final",
        "pending",
        "charge_then_disconnect",
        "timeout_before_result",
        "malformed_response",
        "http_5xx",
    ]
    lookup_mode: Literal["normal", "temporary_not_found", "unavailable"] = "normal"
    lookup_not_found_count: int = Field(default=0, strict=True, ge=0, le=2147483647)
    barrier: Literal["before_charge_commit", "after_charge_commit_before_response"] | None = None


OPERATION_CODES = {
    "succeeded": None,
    "declined_retryable": "issuer_unavailable",
    "declined_terminal": "invalid_payment_method",
    "not_charged_final": "not_charged_final",
    "pending": None,
}


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


def payload_conflicts(row, body, fingerprint):
    return row["request_hash"] != fingerprint or (
        row["amount_minor"],
        row["currency"],
        row["method_ref"],
    ) != (body.amount_minor, body.currency, body.method_ref)


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
    engine = request.app.state.engine
    test_profile = request.app.state.settings.app_profile in {"test", "demo"}
    # Count authenticated requests independently of the financial transaction and
    # before a timeout barrier, including requests whose client disconnects.
    async with engine.begin() as c:
        await c.execute(
            text("INSERT INTO calls(key, method_ref, kind) VALUES(:key,:method_ref,'create')"),
            values,
        )
    async with engine.connect() as c:
        existing = (
            (await c.execute(text("SELECT * FROM operations WHERE key=:key"), values))
            .mappings()
            .one_or_none()
        )
        # Durable operations win before any fixture/barrier. Repeats must remain
        # canonical even when controls now select a timeout or a different mode.
        if existing:
            if payload_conflicts(existing, body, values["request_hash"]):
                return error("idempotency_conflict", 409)
            return envelope(existing)
        fixture_row = (
            (await c.execute(text("SELECT * FROM fixtures WHERE method_ref=:method_ref"), values))
            .mappings()
            .one_or_none()
        )
    mode = fixture_row["charge_mode"] if fixture_row and test_profile else "success"
    point = fixture_row["barrier"] if fixture_row and test_profile else None
    if mode == "timeout_before_result":
        point = "before_charge_commit"
    if point == "before_charge_commit":
        await wait_barrier(engine, body.key, point)
    async with engine.begin() as c:
        status = mode if mode in OPERATION_CODES else "succeeded"
        values |= dict(status=status, error_code=OPERATION_CODES[status])
        created = (
            await c.execute(
                text("""INSERT INTO operations
            (key,external_id,amount_minor,currency,method_ref,request_hash,status,error_code)
            VALUES(:key,:external_id,:amount_minor,:currency,:method_ref,:request_hash,:status,:error_code)
            ON CONFLICT(key) DO NOTHING RETURNING external_id"""),
                values,
            )
        ).scalar()
        if created and status == "succeeded":
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
        conflict = payload_conflicts(row, body, values["request_hash"])
    if point == "after_charge_commit_before_response":
        await wait_barrier(engine, body.key, point)
    if conflict:
        return error("idempotency_conflict", 409)
    # Only the initial request applies the fault; durable final operations never
    # re-run a fixture. The private marker is consumed by the loopback TCP proxy.
    if created and mode == "charge_then_disconnect":
        return Response(headers={"X-Mock-Disconnect": "after-commit"})
    if created and mode == "malformed_response":
        return Response(content="{invalid", media_type="application/json")
    if created and mode == "http_5xx":
        return error("temporarily_unavailable", 503)
    return envelope(row)


@app.get("/charges/by-key/{key}", dependencies=[Depends(credential)])
async def lookup(key: UUID, request: Request):
    hidden, unavailable = False, False
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
        if row and request.app.state.settings.app_profile in {"test", "demo"}:
            ref = {"ref": row["method_ref"]}
            mode = await c.scalar(
                text("SELECT lookup_mode FROM fixtures WHERE method_ref=:ref FOR UPDATE"), ref
            )
            unavailable = mode == "unavailable"
            if mode == "temporary_not_found":
                hidden = bool(
                    await c.scalar(
                        text("""UPDATE fixtures
                    SET lookup_not_found_count=lookup_not_found_count-1
                    WHERE method_ref=:ref AND lookup_not_found_count>0
                    RETURNING lookup_not_found_count+1"""),
                        ref,
                    )
                )
    if unavailable:
        return error("temporarily_unavailable", 503)
    if row and not hidden:
        return envelope(row)
    return JSONResponse({"key": str(key), "status": "not_found"}, status_code=404)


@app.put("/__test/fixtures/{method_ref}", dependencies=[Depends(controls)])
async def fixture(method_ref: str, body: Fixture, request: Request):
    try:
        valid_ref(method_ref)
    except ValueError:
        return error("validation_error", 422, fields=["method_ref"])
    async with request.app.state.engine.begin() as c:
        await c.execute(
            text("""INSERT INTO fixtures
            (method_ref,charge_mode,barrier,lookup_mode,lookup_not_found_count)
            VALUES(:ref,:charge_mode,:barrier,:lookup_mode,:lookup_not_found_count)
            ON CONFLICT(method_ref) DO UPDATE SET charge_mode=excluded.charge_mode,
            barrier=excluded.barrier, lookup_mode=excluded.lookup_mode,
            lookup_not_found_count=excluded.lookup_not_found_count"""),
            {"ref": method_ref} | body.model_dump(),
        )
    return body.model_dump()


class Resolve(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    status: Literal["succeeded", "declined_retryable", "declined_terminal", "not_charged_final"]
    error_code: str | None

    @model_validator(mode="after")
    def final_pair(self):
        if self.error_code != OPERATION_CODES[self.status]:
            raise ValueError("invalid final status/error pair")
        return self


@app.post("/__test/operations/{key}/resolve", dependencies=[Depends(controls)])
async def resolve(key: UUID, body: Resolve, request: Request):
    async with request.app.state.engine.begin() as c:
        row = (
            (
                await c.execute(
                    text("SELECT * FROM operations WHERE key=:key FOR UPDATE"), {"key": key}
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return error("operation_not_found", 404)
        if row["status"] != "pending":
            if (row["status"], row["error_code"]) != (body.status, body.error_code):
                return error("final_operation_conflict", 409)
            return envelope(row)
        updated = (
            (
                await c.execute(
                    text("""UPDATE operations SET status=:status,error_code=:error_code
                WHERE key=:key RETURNING *"""),
                    {"key": key} | body.model_dump(),
                )
            )
            .mappings()
            .one()
        )
        if body.status == "succeeded":
            await c.execute(
                text("""INSERT INTO ledger(id,operation_id,amount_minor,currency)
                VALUES(:id,:external_id,:amount_minor,:currency)"""),
                dict(
                    id=uuid4(),
                    external_id=row["external_id"],
                    amount_minor=row["amount_minor"],
                    currency=row["currency"],
                ),
            )
    return envelope(updated)


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


async def wait_barrier(engine, key, point):
    """Persist reached before waiting; never hold a DB transaction during the wait.

    before_charge_commit gates entry into the atomic charge transaction. After
    30 seconds without release, TimeoutError exits the handler: a before-barrier
    never enters charge, while an after-barrier keeps its committed operation.
    Timeout does not mark the barrier released or change operation/ledger data.
    """
    async with engine.begin() as c:
        await c.execute(
            text("""INSERT INTO barriers(key,point) VALUES(:key,:point)
            ON CONFLICT(key) DO NOTHING"""),
            {"key": key, "point": point},
        )
    async with asyncio.timeout(30):
        while True:
            async with engine.connect() as c:
                released = await c.scalar(
                    text("SELECT released FROM barriers WHERE key=:key"), {"key": key}
                )
            if released:
                return
            await asyncio.sleep(0.01)


@app.get("/__test/barriers/{key}", dependencies=[Depends(controls)])
async def barrier_state(key: UUID, request: Request):
    async with request.app.state.engine.connect() as c:
        row = (
            (await c.execute(text("SELECT * FROM barriers WHERE key=:key"), {"key": key}))
            .mappings()
            .one_or_none()
        )
    return dict(
        key=str(key),
        point=row["point"] if row else None,
        reached=row is not None,
        released=row["released"] if row else False,
    )


class Release(BaseModel):
    model_config = ConfigDict(extra="forbid")


@app.post("/__test/barriers/{key}/release", dependencies=[Depends(controls)])
async def release_barrier(key: UUID, body: Release, request: Request):
    async with request.app.state.engine.begin() as c:
        await c.execute(text("UPDATE barriers SET released=true WHERE key=:key"), {"key": key})
    return await barrier_state(key, request)
