FROM python:3.13.9-slim-bookworm
ARG POETRY_VERSION=2.2.1
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    POETRY_NO_INTERACTION=1 POETRY_VIRTUALENVS_IN_PROJECT=true \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app
RUN pip install --no-cache-dir "poetry==$POETRY_VERSION"
COPY pyproject.toml poetry.lock ./
RUN poetry sync --no-root
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./
COPY tests ./tests
RUN poetry install --only-root && useradd --create-home app && chown -R app:app /app
USER app
CMD ["gunicorn", "payments.presentation.app:app", "--bind", "0.0.0.0:8000", "--workers", "2", "--worker-class", "uvicorn_worker.UvicornWorker", "--graceful-timeout", "10"]
