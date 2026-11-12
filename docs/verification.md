# Фактические проверки

## Ранее выполнено: окружение — 2026-11-11

Этот исторический раздел относится только к bootstrap. Проверки первого платежа
приведены отдельно ниже; результаты последующих сценариев здесь не заявляются.

Проверено на macOS arm64, локальный Python 3.13.13, Docker Linux arm64 с Python
3.13.9, Poetry 2.2.1, PostgreSQL 17.6 и Redis 7.4.6. Для проверки создан новый
Compose project `cascade-bootstrap-check` с отдельным PostgreSQL volume;
API использовал порт 18000, так как 8000 занят сторонним процессом.

| Фактически выполненная проверка | Результат |
|---|---|
| Poetry lock/install, затем poetry sync из lock | Успех, lock не переразрешался при запуске |
| Metadata Requires-Python всех прямых зависимостей и Poetry через versioned PyPI JSON | Python 3.13.9 входит в заявленные диапазоны |
| poetry check | Успех |
| poetry run ruff check . / ruff format --check . | Успех |
| docker compose config -q / build | Успех |
| Новый PostgreSQL volume, SQL init трёх ролей/БД | Успех; CONNECT каждой роли только к своей БД |
| docker compose run --rm migrate, повтор и повтор после restart | Успех, revision 0001 |
| up -d --wait API/worker/dispatcher/mock A/B | Все healthy |
| pytest без integration | 16 passed, 2 явно skipped |
| RUN_INTEGRATION=1 poetry run pytest -q | 18 passed |
| docker compose run --rm -e RUN_INTEGRATION=1 api pytest -q | 18 passed на Python 3.13.9 |
| SQL smoke | Merchant/key UNIQUE, sequence UNIQUE, active partial UNIQUE, FK, amount/check_count/key CHECK; BIGINT max сохранён без float; транзакция откатилась |
| Redis smoke | Одно сообщение у умершего consumer; без новых XADD и DB recovery возвращено исходными bytes/ID через 10+ с, ACK удалил pending, stream length=1 |
| python3.13 scripts/check_bootstrap.py --api-port 18000 | Health API/mock; master + два HTTP worker; при stop Redis и PostgreSQL live=200/ready=503; после start ready=200 |
| Та же smoke-команда | Сохранённая строка пережила restart PostgreSQL; пять application-контейнеров остановились с exit 0, без OOM/SIGKILL; повторный up healthy |
| Проверка итогового Compose env и логов | API/worker/dispatcher не получают admin/provider DB credentials; сгенерированные секреты в логах не найдены |

Первый init через shell bind mount завершился ошибкой bad interpreter/Permission
denied. Заменён SQL init-файлом; проверка нового volume после исправления успешна.
Прежний частично инициализированный volume сохранён, не удалялся. Первая версия
smoke ошибочно считала процессы по argv[0]; после учёта Python launcher проверка
двух HTTP worker прошла. Все тестовые строки smoke удалены после проверки.

Ограничения: не тестировались x86_64, нагрузка, утрата PostgreSQL, платёжные
операции/конкуренция/финансовое recovery. Redis тест доказывает возврат транспортных
bytes и ACK, а не отсутствие повторного списания. Docker tags и lock фиксируют
версии, но доступ к registry/PyPI при первой установке всё ещё нужен.


## Новые проверки: первый сквозной платёж — 2026-11-11

Использован существующий Compose project `cascade-bootstrap-check`, API 18000,
локальные PostgreSQL/Redis/mock HTTP порты. Старые volumes сохранены; чужой процесс
на 8000 не останавливался. Зависимости/lock не обновлялись.

