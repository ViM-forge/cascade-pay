# Cascade Payments

Последовательный каскад: POST → PostgreSQL/outbox → Redis/Taskiq → worker →
HTTP mock A/B → GET с историей попыток. Доступны JWT, конкурентная идемпотентность,
устойчивые отказы, независимые ledger mock A/B и ограниченная сверка unknown.
Восстановление незавершённой обработки идёт из PostgreSQL. Реальные деньги не используются.

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
Taskiq запускает два независимых worker-процесса (по одной активной async-задаче); dispatcher — отдельный процесс того же образа; dispatcher публикует неопубликованный outbox с ограниченным timeout, затем отмечает published_at. Сессия SQLAlchemy создаётся отдельно для каждой единицы работы
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
После потери Redis dispatcher повторно планирует незавершённую работу из PostgreSQL.

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
Unknown автоматически сверяется по сохранённому provider key. Expired in_flight переводится в сверку; потерянные доставки восстанавливаются из БД. Поздний ответ после lease не сохраняется. Эти ограничения не дают
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


## Конкуренция и повторная доставка (D5)

```sh
docker compose --profile demo run --rm demo duplicate-delivery
```

Десять конкурентных клиентских POST дают 202 и один payment UUID. Пока HTTP A
остановлен после provider commit, CLI отправляет три повторных Taskiq-задачи и
ждёт ACK хотя бы одной из них при всё ещё открытом HTTP. Затем явно освобождает
barrier, проверяет succeeded, одну attempt, повтор terminal и ledger A=1/123,
B calls=0. HTTP A остаётся один. Два worker-процесса нужны для воспроизведения
конкуренции; наличие нескольких процессов само по себе не является финансовой
гарантией. Схема и claim защищают данные независимо от процесса.

Mock barrier доступен только с соответствующим provider credential в test/demo:
fixture barrier=before_charge_commit или after_charge_commit_before_response,
GET /__test/barriers/{key}, POST /__test/barriers/{key}/release с `{}`.
Обе точки ждут без открытой транзакции, состояние переживает restart mock.
Ожидание ограничено 30 с; тест ждёт reached=true и освобождает в finally.
Доступны также fault modes и resolve, описанные в provider-contract.md.

Дополнительные host-only проверки (запускать **последовательно**, отдельно от pytest):

```sh
poetry run python scripts/check_idempotency_runtime.py
poetry run python scripts/check_first_payment_runtime.py
```

Первый скрипт останавливает только worker текущего Compose проекта, ставит дубли
до HTTP, запускает worker, проверяет конкурентный дубль во время B HTTP и разные
PID обработчиков. Всегда возвращает worker в healthy. Второй проверяет restart
mock с сохранённой БД, устойчивые финалы, production controls и безопасные логи.
Volumes не удаляются. Результаты — в docs/verification.md.

Клиентский ключ живёт в scope merchant весь срок данных. Hash не заменяет сравнение
нормализованных полей; одинаковый key у разных merchant создаёт разные платежи.
ON CONFLICT DO NOTHING при READ COMMITTED и отдельный SELECT читают победителя
без искусственного исключения/rollback. Переход к другому уровню изоляции требует
пересмотра этого протокола. Provider key — сохранённый UUID попытки; HTTP-повторы
могут увеличить create_calls, но не ledger. Provider idempotency защищает операцию,
а claim/lease/version координируют worker и защищают локальное состояние.


## Защита границы перед сверкой

HTTP-adapter принимает только пять канонических статусов операции провайдера;
внутренние unknown/not_found в HTTP 200 дают unknown без external_id.
Обычный not_found относится только к lookup 404, а не к charge-ответу.

Окно safe_retry_until ограничивает и первую отправку. Задержавшаяся created
при now >= safe_retry_until атомарно становится unknown с прежним key,
deadline/due и reconcile-outbox, без charge и без B. Время БД проверяется
после блокировки payment → attempt. Дубль не создаёт новую попытку/событие.
Dispatcher публикует process_payment и due reconcile_payment; расписание
сверки хранится в PostgreSQL и проверяется worker повторно.


## Потерянный ответ и ограниченная сверка (D4)

```sh
docker compose --profile demo run --rm demo lost-response
# Host-only; запускайте отдельно от pytest и других runtime smoke:
poetry run python scripts/check_reconciliation_runtime.py
```

D4 сначала получает наблюдаемый unknown с external_id=null после настоящего
разрыва TCP уже после commit провайдера, затем разрешает lookup и проверяет
succeeded: одна attempt A, ledger=1/123, create_calls=1, B calls=0.
Test/demo mock используют TCP front на 8000 и backend только на loopback;
в production launcher запускает Uvicorn напрямую. Proxy потребляет внутренний
post-commit marker и не отправляет клиенту ни status line, ни тело ответа.

