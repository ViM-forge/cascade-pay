from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from payments.infrastructure.settings import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        connect_args={"timeout": 2, "command_timeout": 2},
        hide_parameters=True,
    )


def session_factory(engine: AsyncEngine) -> async_sessionmaker:
    # Each future UoW obtains its own session; never share sessions across tasks.
    return async_sessionmaker(engine, expire_on_commit=False)