| Фактически выполненная проверка | Результат |
|---|---|
| Git status/log, Compose ps, версии через importlib.metadata | Исходно дерево чистое; два коммита; семь healthy сервисов |
| Poetry check, Ruff lint и format | Успех |
| Compose config/build | Успех; scripts и отдельные mock migrations включены в образ |
| Payments upgrade head, повтор | Успех, revision 0001 |
| migrate-mock-a / migrate-mock-b и повтор | Успех, mock_0001 в каждой собственной БД |
| RUN_INTEGRATION=1 poetry run pytest -q | 40 passed, без skipped; настоящий HTTP, PostgreSQL, Redis, Taskiq |
| Domain entities | Успех/unknown, запрет terminal transitions/claim unknown, lease boundary/version, frozen/identity, строгие входные значения |
| HTTP POST → 202 → poll GET succeeded | Одна попытка A с external_id; A ledger_count=1, сумма=123; B create_calls=0 |
| Повтор POST и конфликт данных | Тот же id/202/current status; несовпадение →409 |
| Повтор терминальной задачи через Taskiq | Доставка и ACK наблюдены в stream/group; ledger=1, create_calls остался 1 |
| Исключение до DB commit | Задача с некорректным UUID осталась pending; только этот тестовый poison message удалён после проверки |
| Provider HTTP repeat/conflict/lookup | Канонический ответ совпал; три create_calls, один ledger; другой payload →409 |
| JWT negatives, claims, merchant isolation, body/header validation | 401/404/422 по контракту, безопасные коды без эха payload |
| check_first_payment_runtime.py | Успех: restart сохраняет canonical lookup и ledger; повтор charge после restart ledger не увеличил |
| Недоступный mock A | GET unknown, external_id=null, B create_calls=0; автоматическая сверка не выполнялась |
| Отдельный production mock | Test fixtures/stats возвращают 404; контейнер проверки остановлен/удалён, volume сохранён |
| Безопасность | Чужой provider credential →401; публичный ответ не содержит ref/key/hash/lease/version; проверяемые credentials/JWT/ref отсутствуют в логах |
| check_bootstrap.py --api-port 18000 | CONNECT isolation, health, два HTTP worker, ready=503 при stop Redis/PostgreSQL, graceful exit 0, restart с сохранённой строкой |
| docker compose --profile demo run --rm demo first-success | succeeded, A ledger=1/123, B calls=0; CLI выпускает свежий JWT |

В первом запуске mock-миграций новая конфигурация ошибочно указывала на каталог
payments; после исправления повторные миграции прошли. Первые unit health проверки
ожидали bootstrap 404 для новых POST; ожидания обновлены под авторизацию.
Первоначальные ошибки Ruff исправлены и проверки повторены. Это промежуточные
ошибки разработки, а не нерешённые проблемы.

Не проверялись и не реализованы D2–D6 целиком, конкурентная financial fault-matrix,
автоматическая сверка, DB recovery после потери Redis, real provider/real money,
нагрузка, x86_64 и чистый checkout с новым volume для полной сдачи. Базовый повтор
терминальной задачи здесь не означает завершение сценария конкуренции D5.
Unknown и expired in_flight пока требуют будущих сверки/recovery; их нельзя считать
failed или основанием для нового клиентского ключа. Список актуальных проблем —
[known-issues.md](known-issues.md).


## Новые проверки: последовательный каскад — 2026-11-12

Проверки ниже выполнены заново; исторические результаты выше не включены в число
новых тестов. Использован существующий `cascade-bootstrap-check`, API 18000,
исключённый .env. Существующие volumes сохранены, чужой процесс на 8000 не затронут.

