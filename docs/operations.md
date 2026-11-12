# Запуск, разработка и обслуживание

Основной запуск и пять коротких демонстраций находятся в [README](../README.md).
Здесь — дополнительные действия. Все команды выполняются из корня проекта
в Unix shell. Windows/WSL пока не проверены.

## Настройки и данные

`scripts/configure_local.py` создаёт `.env` из `.env.example` со случайными
секретами и правами 0600. Он не перезаписывает существующий файл. Шаблон с
placeholder-секретами не подходит для запуска. `.env` исключён из Git и образа.

При ручной настройке замените все placeholders и согласуйте пароль payments
в `DATABASE_URL` и `PAYMENTS_DB_PASSWORD`. Пароли в URL требуют percent-encoding;
генератор использует URL-safe hex.

На новом PostgreSQL volume init-скрипт создаёт три БД и роли: payments,
mock_a, mock_b. Прямой доступ между ними закрыт. Платёжные процессы получают
только свой пароль БД, каждый mock — свой. Общий `env_file: .env` намеренно
не используется, чтобы не передать всем процессам все пароли.

**Изменение `.env` не меняет пароли уже созданных ролей PostgreSQL.** Для
повторного запуска используйте прежние настройки и сохранённый volume.
Не запускайте генератор заново поверх существующего окружения.
Миграции payments и обоих mock можно применять повторно командами из README.

Порты опубликованы только на loopback: PostgreSQL 5432, Redis 6379,
API 8000, mock A 8001, mock B 8002. Их нужно освободить до запуска.

## Процессы приложения

API работает под Gunicorn с двумя Uvicorn worker. Каждый создаёт свой DB pool
при старте; preload не используется. Taskiq запускает два отдельных worker-процесса,
по одной активной async-задаче в каждом. Dispatcher публикует outbox и запускает
DB recovery отдельно от API. Все три используют одну кодовую базу и Docker image.
SQLAlchemy-сессия создаётся для каждой единицы работы (UoW), не разделяется
между конкурентными задачами.

