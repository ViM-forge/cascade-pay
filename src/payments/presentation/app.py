import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text

from payments.infrastructure.db import create_engine, session_factory
from payments.infrastructure.logging import configure_logging
from payments.infrastructure.settings import load_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = load_settings()
    engine = create_engine(settings)
    redis = Redis.from_url(
        settings.redis_url.get_secret_value(), socket_connect_timeout=2, socket_timeout=2
    )
    app.state.engine = engine
    app.state.sessions = session_factory(engine)
    app.state.redis = redis
    try:
        yield
    finally:
        await redis.aclose()
        await engine.dispose()


app = FastAPI(title="Cascade Payments (bootstrap)", lifespan=lifespan)


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