| Команда / сценарий | Наблюдаемый результат |
|---|---|
| `poetry check` | Успех, зависимости/lock не обновлялись |
| `poetry run ruff check .`, `poetry run ruff format --check .` | Успех |
| `docker compose config -q`, `docker compose build api`, `up -d --wait api worker dispatcher mock-a mock-b` | Успех |
| `docker compose run --rm migrate`, `migrate-mock-a`, `migrate-mock-b`; каждая повторена | Успех; payments 0001, mock A/B mock_0002; прежние данные сохранены |
| `RUN_INTEGRATION=1 poetry run pytest -q` | 100 passed, 0 skipped |
| Docker pytest с `RUN_INTEGRATION=1`, `API_URL=http://api:8000` | 100 passed, 0 skipped; повтор итогового образа подтвердил тот же результат |
| Policy unit | Все результаты на A/B; исчерпание, terminal, pending/unknown/not_found; неверные пары, неактивные состояния, чужой payment/неверный provider, fencing/lease граница |
| Adapter unit с настоящим локальным HTTP | 23 варианта: канонический envelope, B URL/credential/payload, ошибки полей, malformed JSON, HTTP 201/400/404/500, disconnect, общий timeout |
| HTTP e2e A success | Одна A succeeded, A ledger=1/321, B create_calls=0 |
| HTTP e2e A retryable → B success | A/B sequence 1/2, разные UUID/key, A declined/B succeeded, ledger=0/1, сумма B=321 |
| HTTP e2e оба retryable | Failed, две declined, ledger=0/0 |
| HTTP e2e terminal A | Failed, одна declined, B calls=0 |
| HTTP e2e not_charged_final A | B succeeded, ledger=0/1 |
| Terminal Taskiq repeat для всех пяти сценариев | Доставка и ACK наблюдены в Redis; GET неизменен, новых charge/ledger нет |
| Повтор отказа, смена fixture на success, иной payload | Старый canonical envelope неизменен; ledger=0; конфликт 409; create_calls=3 |
| `poetry run python scripts/check_bootstrap.py --api-port 18000` | CONNECT isolation; два HTTP worker; live=200/ready=503 при stop Redis/PostgreSQL; graceful exit 0; restart сохраняет строку |
| `poetry run python scripts/check_first_payment_runtime.py` | Success и все три отказа сохраняются после restart mock; смена fixture и повтор не меняют финал/ledger; outage A → unknown без B; обычный lookup not_found не меняет unknown и не вызывает B; production controls 404, безопасные логи |
| CLI `first-success`, `cascade-success`, `all-declined` через demo profile | Все exit 0; ledger 1/0, 0/1, 0/0; сумма успешного списания 123 |
| `git diff --check` | Успех; commit/amend/push/staging не выполнялись, .gitignore сохранён |

Adapter unit не подменяет финансовое доказательство: его результаты относятся
только к преобразованию HTTP в доменный результат. Списания в e2e/CLI доказаны
отдельным HTTP ledger настоящих mock. Обычный not_found проверен также реальным
mock lookup после outage; автоматического lookup самим worker ещё нет.

Сохранённый routing_snapshot прочитан из payments и используется для выбора
первого/следующего provider. Изменяемой настройки маршрута в текущем контракте
нет: модель и CHECK допускают только A → B. Поэтому сценарий изменения такой
настройки не выполнен. Пользователь подтвердил сохранение фиксированного A → B. Проверены чтение
snapshot из БД, его сериализация/восстановление, неизменность и выбор B из snapshot;
изменяемая настройка не вводилась. Это согласованная граница проверки.

Не реализованы и не названы пройденными: автоматическая сверка, DB recovery,
полная конкурентная идемпотентность, financial fault-matrix, D4–D6,
финальная проверка чистого checkout на новом volume. Это границы следующих этапов.


Финальное уточнение: пользователь выбрал сохранение фиксированного A → B.
После добавления явного unit-теста неизменности/восстановления snapshot локальный
полный набор показал **101 passed, 0 skipped**. Итоговые Poetry/Ruff/format и
`git diff --check` прошли. После изменения контекста логирования повторный
runtime script также прошёл: устойчивые отказы после restart, not_found/unknown
без B, production controls и safe logs. Последний Git-коммит остался 27cf53f;
новый коммит агентом не создавался.

Итоговый Docker pytest с RUN_INTEGRATION=1: **101 passed, 0 skipped**.

## Новые проверки: идемпотентность и конкуренция — 2026-11-12

