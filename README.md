# Cascade Payments

Последовательный каскад: POST → PostgreSQL/outbox → Redis/Taskiq → worker →
HTTP mock A/B → GET с историей попыток. Доступны JWT, базовая идемпотентность,
устойчивые отказы и независимые ledger mock A/B. Автоматическая сверка unknown
и DB recovery ещё не реализованы. Реальные деньги не используются.

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
docker compose run --rm migrate-mock-a
docker compose run --rm migrate-mock-b
# Повтор каждой команды безопасен.
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
для повторного запуска. Mock имеют отдельные миграции operations/ledger/calls/fixtures.

API: Gunicorn запускает два `uvicorn_worker.UvicornWorker` без preload. Каждый
процесс создаёт engine/pool в FastAPI lifespan и освобождает при остановке.
Worker Taskiq и dispatcher — отдельные процессы того же образа; dispatcher публикует неопубликованный outbox с ограниченным timeout, затем отмечает published_at. Сессия SQLAlchemy создаётся отдельно для будущей единицы работы
(UoW — одной операции с общей транзакцией), общих глобальных сессий нет.
Ready проверяет PostgreSQL и Redis; live — только процесс. Mock для ready API
не требуются. Порты опубликованы только на loopback.

Stream payments, group payments-workers, manual ACK, без trim. Адаптер для
taskiq-redis 1.2.4 независимо каждые 2 с возвращает pending через XAUTOCLAIM
(idle 10 с, batch 100, cursor), передавая исходные bytes и ACK исходного ID.
Встроенный reclaim этой версии пропускается при пустом чтении новых сообщений;
поэтому адаптер нужен даже при manual ACK. При обновлении taskiq-redis пересмотрите
адаптер и повторите тест без новых публикаций.
Redis — транспорт, PostgreSQL — источник истины. Worker подтверждает сообщение только после commit результата или проверенного no-op.
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
docker compose run --rm -e RUN_INTEGRATION=1 -e API_URL=http://api:8000 api pytest -q
# Нарушает доступность зависимостей на время smoke-проверки:
python3.13 scripts/check_bootstrap.py
```

Без RUN_INTEGRATION integration/e2e явно пропускаются. DB smoke
проверяет уникальности, FK, диапазоны и BIGINT max в откатываемой транзакции.
Redis smoke оставляет исходное сообщение у умершего consumer и ждёт reclaim
без новых публикаций и DB recovery. Платёжные e2e отдельно проверяют повтор POST/Taskiq и ledger через HTTP.

Для локального API: `poetry run uvicorn payments.presentation.app:app`.
Settings явно читает `.env`; environment имеет приоритет. Локальная миграция:
`poetry run alembic upgrade head`. Compose переопределяет host URL адресами сервисов.

Остановка: `docker compose stop`; повторный старт — команда `up -d --wait`
выше. Миграции можно повторять. Не запускайте несколько bootstrap smoke
одновременно. Данные хранятся в volume PostgreSQL; JSON-логи приложения содержат событие, уровень, logger, PID и контекст обработки
payment_id/attempt_id/provider/duration/result; без произвольных исключений/credentials.

Контракты и будущие критерии: [решения](docs/decisions.md),
[провайдеры](docs/provider-contract.md), [приёмка](docs/acceptance.md).

Фактические результаты: [verification](docs/verification.md).

[Актуальные проблемы и правила их учёта](docs/known-issues.md).

## Первый платёж (D1)

После миграций и запуска выше выполните:

```sh
docker compose --profile demo run --rm demo first-success
```

CLI выпускает свежий JWT локально, создаёт уникальный ref, делает настоящий POST,
ждёт GET succeeded и проверяет HTTP stats провайдеров. Ожидается одна попытка A,
external_id, A ledger_count=1, ledger_amount_minor=123, B create_calls=0.
Повтор команды создаёт новый независимый платёж. В `.env.example` явно включён
APP_PROFILE=demo для mock fixtures/stats; без APP_PROFILE или с production эти
пути возвращают 404. Они требуют service credential, отдельный для A и B.
Payments-процессы не получают пароли БД провайдеров. Каждый mock получает только
свой DB пароль и собственный service credential.

Для ручного клиента выпустите токен (не помещайте его в логи или Git):

```sh
poetry run python scripts/issue_demo_token.py --merchant 1
poetry run python scripts/issue_demo_token.py --merchant 2
```

POST /payments принимает Bearer JWT, единственный Idempotency-Key из 1..128 видимых
ASCII символов и JSON с ровно amount_minor (положительный integer до BIGINT max),
currency (USD, регистр нормализуется), payment_method_ref (1..255 без пробелов и
управляющих). Возвращает 202 с id/status и Location. Равный повтор возвращает тот
же id, конфликт данных — 409. GET /payments/{id} доступен только своему merchant,
возвращает безопасную историю попыток. Точный контракт — в docs/decisions.md.

Проверки шага первого платежа с запущенными сервисами:

```sh
# Локально .env должен указывать на опубликованные PostgreSQL/Redis/mock порты.
# Для другого API_PORT задайте API_URL=http://127.0.0.1:<порт>.
RUN_INTEGRATION=1 poetry run pytest -q
# Host-only: временно останавливает только mock-a этого Compose проекта;
# создаёт отдельный production mock на свободном localhost:18001 и удаляет его.
poetry run python scripts/check_first_payment_runtime.py
```

Runtime-проверка подтверждает сохранность операции/ledger после restart, unknown
при недоступном A без вызова B, 404 controls вне demo/test и отсутствие проверяемых
секретов/ref в логах. Для Docker pytest задайте API_URL=http://api:8000.
Unknown пока остаётся unknown; автоматической сверки и восстановления потери
Redis нет. Поздний ответ после lease не сохраняется. Эти ограничения не дают
основания повторять платёж с новым клиентским ключом.


## Последовательный каскад (D2/D3)

```sh
docker compose --profile demo run --rm demo cascade-success
docker compose --profile demo run --rm demo all-declined
```

D2: A declined → B succeeded, ledger A=0/B=1, сумма B=123. D3: failed,
две declined attempt, ledger 0/0. Каждая команда использует новые ref/key.

Маршрут A → B сохраняется в routing_snapshot при создании платежа; worker читает
его из БД. Изменяемой настройки маршрута нет: контракт фиксирует A → B. B получает sequence=2 и собственный UUID/key. Под lock payment → attempt
отказ A, B created и outbox сохраняются одной транзакцией. Между провайдерами
нет открытой DB-транзакции. Дубли результата/задачи не создают ещё одну B;
терминальная задача делает no-op и ACK после commit проверенного no-op.

Переход разрешают только declined_retryable/issuer_unavailable и устойчивая
not_charged_final. Declined_terminal/invalid_payment_method завершает failed
без B. Исчерпание маршрута после разрешённых отказов тоже даёт failed.
Not_charged_final — неизменная операция, запрещающая любое будущее списание по
ключу; обычный lookup not_found такой гарантии не даёт. Timeout/disconnect,
pending, невалидный envelope, неизвестный status/error_code и неожиданный
HTTP-код оставляют unknown без B. HTTP-код сам по себе не бизнес-отказ.

Fixtures success/declined_retryable/declined_terminal/not_charged_final выбираются
только защищённым test/demo endpoint. Смена fixture действует на новые ключи;
старые финальные операции неизменны. Счётчики HTTP-вызовов и ledger независимы.
Обновление существующего окружения требует новой mock-миграции на обеих БД
(команды migrate-mock-a/migrate-mock-b выше); старая история сохранена.
