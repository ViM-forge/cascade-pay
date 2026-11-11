# Cascade Payments

Воспроизводимое окружение для будущего каскада платежей. Сейчас доступны только
`GET /health/live` и `/health/ready`; платежей, списаний, JWT endpoints, publisher
и DB recovery пока нет. Mock A/B предоставляют только health.

Нужны Docker Desktop/Engine с Compose v2. Для локальной разработки — Python 3.13
и Poetry **2.2.1** (для установки командой ниже нужен uv). В Docker: Python 3.13.9, PostgreSQL 17.6, Redis 7.4.6.
Python-зависимости и хеши пакетов закреплены в `poetry.lock`.

## Запуск

Из корня репозитория:

```sh
python3.13 scripts/configure_local.py
docker compose config -q
docker compose build
docker compose up -d --wait postgres redis
docker compose run --rm migrate
docker compose run --rm migrate
docker compose up -d --wait api worker dispatcher mock-a mock-b
curl --fail http://localhost:8000/health/live
curl --fail http://localhost:8000/health/ready
curl --fail http://localhost:8001/health/live
curl --fail http://localhost:8002/health/live
```

Генератор создаёт `.env` из `.env.example`, генерирует локальные случайные секреты,
ставит права 0600 и отказывается перезаписывать существующий файл. Шаблон с
placeholder-секретами запускать нельзя. `.env` не входит в Git или Docker image.
Для самостоятельной подготовки скопируйте шаблон и замените все placeholders,
согласовав пароль payments в `DATABASE_URL` и `PAYMENTS_DB_PASSWORD`.
URL-пароли должны быть percent-encoded; генератор использует URL-safe hex.

Если порт 8000 занят, добавьте `API_PORT=18000` в `.env`, используйте этот порт
в curl и передайте `--api-port 18000` скрипту check_bootstrap.py.

Init-скрипт выполняется PostgreSQL только на новом volume. Он создаёт отдельные
БД и роли payments/mock_a/mock_b и закрывает межбазовый CONNECT. Payments-процессы
получают только свои DB credentials, mock — свои. Общий `env_file: .env` здесь
не используется: он передал бы платежным процессам также admin/mock DB пароли
и позволил бы обойти изоляцию ролей. Общие настройки заданы одним YAML anchor.
Изменение `.env` после
инициализации не меняет пароли существующих ролей. Обычный restart сохраняет
volume; `docker compose down` тоже сохраняет его. Не используйте `down -v`
для повторного запуска. Mock-миграции и ledger появятся вместе с их поведением.

API: Gunicorn запускает два `uvicorn_worker.UvicornWorker` без preload. Каждый
процесс создаёт engine/pool в FastAPI lifespan и освобождает при остановке.
Worker Taskiq и dispatcher — отдельные процессы того же образа; dispatcher пока
ожидает сигнал. Сессия SQLAlchemy создаётся отдельно для будущей единицы работы
(UoW — одной операции с общей транзакцией), общих глобальных сессий нет.
Ready проверяет PostgreSQL и Redis; live — только процесс. Mock для ready API
не требуются. Порты опубликованы только на loopback.

Stream payments, group payments-workers, manual ACK, без trim. Адаптер для
taskiq-redis 1.2.4 независимо каждые 2 с возвращает pending через XAUTOCLAIM
(idle 10 с, batch 100, cursor), передавая исходные bytes и ACK исходного ID.
Встроенный reclaim этой версии пропускается при пустом чтении новых сообщений;
поэтому адаптер нужен даже при manual ACK. При обновлении taskiq-redis пересмотрите
адаптер и повторите тест без новых публикаций.
Redis — транспорт, PostgreSQL — источник истины. Платёжные handlers и ACK после
DB commit будут добавлены вместе с обработкой; сейчас worker не списывает деньги.
Потеря Redis не компенсируется на этом этапе: DB recovery ещё не реализован.

## Локальная разработка и проверки

```sh
uv tool install poetry==2.2.1 --python python3.13
POETRY_VIRTUALENVS_IN_PROJECT=true poetry env use python3.13
poetry sync
poetry check
poetry run ruff check .
poetry run ruff format --check .
poetry run pytest -q
# При работающих Compose PostgreSQL/Redis и применённых миграциях:
RUN_INTEGRATION=1 poetry run pytest -q
# Или все тесты из общего Docker image:
docker compose run --rm -e RUN_INTEGRATION=1 api pytest -q
# Нарушает доступность зависимостей на время smoke-проверки:
python3.13 scripts/check_bootstrap.py
```

Без RUN_INTEGRATION два теста настоящих БД/Redis явно пропускаются. DB smoke
проверяет уникальности, FK, диапазоны и BIGINT max в откатываемой транзакции.
Redis smoke оставляет исходное сообщение у умершего consumer и ждёт reclaim
без новых публикаций и DB recovery. Это ещё не проверка платёжной идемпотентности.

Для локального API: `poetry run uvicorn payments.presentation.app:app`.
Settings явно читает `.env`; environment имеет приоритет. Локальная миграция:
`poetry run alembic upgrade head`. Compose переопределяет host URL адресами сервисов.

Остановка: `docker compose stop`; повторный старт — команда `up -d --wait`
выше. Миграции можно повторять. Не запускайте несколько bootstrap smoke
одновременно. Данные хранятся в volume PostgreSQL; JSON-логи приложения содержат
только тип события, уровень, logger и PID, без произвольных исключений/credentials.

Контракты и будущие критерии: [решения](docs/decisions.md),
[провайдеры](docs/provider-contract.md), [приёмка](docs/acceptance.md).

Фактические результаты: [verification](docs/verification.md).

[Актуальные проблемы и правила их учёта](docs/known-issues.md).