Это новые запуски на рабочем дереве поверх существующего пользовательского
коммита `5d26b43`; исторические 101 тест выше не подменяют текущие результаты.
На старте Git чистый, семь сервисов `cascade-bootstrap-check` healthy, API 18000.
Порт 8000 по-прежнему занят чужим Python-процессом; он не останавливался.
Volumes `cascade-bootstrap-check_postgres-data` и `cascade-payments_postgres-data`
сохранены. Исключённый .env, .gitignore, зависимости и poetry.lock не менялись.

Установленные версии проверены importlib.metadata: SQLAlchemy 2.0.57,
asyncpg 0.32.0, Taskiq 0.13.0, taskiq-redis 1.2.4, httpx 0.28.1,
Pydantic 2.14.3, Alembic 1.20.1. Новый mock schema revision — mock_0003;
payments schema по-прежнему достаточна (0001).

| Фактически выполненная проверка | Результат |
|---|---|
| `poetry check`, `ruff check .`, `ruff format --check .` | Успех |
| `docker compose config -q`, build и up --wait пяти application services | Успех |
| `compose run --rm migrate`, `migrate-mock-a`, `migrate-mock-b`, каждая дважды | Успех; реальные revision SELECT: 0001/mock_0003/mock_0003 |
| `RUN_INTEGRATION=1 poetry run pytest -q` | **123 passed, 0 skipped**; последний повтор после уточнения ACK: 21.03 с |
| `compose run --rm -e RUN_INTEGRATION=1 -e API_URL=http://api:8000 api pytest -q` | **123 passed, 0 skipped**; последний повтор: 23.43 с |
| 10 конкурентных одинаковых POST | Все 202, один UUID, одна payment/attempt/outbox |
| 10 конкурентных POST с двумя payload | Победившие данные 202/один UUID, остальные 409; нормализованный terminal repeat совпадает |
| Same key у двух merchant | Разные платежи; чужой GET остаётся 404 |
| Fake equal hash, разные поля | 409; hash не заменяет поля |
| D5 на before_charge_commit и after_charge_commit_before_response | Настоящий HTTP/Taskiq, ACK дубля при открытом исходном HTTP; после release одна A, calls=1, ledger=1/123, B=0 |
| Stale worker HTTP result: другой version или истёкший lease | После доставки/ACK платёж не изменён ответом, external_id не сохранён, provider ledger=1/345, одна attempt |
| DB finish с чужим UUID, version, expired lease | False/no-op; без ложного финала |
| DB lock wait | Через pg_blocking_pids наблюдено ожидание payment lock; lease истёк до получения lock, ответ не сохранён |
| Повтор результата A → B, затем старые результаты после terminal B | Две attempt/outbox, terminal succeeded не перезаписан |
| Direct concurrent HTTP к mock A и B: success и три отказа | На каждый сценарий 10 конфликтующих + 10 одинаковых запросов; одна canonical operation, 20 create_calls, 1 lookup; ledger=1 только для success, отказы=0 |
| Credentials barrier | Нет credential/credential другого provider →401 |
| `check_idempotency_runtime.py` | Три задачи до первого HTTP; ещё дубли при открытом B; разные PID 8/9 (ранее 6/7 и 7/8); calls A/B=1/1, ledger=0/1; после доставки две attempt/outbox |
| Тот же runtime: конкурентный direct charge и restart mock A | 10 одинаковых HTTP, затем restart и повтор: прежний canonical external_id, create_calls=11, ledger=1/567 |
| `check_first_payment_runtime.py` | Success и все отказы после restart неизменны; unknown/not_found без B; production controls, включая barrier GET/release, →404; safe logs |
| `check_bootstrap.py --api-port 18000` | CONNECT isolation, два HTTP worker, live=200/ready=503 при недоступности Redis/PostgreSQL; graceful exit 0; строка/volumes сохранены после restart |
| CLI `first-success` | exit 0; A succeeded, ledger=1/0, сумма=123 |
| CLI `cascade-success` | exit 0; A declined/B succeeded, ledger=0/1, сумма=123 |
| CLI `all-declined` | exit 0; failed, две declined, ledger=0/0 |
| CLI `duplicate-delivery` | exit 0; 10 concurrent POST →один UUID, Taskiq no-op ACK при barrier; terminal repeat, calls=1/0 и ledger=1/0 |
| `git diff --check` | Успех; commit/amend/push/staging не выполнялись |