Unknown не разрешает B. Lookup выполняется по сохранённому key без external_id.
Обычный not_found не означает отказ; только до safe_retry_until разрешён максимум
один charge в цикле с прежними UUID/key/payload. Pending и ошибка lookup charge
не вызывают. Not_charged_final — устойчивая финальная запись с запретом будущего
списания: в отличие от 404 она разрешает каскад. Policy process/reconcile общая.

До lookup коммитятся lease/version и check_count. Первая проверка через 1с,
затем 2/4/8/16с от результата, максимум пять проверок и deadline 60с от первого
unknown. Действующий владелец пятой проверки может сохранить финал после deadline,
пока lease действует; просроченный или чужой ответ не сохраняется. При исчерпании
платёж остаётся unknown/manual, next_check_at=NULL; новых HTTP и B нет. Перезапуск
не сбрасывает счётчик, deadline, окно повтора или manual marker.

Dispatcher выбирает due unknown без действующего lease, включая исчерпанные
независимо от due. Публикация имеет timeout; publish-before-mark сохранён.
При сбое событие/due остаётся доступно следующему проходу. Общий recovery также находит pending/created/expired in_flight после потери Redis. Taskiq manual ACK — после commit либо проверенного
no-op. Независимый periodic reclaim сохранён.

Ручной разбор: собрать payment/attempt/provider/key из защищённого хранилища,
сопоставить canonical lookup с независимым provider ledger и данными запроса.
Не менять статусы SQL вслепую и не повторять неизвестный платёж с новым клиентским
ключом: это создаст другое намерение списания. Время ожидания не доказывает отказ.
Финансового endpoint ручного разрешения нет; manual marker сам не снимается.
HTTP calls считают обращения, ledger считает списания — эти числа различаются.


## Восстановление и D6

Dispatcher раз в RECOVERY_INTERVAL_SECONDS (по умолчанию 2с) выбирает максимум
20 незавершённых платежей из PostgreSQL. Row locks с SKIP LOCKED позволяют
конкурентным сканам пропускать уже занятые платежи. Это простой polling,
без обещанного SLA: очередь и backlog влияют на время завершения.

Pending без попытки получает A. Created получает повторную задачу даже при
published_at у прежнего события. Expired in_flight получает unknown, новую
version и lookup прежнего key: ни возраст платежа, ни таймаут не разрешают B.
Processing без активной попытки восстанавливается по сохранённой истории;
только подтверждённый разрешённый отказ создаёт B.

Активный lease не меняется. Исчерпанные count/deadline после expiry дают manual
до due-времени, без шестого HTTP. Manual marker, budget, deadline и сохранённое
окно safe_retry_until не сбрасываются. Версия владельца и lease отсекают поздний
ответ. Изменение состояния и новое outbox-событие фиксируются одной транзакцией.
Повторное unpublished событие не добавляется; published событие можно заменить
новым сигналом для того же платежа. Publisher сначала отправляет, затем отмечает
published_at, с сетевым timeout. Ошибка оставляет событие; crash после отправки
допускает безопасный дубль. Exactly-once доставки нет.

D6 запускается **на хосте**, отдельно от pytest и других runtime-скриптов:

```sh
API_URL=http://127.0.0.1:18000 poetry run python scripts/demo.py restart-recovery
# та же полная матрица напрямую:
poetry run python scripts/check_recovery_runtime.py
```

Нужны Docker Compose и локальные зависимости Poetry; .env указывает на host
порты БД/Redis/mock, mock запущены с APP_PROFILE=demo/test. Скрипт останавливает
Compose worker/dispatcher, запускает настоящие локальные Taskiq/dispatcher,
убивает их группы процессов SIGKILL в известных точках, пересоздаёт Redis,
перезапускает API и mock. PostgreSQL volume не удаляется. В конце worker и
dispatcher Compose запускаются снова. Redis данного проекта очищается; выполняйте
проверку в выделенном демонстрационном окружении.

Матрица проверяет payment/attempt/key, GET после restart и независимые ledger,
crash после списания до result commit, активный lease, поздний ответ, пятый claim,
manual после restart и periodic reclaim исходного stream ID без recovery и новых
публикаций до и после result commit. В случае reclaim незавершённого in_flight
transport ACK no-op не заменяет последующий DB recovery.

Barriers недоступны через API: необходимы одновременно APP_PROFILE=test,
TEST_BARRIER_DIR и локальный файл <point>.<payment UUID>.arm. Достижение создаёт
.reached; удаление .arm освобождает только первого достигшего владельца.
Точки: before_payment_commit, after_payment_commit, after_prepare, after_claim,
before_result_commit, after_result_commit, before_publish, after_publish.
По умолчанию механизм выключен. Before_payment_commit намеренно удерживает
тестовую транзакцию; HTTP-провайдер вызывается только после commit claim.

Гарантия требует сохранённых PostgreSQL-данных и контракта устойчивого key/lookup
провайдера. Потеря PostgreSQL volume, реальные деньги и нагрузочный SLA не покрыты.