Версии образов заданы в Dockerfile/Compose: Python 3.13.9, PostgreSQL 17.6,
Redis 7.4.6. Детали ACK, возврата pending-сообщений и адаптера stream broker —
в [решениях](decisions.md#брокер-ack-и-повторная-доставка).

## Если порт API занят

Добавьте в `.env`:

```dotenv
API_PORT=18000
```

Пересоздайте API с новым опубликованным портом:

```sh
docker compose up -d --wait api
curl --fail http://127.0.0.1:18000/health/ready
export API_URL=http://127.0.0.1:18000
```

`API_PORT` задаёт внешний порт контейнера; `API_URL` сообщает локальным тестам
и runtime-скриптам, куда обращаться. Это разные настройки.
Внутри Compose адрес всегда `http://api:8000`, поэтому контейнерные D1–D5
и Docker pytest не требуют изменения URL.

Для стандартного порта перед host-only проверками явно задавайте:

```sh
export API_URL=http://127.0.0.1:8000
```

Это важно: внутренний default HTTP-тестов и большинства runtime-скриптов — 18000.
Оболочка `demo-recovery.sh` по умолчанию выбирает 8000.

## Локальная разработка

Нужны Python 3.13 и Poetry 2.2.1; команда установки Poetry ниже использует uv.
Версии зависимостей и хеши закреплены в `poetry.lock`.

```sh
uv tool install poetry==2.2.1 --python python3.13
POETRY_VIRTUALENVS_IN_PROJECT=true poetry env use python3.13
poetry sync --with dev
poetry check
poetry run ruff check .
poetry run ruff format --check .
poetry run pytest -q
```

Последняя команда без `RUN_INTEGRATION=1` проверяет тесты без внешнего окружения;
интеграционные проверки явно пропускаются. Для полного запуска нужны работающие
Compose-сервисы с миграциями и корректные host URL БД/Redis/mock в `.env`:

```sh
RUN_INTEGRATION=1 API_URL="${API_URL:-http://127.0.0.1:8000}" poetry run pytest -q
```

Локальный API: `poetry run uvicorn payments.presentation.app:app`.
Локальная миграция: `poetry run alembic upgrade head`.
Перед запуском API освободите его порт, если там уже работает Compose API.
Settings читает `.env`; переменные окружения имеют приоритет.
Compose подставляет внутренние адреса сервисов вместо host URL.

## Ручной запрос к API

Для этого примера нужны локальные зависимости из предыдущего раздела.
CLI выпускает JWT первого тестового merchant на срок до 15 минут.
Вместо банковских реквизитов передаётся только тестовый `payment_method_ref`.

```sh
DEMO_TOKEN=$(poetry run python scripts/issue_demo_token.py --merchant 1)
PAYMENT_ID=$(curl --fail --silent "${API_URL:-http://localhost:8000}/payments" \
  -H "Authorization: Bearer $DEMO_TOKEN" \
  -H "Idempotency-Key: manual-$(python3.13 -c 'import uuid; print(uuid.uuid4())')" \
  -H 'Content-Type: application/json' \
  -d '{"amount_minor":123,"currency":"USD","payment_method_ref":"manual-demo"}' \
  | python3.13 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
curl --fail --silent "${API_URL:-http://localhost:8000}/payments/$PAYMENT_ID" \
  -H "Authorization: Bearer $DEMO_TOKEN"
unset DEMO_TOKEN
```

GET можно повторять до итогового состояния. Первый GET может ещё показывать
`pending` или `processing`: POST не ждёт внешнего списания.
Для второго merchant используйте `--merchant 2`; чужой платёж ему недоступен.
Не включайте shell tracing (`set -x`) и не сохраняйте JWT в Git или логах.

## Наблюдение за работой

```sh
docker compose ps
docker compose logs --tail=50 api worker dispatcher
curl --fail "${API_URL:-http://localhost:8000}/health/live"
curl --fail "${API_URL:-http://localhost:8000}/health/ready"
curl --fail http://localhost:8001/health/live
curl --fail http://localhost:8002/health/live
```

| Проверка | Что она подтверждает |
|---|---|
| API live | Процесс API отвечает |
| API ready | API имеет доступ к PostgreSQL и Redis; при недоступности отвечает 503 |
| Worker health | Свежесть heartbeat чтения Redis |
| Dispatcher health | Наличие процесса |
| Mock live/ready | HTTP-процесс отвечает; БД не проверяется |

Healthy не доказывает успешное списание. Для этого нужны GET платежа и проверки
provider ledger в CLI/e2e. В JSON-логах есть событие, уровень, logger, PID и
контекст payment/attempt/provider/duration/result без сырых тел и credentials.

## Аварийные проверки D6

Запускайте только в выделенном demo-окружении, **последовательно и отдельно от
pytest и других runtime-скриптов**. Нужны Unix, Docker Compose, локальные зависимости
Poetry, host URL в `.env` и mock с `APP_PROFILE=demo` или `test`.

```sh
API_URL="${API_URL:-http://127.0.0.1:8000}" sh scripts/demo-recovery.sh
```

Это полная матрица из 11 случаев, а не короткий restart. Скрипт останавливает
Compose worker/dispatcher, запускает локальные процессы, убивает их SIGKILL
в заданных точках, пересоздаёт Redis и перезапускает API/mock. PostgreSQL volume
не удаляется; в конце Compose worker/dispatcher запускаются снова.
**Redis выбранного Compose-проекта очищается.**

Проверяются сбои до/после публикации, до HTTP, после списания до сохранения
результата, до ACK, поздний ответ владельца и границы последней сверки.
Полная связь cases с гарантиями — в [матрице приёмки](acceptance.md).
Тот же набор напрямую: `poetry run python scripts/check_recovery_runtime.py`;
перед ним явно экспортируйте `API_URL` согласно разделу о портах.

Дополнительные проверки, также по очереди:

```sh
export API_URL=http://127.0.0.1:8000
poetry run python scripts/check_idempotency_runtime.py
poetry run python scripts/check_first_payment_runtime.py
poetry run python scripts/check_reconciliation_runtime.py
python3.13 scripts/check_bootstrap.py --api-port 8000
```

Для другого API-порта измените export и `--api-port`. Bootstrap также использует
стандартные mock-порты 8001/8002; first-payment runtime требует свободный 18001.
Эти скрипты временно меняют доступность процессов/зависимостей:

| Скрипт | Что проверяет |
|---|---|
| `check_idempotency_runtime.py` | Дубли до HTTP, конкурентный дубль B и разных обработчиков; перезапускает worker |
| `check_first_payment_runtime.py` | Сохранность операций после restart mock, закрытые production controls, безопасные логи |
| `check_reconciliation_runtime.py` | Пять lookup, остановку автоматики после лимита и устойчивость финалов |
| `check_bootstrap.py` | Health при отключении БД/Redis, процессы, остановку и сохранность данных после restart |

## Неизвестный результат и ручной разбор

`unknown` означает, что подтверждённого исхода пока нет. Это не отказ.
После исчерпания сверки сохраняется `manual_review_required=true`;
автоматика больше не вызывает HTTP и не переключается на B.

Для разбора нужно сопоставить payment, attempt и provider key из защищённого
хранилища с lookup и журналом списаний провайдера. Не меняйте SQL-статус вслепую
и не создавайте повтор с новым клиентским ключом: это другое платёжное намерение.
Endpoint ручного разрешения в MVP нет; marker автоматически не снимается.

## Полный сброс demo

Обычная остановка `docker compose stop` и `docker compose down` сохраняют volume.
Если данные выделенного demo-проекта больше не нужны, полный сброс:

```sh
# Удаляет PostgreSQL volume, все платежи и журналы списаний этого проекта.
docker compose down -v --remove-orphans
```

После сброса повторите миграции и запуск из README.
Не используйте эту команду для окружения, данные которого нужно сохранить.