Применённые миграции не переписывались. Два Taskiq worker-процесса создают
собственные engine/pool в startup, имеют по одной активной async-задаче; API
остаётся под Gunicorn/uvicorn-worker без preload. HTTP выполняется вне DB
транзакции, lock payment → attempt, manual ACK после commit/no-op,
publish-before-mark, bounded HTTP timeout и periodic reclaim сохранены.
Regression включает исключение до commit без ACK, JWT/строгий ввод/безопасные
ответы, transport reclaim без нового трафика и предыдущие D1–D3.

Промежуточные ошибки не скрываются: первый запуск дал 109 passed и два failed
из-за подготовки новых DB-тестов (raw lease UPDATE до flush claim и UUID без
соответствующего provider key). Исправлена подготовка, рабочий механизм не
переписывался. Runtime сначала пережидал HTTP timeout при ожидании health,
затем ждал дубль, попавший в очередь занятого worker. Исправлено наблюдение
barrier и ожидание ACK любого нового дубля; после release проверяются все.
Неудачные прогоны оставили unknown с реальным ledger — они не названы успехом
или отказом. В одном промежуточном прогоне pytest начат до финального результата
runtime; итоговые runtime, CLI и pytest выполнены последовательно.

Границы доказанного: финансовые assertions читают независимый provider ledger
через HTTP. Дополнительные DB-тесты с принудительным lease/version и откатом
не являются financial recovery. Изменение уровня изоляции требует пересмотра
ON CONFLICT/SELECT протокола. Provider idempotency не заменяет владение worker.
Автоматическая сверка unknown, DB recovery, D4/D6, полная fault-matrix, нагрузка,
реальный provider и финальная проверка чистого checkout не реализованы и не
объявлены пройденными. Это согласованные границы последующих этапов.
Актуальных подтверждённых нерешённых проблем не выявлено — [known issues](known-issues.md).


## Текущая валидация перед сверкой unknown — 2026-11-12

Это новый запуск baseline, отдельно от исторических результатов выше. Реализация
автоматической сверки ещё не добавлена. HEAD на старте: 3157f5a; рабочее дерево
было чистым. Коммиты/публикации агент не выполнял.

Окружение проверено: Poetry 2.2.1, host Python 3.13.13 / Docker Python 3.13.9,
PostgreSQL 17.6 / READ COMMITTED, Redis 7.4.6. Python packages:
FastAPI 0.162.4, Pydantic 2.14.3, SQLAlchemy 2.0.57, asyncpg 0.32.0,
Alembic 1.20.1, Taskiq 0.13.0, taskiq-redis 1.2.4, redis client 8.1.0,
httpx 0.28.1, Uvicorn 0.54.0, uvicorn-worker 0.4.0, pytest 8.4.2, Ruff 0.14.14.
Версии прочитаны из установленных пакетов; зависимости не обновлялись.

Compose project cascade-bootstrap-check: семь сервисов healthy, API на 18000.
Через compose top подтверждены два отдельных дочерних worker-процесса.
Оба существующих PostgreSQL volume сохранены. При чтении было 9 unknown и
12 in_flight; их не чистили и не меняли статусы. Порт 8000 занят чужим Python,
он не останавливался.

| Фактически выполнено | Результат |
|---|---|
| poetry check | exit 0 |
| poetry run ruff check . | exit 0 |
| poetry run ruff format --check . | exit 0, 47 файлов |
| docker compose config -q | exit 0 |
| poetry run alembic upgrade head, два раза | exit 0, revision 0001 |
| docker compose exec -T mock-a alembic -c mock_alembic.ini upgrade head, два раза | exit 0, mock_0003 |
| То же для mock-b | exit 0, mock_0003 |
| RUN_INTEGRATION=1 API_URL=http://127.0.0.1:18000 poetry run pytest -q | 123 passed, 0 skipped, 21.44s |
| docker compose run --rm -e RUN_INTEGRATION=1 -e API_URL=http://api:8000 api pytest -q | 123 passed, 0 skipped, 23.48s |
| docker compose --profile demo run --rm demo first-success | exit 0, A/B ledger 1/0, amounts 123/0 |
| … demo cascade-success | exit 0, ledger 0/1, amounts 0/123 |
| … demo all-declined | exit 0, failed, ledger 0/0 |
| … demo duplicate-delivery | exit 0, 10 POST, один payment/attempt, ledger 1/0, create_calls 1/0 |
| git diff --check после правки EOF новой записи | exit 0 |

Независимые воспроизведения выявили два дефекта, не покрытых этим baseline:
[KI-01/KI-02](known-issues.md). Настоящий локальный HTTP 200 с полным envelope
status=not_found/unknown был принят адаптером с external_id. В отдельной
откатываемой DB-транзакции created с истёкшим safe_retry_until получила in_flight
claim; HTTP в этом воспроизведении не вызывался. Обнаруживший агент по правилам
не исправлял дефекты, запросил передачу. Все проверки baseline прошли на прежнем
коде; добавлены только записи документации.

Новые fixtures/resolve, bounded reconciliation, её due-dispatch и D4 lost-response
ещё не реализованы и не проверены. Проверки финансовых исходов unknown, окна
повтора, бюджета/пятого владельца, restart/manual из нового объёма не выполнены.
Причина: зависимая работа ожидает передачи подтверждённых дефектов и решения по
настоящему disconnect. Runtime smoke со stop/restart сервисов в этом запуске не
повторяли; результаты прежних запусков выше остаются историческими.


## Исправления после независимой валидации — 2026-11-12

Пользователь поручил исправлять выявленные проблемы другими агентами. KI-01 и
KI-02 исправлены отдельными агентами; третий read-only reviewer выявил KI-03
на стыке нового outbox producer и publisher. Его исправил другой агент.
Root прочёл diff, независимо повторил HTTP-тесты и полный набор. Итоговый review
не выявил новых подтверждённых нарушений SOLID/DRY/YAGNI. После проверки KI-01–03
удалены из known-issues; правила документа сохранены.

| Выполненная команда/проверка | Результат |
|---|---|
| poetry run pytest -q tests/unit/test_http_provider.py | 26 passed, 0.50s |
| poetry check | exit 0 |
| poetry run ruff check . | exit 0 |
| poetry run ruff format --check . | exit 0, 50 файлов |
| git diff --check | exit 0 |
| RUN_INTEGRATION=1 API_URL=http://127.0.0.1:18000 poetry run pytest -q | 138 passed, 0 skipped, 20.91s |
| docker compose build | exit 0 |
| docker compose up -d --wait api worker dispatcher mock-a mock-b | exit 0, healthy |
| docker compose run --rm -e RUN_INTEGRATION=1 -e API_URL=http://api:8000 api pytest -q | 138 passed, 0 skipped, 23.68s |

Новые 15 регрессий: три HTTP envelope case, семь unit window case,
три DB/application case окна, два DB/transport-boundary case publisher.
Граница доказательства: HTTP-adapter тестируется настоящим локальным HTTP;
окно/атомарность/время после lock — реальным PostgreSQL и дополнительным
application test без provider вызова. Publisher тест использует PostgreSQL
временную таблицу и подмену публикации на границе транспорта. Финансовые
success/cascade/idempotency e2e через настоящий HTTP/Redis/Taskiq остаются
в полном наборе. Эти проверки не являются e2e автоматической сверки.

Миграции и зависимости не менялись. PostgreSQL/Redis не останавливались,
старые unknown/in_flight и provider ledger не чистились. Новые DB-тесты либо
откатывают свои записи, либо удаляют только собственные UUID fixture без
операций провайдера. .env/.gitignore/volumes и Git HEAD сохранены; commit,
amend, push и staging не выполнялись.

Предохранители готовы: недопустимый charge envelope не сохраняет external_id;
просроченная первая отправка сохраняет unknown/reconcile outbox без charge;
неподдерживаемый reconcile остаётся unpublished. Автоматическая сверка, D4,
новые mock modes/resolve и обязательные граничные проверки сверки пока не
реализованы. Способ настоящего disconnect остаётся открытым выбором;
готовность шага 6 и переход к шагу 7 не заявляются.


После обновления образа последовательно повторены CLI:

- docker compose --profile demo run --rm demo first-success — succeeded,
  ledger A/B 1/0, amounts 123/0.
- docker compose --profile demo run --rm demo cascade-success — succeeded,
  ledger 0/1, amounts 0/123.
- docker compose --profile demo run --rm demo all-declined — failed,
  две declined, ledger 0/0.
- docker compose --profile demo run --rm demo duplicate-delivery — succeeded,
  10 POST, одна attempt, ledger 1/0, create_calls 1/0.

Все четыре команды завершились успешно. Итоговый docker compose ps — семь
healthy сервисов; git diff --check — exit 0. Runtime/bootstrap со специальными
stop/restart failpoints в этой итерации не повторяли. Git HEAD по-прежнему
3157f5a; новые test-файлы остаются untracked для пользовательского коммита.


## Текущий этап: ограниченная сверка unknown — 2026-11-12

Проверено незакоммиченное рабочее состояние поверх HEAD3157f5a, отдельно от всех
исторических разделов выше. Зависимости/.env/.gitignore и старые volumes сохранены.
Актуальные payments revision0001, mock A/B mock_0004. Новая mock0004 применена
на каждой БД; команды migrate/migrate-mock-a/migrate-mock-b повторены успешно.
Сборка и up --wait api/worker/dispatcher/mock-a/mock-b завершились успешно.

| Команда/проверка | Наблюдаемый результат |
|---|---|
| poetry check | exit0 |
| poetry run ruff check . | exit0 |
| poetry run ruff format --check . | exit0, 61 файлов |
| git diff --check | exit0 |
| poetry run pytest -q tests/unit/test_http_provider.py tests/unit/test_lookup_provider.py | 41 passed, 0.55s |
| RUN_INTEGRATION=1 API_URL=http://127.0.0.1:18000 poetry run pytest -q tests/integration/test_unknown.py -x | 10 passed, 59.55s; до добавления отдельного Taskiq due/lease теста |
| RUN_INTEGRATION=1 API_URL=http://127.0.0.1:18000 poetry run pytest -q | 259 passed, 0 skipped, 86.63s; включая добавленный due/lease тест и KI04 |
| poetry run python scripts/check_reconciliation_runtime.py | exit0; unknown/manual, count5, nextNULL, lookup5/create1, ledger0/0; restart worker/dispatcher/A/B сохраняет DB snapshot; 8 resolved finals A/B canonical/ledger сохранены |

Финансовые e2e выполняют настоящий POST/GET/fixtures/resolve/stats по HTTP,
публикуют настоящие Taskiq задачи и наблюдают PostgreSQL/Redis/два worker.
D4 helper гарантирует промежуточный unknown/null external_id: lookup временно
недоступен до наблюдения unknown, затем разрешён. TCP proxy действительно
закрывает клиентское соединение после commit, без HTTP bytes; адаптер даёт
unknown, canonical lookup находит единственный ledger. B не вызывается.

Остальные e2e: pending resolve во все4 финала, malformed/5xx→unknown,
временный404 при существующем success→тот же charge key/ledger1,
timeout до operation с reached before barrier→lookup404→повтор старого key,
release→одна attempt/ledger1. Бюджет проверен пятью настоящими lookup без
ускорения production таймингов; ручной marker не оживает после restart.
Taskiq до due/при lease проверен отдельным тестом с управлением только timing
полями собственного payment; это не имитация финансового ответа или blind fix
производственных статусов. Исходные неизвестные платежи не чистились. Due unknown
теперь обрабатываются протоколом сверки/лимитов; expired in_flight не сканируются.

Дополнительные unit/DB/application тесты контролируют точные границы окна,
lease/deadline, время после ожидания row lock, foreign/stale/expired/manual result,
право пятого действующего владельца сохранить финал после deadline и atomic
lookup decline/B/outbox. Transport-boundary tests используют TEMP DB таблицы и
mock только публикации, отдельно от финансовых e2e. Provider HTTP tests проверяют
на A/B настоящий disconnect, malformed/503, resolve, точный persisted counter
при8 конкурентных lookup и canonical repeat после смены fixture на barrier.

Обнаруженный reviewer KI04 исправлен другим агентом и прошёл6 реальных HTTP
регрессий в полном наборе. После проверки удалён из known issues; актуальных
подтверждённых проблем нет. Новых подтверждённых SOLID/DRY/YAGNI нарушений
независимый review не нашёл. Лишних слоёв/registry/движков не добавлялось.

Не выполняются в этом этапе: общий recovery pending/created/expired in_flight,
Redis-loss recovery, D6, полная combined fault-matrix, нагрузка/реальные деньги,
финальный clean checkout. Это границы шагов7–9, не скрытые пройденные проверки.
Ни один skipped не объявлялся passed. Docker полный прогон и текущие CLI/runtime
результаты будут добавлены отдельным продолжением после завершения команд.


### Итог текущих e2e/CLI/runtime запусков

- Docker полный набор: `docker compose run --rm -e RUN_INTEGRATION=1
  -e API_URL=http://api:8000 api pytest -q` — **259 passed, 0 skipped, 88.33s**.
- Все пять `docker compose --profile demo run --rm demo <scenario>` выполнены
  последовательно с проверкой exit code: first-success, cascade-success,
  all-declined, duplicate-delivery, lost-response — exit0.
  Ledger1/0,0/1,0/0,1/0,1/0. D4 вывел intermediate_status=unknown,
  intermediate_external_id=null → succeeded, lookup1/create1, Bcalls0.
- `poetry run python scripts/check_idempotency_runtime.py` — exit0,
  queued before HTTP3, разные worker PID6/7, ledger0/1 и createcalls1/1,
  concurrent mock restart passed.
- Первый `check_first_payment_runtime.py` упал на старом assert404 после restore A:
  новая сверка уже успела корректно разрешить unknown. Это KI05 verifier,
  а не подтверждение финансового дефекта. Другой агент обновил ожидание на
  тот же payment/attempt/key → succeeded + independent ledger. Повтор
  `poetry run python scripts/check_first_payment_runtime.py` — exit0:
  observed unknown при недоступном A, затем тот же key/ledger1/789, B0;
  canonical finals после restart, production controls включая resolve404,
  проверенные JWT/секреты/ref отсутствуют в логах. KI05 удалён из known issues
  после успешного повтора; причины остались в документации/истории проверок.
- Подтверждены два независимых дочерних worker-процесса через compose top.
  Оба прежних volume сохранены, порт8000 чужого Python не трогали.

Эти runtime запуски выполнялись последовательно после pytest; остановок нужных
сервисов одновременно с тестами не было. После изменения только smoke verifier
259 тестов не прогоняли повторно без причины: платёжный код/схема не менялись,
сам исправленный smoke повторён реально. Образ после script change пересобран.


Финальная пересборка и `poetry run python scripts/check_bootstrap.py --api-port 18000`
прошли exit0: role isolation, live/ready, два HTTP worker; при stop Redis/PostgreSQL
live200/ready503; все application containers graceful exit0; restart сохранил
существующий volume и собственную sentinel row (после проверки удалена только она).
Запуск был после всех pytest/CLI/runtime, без конкурирующих тестов/остановок.
Новый source не менялся после259/259, пересборка включила исправленный smoke script.
