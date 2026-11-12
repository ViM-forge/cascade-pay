# Результаты проверок

Здесь описаны **фактически выполненные проверки**. Команды для повторения —
в [README](../README.md#проверки) и [инструкции обслуживания](operations.md).
Связь отдельных гарантий с тестами — в [матрице приёмки](acceptance.md).

## Краткий итог на 2026-11-12

| Проверка | Результат |
|---|---|
| Полный pytest в изолированной копии | 299 passed, без пропусков, локально и в Docker |
| Пять коротких демонстраций D1–D5 | Успех; проверены состояния, вызовы и независимые журналы списаний |
| Аварийная матрица D6 | Успех, 11 cases с падениями процессов и восстановлением |
| Дополнительные runtime-проверки | Идемпотентность, сверка, restart, health и сохранность данных прошли |
| Poetry, Ruff, миграции | Успех; повтор миграций не изменил данные |
| Повторный обзор после подготовки передачи | 299 passed за 89,23с; D1–D5 успешны; семь сервисов healthy |

Для проверки передачи использована отдельная копия файлов кандидата сдачи:
новые секреты, venv и PostgreSQL volume. Занятые стандартные порты заменены
временным override. Это **не clone будущего коммита**; cache образов и пакетов
использовался. Точные команды и результаты сохранены ниже.

Повторный обзор выполнялся в рабочем Compose-окружении с API 18000:
`RUN_INTEGRATION=1 API_URL=http://127.0.0.1:18000 .venv/bin/pytest -q` —
299 passed, без пропусков, 89,23с. Затем последовательно выполнены Docker CLI
D1–D5: success A, каскад A → B, оба отказа, lost-response и duplicate-delivery;
все exit0. Ruff lint/format и poetry check прошли. Новый D6 и запуск в чистой
копии в этом повторном обзоре не выполнялись. Это результаты предыдущего
обзора, а не новые запуски при редактировании документации.

## Что подтверждено и где границы

Проверки покрывают конкурентные POST, дубли Taskiq, потерянный ответ после
списания, lookup без external_id, lease/version, лимиты сверки, recovery после
потери Redis, JWT и изоляцию merchant. Факт списания сверяется с ledger mock,
а не только с локальным статусом payment. SQL-fixture count4 в двух crash cases
не выдаётся за четыре реальных lookup; пять реальных lookup проверены отдельно.

Проверено на macOS arm64 с Linux-контейнерами; Python хоста 3.13.13,
контейнера 3.13.9. **Windows/WSL, нагрузка/SLA, реальные провайдеры и деньги,
потеря PostgreSQL volume не проверялись.** Гарантии зависят от provider-контракта.
Результаты тестов не означают exactly-once доставку или отсутствие любых дефектов.

## Подготовка передачи: чистая копия — 2026-11-12

Проверка начата из чистого рабочего дерева HEAD `4dbc67c`. Экспортированы только
отслеживаемые файлы с текущими изменениями README и новым `scripts/demo-recovery.sh`,
подготовленным к добавлению пользователем; Git-коммит не создавался.
В копии нет исходных .env, .venv и внутренних документов автора. Созданы новые
случайные секреты (0600), новый venv и пустой PostgreSQL volume
`cascade-delivery-check_postgres-data`, отдельный project `cascade-delivery-check`
и образ `cascade-payments:delivery-check`. Рабочий `cascade-bootstrap-check`
не останавливался, его данные не очищались.

Из-за занятых рабочих портов использован временный override только host-портов
и имени образа: PostgreSQL 25432, Redis 26379, API 28000, mock 28001/28002.
Host URL нового .env согласованы с ними; COMPOSE_FILE указывает на compose.yaml
и временный override с !override для ports. Версия Compose 5.2.0.
Обычный запуск на стандартных портах одновременно с рабочим проектом не выполнялся.
Последовательность README сохранена; подмена портов — явное ограничение проверки.

| Фактическая команда / проверка | Результат |
|---|---|
| `python3.13 scripts/configure_local.py` | новый .env, mode0600 |
| `POETRY_VIRTUALENVS_IN_PROJECT=true poetry env use python3.13`, `poetry sync --with dev` | новый venv, установка lock успешна |
| `docker compose config -q`, `docker compose build` | exit0 |
| `docker compose up -d --wait postgres redis` | новый volume, healthy |
| `docker compose run --rm migrate`, `migrate-mock-a`, `migrate-mock-b` | все exit0 |
| `docker compose up -d --wait api worker dispatcher mock-a mock-b` | семь сервисов healthy |
| curl live/ready API и обоих mock на портах копии | все HTTP200 |
| `poetry check`, `poetry run ruff check .`, `poetry run ruff format --check .` | exit0; 68 файлов |
| `RUN_INTEGRATION=1 API_URL=http://127.0.0.1:28000 poetry run pytest -q` | **299 passed, 0 skipped, 93.60с** |
| `docker compose --profile demo run --rm demo first-success` | succeeded, A/B ledger1/0, сумма123 |
| `... demo cascade-success` | A declined/B succeeded, ledger0/1 |
| `... demo all-declined` | failed, ledger0/0 |
| `... demo lost-response` | наблюдаемый unknown/null → succeeded, create1/0, lookup1, ledger1/0 |
| `... demo duplicate-delivery` | 10 POST, один UUID, настоящий Taskiq noop ACK при удержанном HTTP, create1/0, ledger1/0 |
| `API_URL=http://127.0.0.1:28000 sh scripts/demo-recovery.sh` | exit0, все 11 аварийных cases |

D6 подтвердил commit_before_publish, publish_before_mark, published_redis_loss,
claim_before_http, claim_before_ack_reclaim, charge_before_result, late_owner,
commit_before_ack_reclaim, combined_failures, fifth_owner_success_after_deadline
и fifth_claim_manual_restart. Исходные payment/attempt/key/window сохранялись;
SQL-fixture count4 в двух cases не считается четырьмя реальными lookup.

При проверке инструкции обнаружен и передан другому агенту конфликт
стандартного API8000 с default host pytest18000. Исправлены две команды README:
они явно выбирают 8000 и сохраняют экспортированный API_URL. Подстановки
8000/28000 проверены отдельно. Финансовый код, конфигурация и lock не менялись.

Дополнительно последовательно, после D6:

- `API_URL=http://127.0.0.1:28000 poetry run python scripts/check_idempotency_runtime.py`: exit0; queued3, разные PID, конкурентный дубль B, create1/1, ledger0/1.
- `API_URL=http://127.0.0.1:28000 poetry run python scripts/check_first_payment_runtime.py`: exit0; устойчивые финалы после restart, прежний key, ledger1, production controls404, safe_logs passed.
- `API_URL=http://127.0.0.1:28000 poetry run python scripts/check_reconciliation_runtime.py`: exit0; пять настоящих lookup, manual с next=NULL после restart и дублей, без нового HTTP; восемь canonical финалов A/B.
- `python3.13 /tmp/cascade-delivery-lifecycle.py` в копии: exit0. Вспомогательная проверка через Compose/HTTP/psql подтвердила Gunicorn master + 2 worker, отсутствие dispatcher в API, запрет межбазового CONNECT; при stop Redis/PostgreSQL live200/ready503 и восстановление. Все пять application-сервисов при stop получили ExitCode0 без OOM. После stop/start самой БД совпали fingerprints терминальных payments и обоих ledger. Повторены все три migration-команды: payments revision0002, mock revision mock_0004; данные неизменны. Семь сервисов снова healthy. Это эквивалентная проверка lifecycle с портами копии; оригинальный check_bootstrap.py с hardcoded mock8001/8002 отдельно не запускался.

Новые подтверждённые нарушения SOLID/DRY/YAGNI не выявлены при просмотре domain/application imports, портов, orchestration use cases, repository, scheduling и dispatcher. Правила каскада общие для finish/recovery, начальные A/B создаёт один helper, SQL-готовность сверки общая; доменная перепроверка после locks сохраняется. Это ограниченное ревью конкретных компонентов, не доказательство отсутствия всех дефектов.

Итоговые проверки после runtime:

| Команда / действие | Результат |
|---|---|
| `docker compose run --rm -e RUN_INTEGRATION=1 -e API_URL=http://api:8000 api pytest -q` | **299 passed, 0 skipped, 87.92с**, exit0 |
| `python3.13 /tmp/cascade-delivery-final.py` | пример POST/GET из README через sh -e: exit0; недоступный API в CLI: nonzero; invalid scenario: exit2; повтор генератора .env: nonzero, содержимое и mode0600 сохранены |
| просмотр и скриптовая проверка всех application-логов | созданных signing/service/DB credentials и JWT нет; first-payment runtime также проверил ref |
| финальные Poetry check, Ruff lint/format в копии | exit0, 68 файлов |
| локальные Markdown-ссылки, список tracked файлов, private-key/JWT scan | отсутствующих файлов и кандидатов не найдено; .env/venv/логи/внутренние материалы не отслеживаются |
| `sh -n scripts/demo-recovery.sh`, `git diff --check` | exit0 |
| `docker compose -p cascade-delivery-check down -v --remove-orphans` в копии | exit0, удалён только тестовый project и его новый volume |
| `docker compose ps` рабочего проекта и `docker volume ls` | семь исходных сервисов healthy; исходный PostgreSQL volume сохранён, тестовый удалён |

JWT positive/negative, merchant isolation, lease/fencing, timeout/not_found без B,
поздние результаты, manual и test-only controls проверены полными unit/integration/e2e
и runtime, указанными выше; связь гарантий с тестами — [acceptance](acceptance.md).
Невыполненных обязательных проверок в пределах MVP нет. Новых открытых проблем
не осталось. Новый wrapper необходимо включить в пользовательский коммит вместе
с README и этим отчётом. Staging, commit, публикация и отправка не выполнялись.

Граница доказанного: запуск выполнен из экспорта файлов кандидата сдачи,
со свежими секретами/venv/volume и указанной заменой занятых портов; это не clone
будущего пользовательского коммита. Dependency/container cache использован;
скачивание всех пакетов с нулевым сетевым cache не проверялось. Host Python 3.13.13,
Docker Python 3.13.9. Unix runtime; Windows, нагрузка/SLA, потеря PostgreSQL volume,
реальные деньги/провайдеры не проверялись. Mock-контракт необходим для гарантий ledger.

## История проверок по этапам

Ранние отчёты описывают состояние на дату соответствующего этапа. Формулировки
«не реализовано» или «не проверено» внутри истории не отменяют более поздние
результаты выше. История сохранена для прослеживаемости ошибок и повторных проверок.

<details>
<summary>Развернуть хронологию разработки и промежуточных проверок</summary>

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


## Восстановление из PostgreSQL — 2026-11-12

Фактически выполнено:

```sh
poetry run alembic upgrade head
poetry run alembic current
poetry run alembic upgrade head
API_URL=http://127.0.0.1:18000 poetry run python scripts/demo.py restart-recovery
```

Миграция0002 применена; повторный upgrade успешен. D6 завершён exit0,
10 сценариев. Worker/dispatcher — настоящие локальные процессы Taskiq/Python,
остановлены SIGKILL и запущены заново; PostgreSQL/Redis/API/mock — Compose.
PostgreSQL volume сохранён. Redis очищен и пересоздан. API/worker/dispatcher/mock
перезапущены; mock также перезапускались независимо, при работающем API.

| Сбой / сценарий | payment_id | attempt_id = provider key | ledger A/B |
|---|---|---|---|
| commit_before_publish | 47e582a4-f59e-49ec-b169-cacd4d713792 | 328c9409-3561-4753-a3a9-c1df6f740d10 | 1/0 |
| publish_before_mark | 5b5a3fc0-9570-4be0-8e2a-ded4a6edb6ef | ce5eb709-21f4-48b0-be0c-002ea75011f6 | 1/0 |
| published_redis_loss | 14b91c03-edb9-4c95-9a78-b0e5d1862dad | 1b7db165-6e5d-4bbf-9e7d-4713e1a1b89b | 1/0 |
| claim_before_http | 26dd1bba-d7e1-4ef5-9922-c62a614ccdfd | 2181c943-af9f-4fe2-9504-3ed5e9095a0c | 1/0 |
| claim_before_ack_reclaim | af461cf1-deff-44ed-8cf0-52dc31bdee48 | cb7cccfb-170b-4859-a3ee-fb8e6ca9ef74 | 1/0 |
| charge_before_result | 0300476e-1e43-4be5-a0a0-87b2b8490983 | f8b6f1e8-9dac-4930-8733-088730213802 | 1/0 |
| late_owner | ba646b6c-4abf-4fa4-8ee8-5bc72510cca3 | acfeaa03-cb95-4069-9189-de61be23334d | 1/0 |
| commit_before_ack_reclaim | b9bb357a-3c6e-4165-90d2-f76a1f6dbf4d | 352b1e18-72a5-4818-bee1-75616de09e29 | 1/0 |
| restart_active_http | b3fdef06-7ea9-4024-8cf7-e620ad65d437 | 7188bb1a-48cb-4b81-9993-14afd86330a2 | 1/0 |
| fifth_claim_manual_restart | 9e2a4c41-35e6-4c32-99b2-f308143d0c63 | 4b1bf519-f3a8-44e6-8cba-c48252296f4a | 0/0 |

Во всех9 success: A create_calls=1/ledger_amount=123, B create/lookup=0;
сохранены payment_id, attempt_id/key и safe_retry_until. Для начального pending
до появления попытки сравнивается исходный payment_id, а созданная попытка затем
проверяется по GET и БД. Created с уже published событием и сохранение остальных
полей отдельно проверены в DB-матрице.

Активный lease не изменялся при скане. Для позднего владельца новый lookup
сохранил канонический финал, после release прежнего ответа snapshot не изменился.
Два reclaim-сценария запускали новый worker без dispatcher/recovery и новых
сообщений: PEL исходного stream ID очистился, XRANGE остался побайтно прежним.
Для незавершённого in_flight после transport reclaim включался DB recovery.

Для пятого claim count4 задан SQL только собственному тестовому платежу;
настоящий claim5 закоммичен, worker убит, expiry дал version+1/manual/leaseNULL/
nextNULL. Lookup_calls=0 для этой fixture: claim5 умер перед HTTP, шестого нет.
После flush/restart manual snapshot и stats неизменны, дубли process/reconcile
не вызвали HTTP. Полные5 реальных lookup покрывает существующая e2e pytest.

Промежуточные прогоны не считаются успешными: первый controller потерял Redis
connection после recreate; новый активный HTTP verifier получил422 на release
без JSON. После исправлений pool disconnect и JSON {} приведённый итоговый D6
прошёл полностью. Ошибки отражены здесь, а не подменены успешными статусами.

Ограничения: проверки используют договорённый mock key/lookup contract;
потеря PostgreSQL данных, нагрузка, реальные деньги, exactly-once и SLA не доказаны.
D6 host-only и меняет состояние Redis данного Compose проекта; не запускать
параллельно с pytest/другими runtime-скриптами. Все PostgreSQL volumes сохранены.


Итоговые проверки шага:

| Проверка | Фактический результат |
|---|---|
| RUN_INTEGRATION=1 poetry run pytest -q | 276 passed, 88,30с, без skipped |
| docker compose run --rm -e RUN_INTEGRATION=1 -e API_URL=http://api:8000 api pytest -q | 276 passed, 89,89с, без skipped |
| poetry run ruff check . | passed |
| poetry run ruff format --check . | 66 files already formatted |
| poetry check; git diff --check | passed |
| docker compose build; up --force-recreate --wait api worker dispatcher | passed, healthy |
| alembic current; upgrade head с повтором | 0002 head, passed |

CLI D1–D5 запускались последовательно после pytest, на host через
API_URL=http://127.0.0.1:18000 poetry run python scripts/demo.py <scenario>.
Все5 завершились exit0:

| Демо | payment_id | Результат / ledger A,B |
|---|---|---|
| first-success | aeb73eec-bd26-4958-9641-0bdde945df1d | succeeded / 1,0 |
| cascade-success | c599f179-9fbd-4fdf-bdb5-2aa68538a341 | succeeded / 0,1 |
| all-declined | 6ea678e1-98d8-42ba-bd90-c6b9cf8c9b17 | failed / 0,0 |
| lost-response | c0c5191a-7ac6-414f-ab02-8ba0e47b5a4e | unknown/null → succeeded / 1,0, lookup1 |
| duplicate-delivery | 4e55e8b0-50c2-47c0-8907-345a253a9193 | 10 concurrent POST, succeeded / 1,0 |

Все шесть основных демо подтверждены, включая D6 выше. Технические и локальные
журналы обновлены. Новых нерешённых known issues не обнаружено. Проверки не
подменяют будущий clean checkout и остальные проверки финальной передачи.

## Шаг 8: совместные границы сбоев (2026-11-12)

Проверяется рабочее дерево поверх пользовательского HEAD `f3e0984`
(`feat: добавить восстановление обработки платежей из PostgreSQL`). Исходный
`git diff` пуст; новые изменения шага незакоммичены, Git-коммит агент не создавал.
Матрица D1–D6/Q1–Q6/G1–G22 с конкретными test IDs:
[acceptance.md](acceptance.md). Результаты предыдущего шага выше — исторические;
ниже только самостоятельные запуски текущего этапа.

Окружение: macOS, Python 3.13.13, pytest 8.4.2, Ruff 0.14.14,
SQLAlchemy 2.0.57, asyncpg 0.32.0, httpx 0.28.1, Taskiq 0.13.0,
taskiq-redis 1.2.4, redis-py 8.1.0; Compose PostgreSQL 17.6 / Redis 7.4.6,
API на 127.0.0.1:18000, два независимых mock HTTP/БД. Секреты берутся из `.env`.
Версии не обновлялись.

### Промежуточные проверки и ошибки

- Исходный `RUN_INTEGRATION=1 .venv/bin/pytest -q`: **276 passed**, 87.18с,
  без skipped. Это самостоятельная проверка исходного состояния.
- Focused новый DB fencing и domain: **25 passed**, 0.34с (до добавления
  дополнительного fifth-expired DB кейса). Не итоговый полный набор.
- Ruff сначала нашёл длинную строку в новом DB-тесте; formatter исправил,
  повтор lint успешен.
- Первый расширенный runtime: 8 завершённых cases, затем AssertionError нового
  combined. Старт второго consumer/ожидание ACK заняли исходный HTTP timeout;
  snapshot уже изменился в unknown. Cleanup восстановил healthy-сервисы.
- Второй runtime: 8 cases, затем AssertionError: готовность consumer сама по
  себе не запрещает сохранение timeout во время ожидания дубликатов.
- Третий runtime: 8 cases, затем timeout ожидания ACK. Пока worker удержан,
  его reader может заранее получить дубли и оставить их во внутренней очереди.
  Это ошибка построения нового сценария, не дефект финансовой обработки.
- Контроллер теперь использует before_result_commit barrier также для timeout,
  SIGSTOP исходной группы перед публикацией дублей (останавливается и reader),
  затем SIGKILL и restart. Ожидание первого ACK вынесено из D5 в общий helper;
  ожидание старого ответа после release — по ACK, без фиксированного sleep.
  Во всех неуспешных прогонах PostgreSQL volume сохранён, сервисы восстановлены.
- При чтении mock Resolve DTO до исполнения success5 исправлено обязательное
  поле `error_code: null`; падения на этом запросе не было.

Итоговый последовательный прогон выполняется; результаты будут приведены ниже.

Четвёртый runtime: 8 cases, затем AssertionError **после scan** combined:
контроллер требовал полностью неизменный snapshot, хотя lease к тому времени
законно истёк и recovery уже перевёл attempt в unknown. Убран лишний readiness
цикл до HTTP; SIGSTOP исключает prefetch исходным reader. После scan проверяются
неизменные id/key/window/count, а смена статуса/version при expiry разрешена.
Отдельные существующие live-lease проверки по-прежнему требуют полного no-op.
Пятый последовательный прогон запущен. Для позднего ответа дополнительно ожидается
checkpoint after_result_commit, а не просто пауза после release.

### Финальная аварийная матрица и runtime

Пятый полный аварийный прогон: **11 cases, exit 0**. Команда:
`API_URL=http://127.0.0.1:18000 .venv/bin/python scripts/check_recovery_runtime.py`.
Ниже payment / исходный attempt (= provider key для успешных cases), ledger A/B:

| Case | Payment | Attempt / provider key | Ledger |
|---|---|---|---|
| commit_before_publish | ec6ebc4e-0a6c-44f7-b801-82088dc9f4e7 | b1d87267-a4f2-46ba-bad9-d7da54085d44 | [1, 0] |
| publish_before_mark | 729cb99b-fdaa-4f10-aa6e-7aeb3ce17913 | 6ab9e554-92d8-4284-abc9-2cbaf892426b | [1, 0] |
| published_redis_loss | 6c275c15-a166-4d2d-83e2-6c929e0f80ff | 14742c3e-5da2-4041-a2d3-38c36548a101 | [1, 0] |
| claim_before_http | b3a8f1db-a4f0-4064-ae9a-fe0d63dc20ed | 90322a3d-eecf-474a-af07-c55248bab378 | [1, 0] |
| claim_before_ack_reclaim | d31b635d-becb-4d09-bc68-75fe5b31c6c1 | 6ead1ffa-e1e1-4ebd-aa4c-5d3fb28370c7 | [1, 0] |
| charge_before_result | 5766bc3e-3206-4b58-9aa5-c6c807f7fa17 | 4f835fe8-9668-4459-acd7-c10ebc3c01b9 | [1, 0] |
| late_owner | 6b0abea5-2399-4f66-bfd9-321d95c8c2fb | 1d540b24-f2e7-4f60-8970-3c50e28e5efa | [1, 0] |
| commit_before_ack_reclaim | 91f44077-ebcc-4017-8e48-24a2b56071d4 | 5c4da98b-cd5b-46f2-8920-1060631ce9eb | [1, 0] |
| combined_failures | 37f54043-1a25-465b-81b6-18dc4fe917b7 | 23d10c9e-cd55-41f9-b885-d37da1c454cf | [1, 0] |
| fifth_owner_success_after_deadline | bd75d330-b484-479e-8176-0a8e5b98c4d8 | 5cae2c6d-edbb-4a0d-a219-066cc407fd54 | [1, 0] |
| fifth_claim_manual_restart | 6070e315-2744-4c1c-8ffd-956a90f614e0 | 59f25c30-b4aa-435b-8e5c-49ff23db32e9 | [0, 0] |

В `combined_failures`: 10 конкурентных клиентских POST, три реальных Taskiq
дубля, наблюдение ACK при удерживаемом результате, SIGSTOP/SIGKILL исходной
группы, restart с сохранёнными данными, canonical lookup. Проверены одна attempt,
исходные payment/attempt/key/safe_retry_until, ledger amount=123, совпадение
external_id с независимой operation, B create/lookup=0; create A=1, lookup A=1,
check_count=1. Прежний safe_retry_until: `2026-11-13T12:06:54.957071+00:00`.

В `fifth_owner_success_after_deadline` count4 и прошедший deadline — SQL fixtures.
Пятый claim настоящий, provider pending разрешён один раз в success через HTTP,
пятый live owner получает один настоящий lookup и сохраняет финал после deadline
до expiry. Ledger1, check_count5, window/deadline сохранены. Это не пять HTTP.
В `fifth_claim_manual_restart` count4 тоже fixture, lookup=0; настоящий claim5
commit, SIGKILL до HTTP, expiry/manual, marker/count/deadline/window переживают
Redis/process/mock restart. Пять настоящих lookup отдельно проверены ниже.

Последовательно после D6 успешно завершились (exit0):

- `.venv/bin/python scripts/check_idempotency_runtime.py`: queued3 до HTTP,
  разные worker PID, конкурентный дубль B; create1/1, ledger0/1.
- `.venv/bin/python scripts/check_first_payment_runtime.py`: canonical finals
  после restart, recovery старой attempt/key, ledger1, production controls404,
  безопасные логи.
- `.venv/bin/python scripts/check_reconciliation_runtime.py`: пять реальных
  lookup + create1, manual после restart и дублей без нового HTTP, ledger0/0,
  восемь immutable операций обоих mock после restart.
- `.venv/bin/python scripts/demo.py <scenario>` для first-success,
  cascade-success, all-declined, lost-response, duplicate-delivery: D1–D5 exit0.

Runtime не запускались одновременно с pytest или друг с другом. PostgreSQL
volume не удалялся. Рабочие Compose сервисы восстановлены также после неуспехов.

### Итоговый локальный набор

После всех изменений кода, последовательно после runtime/демо:

```sh
RUN_INTEGRATION=1 API_URL=http://127.0.0.1:18000 .venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
docker compose build
```

**286 passed за 88.42с, без skipped**; Ruff lint и format exit0,
67 файлов отформатированы; Compose build exit0. Новые 10 проверок:
7 доменных вариантов запрета позднего результата после success,
2 DB canonical success/terminal-decline после fencing,
1 DB expired пятый owner с прежней version до recovery и последующим manual.
Production-код и зависимости не менялись.

### Итоговый Docker и состояние передачи

`docker compose run --rm -e RUN_INTEGRATION=1 -e API_URL=http://api:8000 api pytest -q`:
**286 passed in 88.61s (0:01:28)**, без skipped, exit0. Проверены окончательные изменения, включая
общий ACK helper в CLI D5 и комбинированной матрице. `git diff --check` успешен;
указанные test IDs и локальные ссылки текущих журналов существуют.

Все критерии этого этапа выполнены. Новых known issues не обнаружено. Изменения
остаются незакоммиченными поверх f3e0984; коммиты создаёт пользователь.
Ограничения: гарантии mock при сохранности PostgreSQL, host-only Unix runtime;
SQL fixtures count4/deadline не являются HTTP-вызовами. Нагрузка/SLA, реальные
деньги, потеря PostgreSQL volume, Windows runtime и финальный clean checkout
не проверялись. Exactly-once транспорта не заявляется.


## Ревью SOLID, DRY, YAGNI и удаление прежнего scheduler — 2026-11-12

Удалена `dispatcher.publish_due`: после перехода на DB recovery рабочий цикл
вызывает `recover_batch` и `publish_one`, прежнюю функцию вызывали только тесты.
Проверки due/lease/manual и повтора после сбоя перенесены на `publish_one` с
настоящим PostgreSQL и подменой только границы публикации Taskiq. Удалён один
тест batch старого пути; batch=20 действующего recovery проверяет
`test_recovery.py::test_batch_rotates_and_does_not_grow_unpublished_outbox`.
Рабочий протокол отправки и финансовые переходы не изменены.

| Выполненная команда | Результат |
|---|---|
| `poetry check` | exit 0 |
| `poetry run ruff check .` | exit 0 |
| `poetry run ruff format --check .` | exit 0, 67 файлов |
| `RUN_INTEGRATION=1 API_URL=http://127.0.0.1:18000 poetry run pytest -q tests/integration/test_reconciliation_transport.py tests/integration/test_dispatcher_event_types.py tests/integration/test_recovery.py` | 20 passed, 1.27с |
| `RUN_INTEGRATION=1 API_URL=http://127.0.0.1:18000 poetry run pytest -q` | 285 passed, 0 skipped, 88.36с |
| `git diff --check` | exit 0 |

Подтверждённые DRY-долги KI-05/KI-06 записаны в [known-issues](known-issues.md):
два SQL-предиката готовности unknown и две инициализации попытки.
Их рефакторинг не выполнялся. Подтверждённых нарушений SOLID, требующих изменения
архитектуры согласованного MVP, не выявлено; это результат ревью, а не гарантия
отсутствия всех дефектов. YAGNI: удалён заменённый scheduler без добавления слоёв.

Тесты выполнялись локальным кодом на работающем Compose-окружении.
Docker-образ в этом этапе не пересобирался, отдельный crash-runtime не запускался,
чистый checkout и финальная сдача повторно не проверялись.


## Исправление KI-05/KI-06 — 2026-11-12

Независимая проверка подтвердила оба дублирования в текущем коде; расхождений
условий и полей не было. SQL-готовность unknown теперь определена в
`infrastructure/scheduling.py`, начальные поля A/B — в приватном конструкторе
репозитория. Блокировки, транзакции, доменные перепроверки, ключи и окна повторов
сохранены. Правила сопровождения — в [decisions.md](decisions.md#сопровождение-sql-отбора-и-создания-попыток).
Обе записи удалены из known-issues после проверки; новых проблем не обнаружено.

Использован существующий Compose project `cascade-bootstrap-check`, API 18000.
Исходные незакоммиченные изменения прежнего ревью сохранены. PostgreSQL/Redis
volumes не удалялись; конфигурация, зависимости и lock не менялись.

| Реально выполненная команда | Результат |
|---|---|
| `RUN_INTEGRATION=1 API_URL=http://127.0.0.1:18000 poetry run pytest -q tests/integration/test_recovery.py tests/integration/test_reconciliation_transport.py tests/integration/test_initial_window_db.py tests/integration/test_reconciliation_db.py` | 49 passed, 2.18с; до добавления пяти дополнительных проверок точных SQL-границ |
| `docker compose build api` | exit 0, общий образ с изменённым production-кодом собран |
| `docker compose up -d --wait api worker dispatcher mock-a mock-b` | exit 0, все семь сервисов healthy |
| `RUN_INTEGRATION=1 API_URL=http://127.0.0.1:18000 poetry run pytest -q` | **299 passed, 0 skipped, 88.75с**; все 14 новых регрессий включены |
| `poetry check` | exit 0 |
| `poetry run ruff check .` | exit 0 |
| `poetry run ruff format --check .` | exit 0, 68 файлов |
| `git diff --check` | exit 0 |

Полный pytest выполнен локально против пересобранных application-сервисов:
DB integration и HTTP/Taskiq e2e запускались, а не пропускались. Новые проверки
recovery изолированы временными таблицами; SQL-границы проверены с фиксированным
временем БД и лимитом 3. Проверка A → B подтверждает flush терминальной A,
начальные поля обеих попыток и сохранение исходного key/window при prepare/finish.
Отдельный pytest внутри Docker, crash-runtime, чистый checkout, нагрузка и
реальные провайдеры/деньги в этом этапе не проверялись. Exactly-once транспорта
не заявляется. Коммиты агент не создавал.

### Независимая повторная валидация перед коммитом — 2026-11-12

Прочитан весь текущий diff и новая `scheduling.py`, сопоставлены старые/новые
SQL-условия и конструкторы A/B. Замена сохраняет приоритет live lease/manual,
выбор exhausted до due, явные часы и лимит; доменный claim остаётся после locks.
Создание UUID/key/window и порядок terminal-A flush → insert-B не изменены.
Рефакторинг не добавляет новые зависимости и не меняет направление к domain/application.
Удалённый publish_due не имел вызовов в рабочем цикле; действующий recovery batch
и publisher покрыты текущими тестами. Подтверждённых новых дефектов и архитектурных
развилок не выявлено. Production-код при этой повторной проверке не редактировался.

Проверено незакоммиченное рабочее дерево поверх HEAD `c2aa251`, включая новый
неотслеживаемый `src/payments/infrastructure/scheduling.py`. Этот файл должен войти
в коммит, поскольку publisher/repository уже импортируют его.

| Самостоятельно выполненная проверка | Результат |
|---|---|
| `RUN_INTEGRATION=1 API_URL=http://127.0.0.1:18000 .venv/bin/pytest -q` | 299 passed in 88.57s (0:01:28); без skipped |
| `.venv/bin/ruff check .` | exit0 |
| `.venv/bin/ruff format --check .` | exit0, 68 файлов |
| `poetry check` | exit0 |
| `git diff --check` | exit0 |
| `API_URL=http://127.0.0.1:18000 .venv/bin/python scripts/check_recovery_runtime.py` | exit0, 11 аварийных cases |
| `API_URL=http://127.0.0.1:18000 .venv/bin/python scripts/check_reconciliation_runtime.py` | exit0; 5 настоящих lookup, manual после restart, без шестого HTTP; 8 canonical finals A/B |
| `docker compose build api` | exit0 |
| `docker compose run --rm -e RUN_INTEGRATION=1 -e API_URL=http://api:8000 api pytest -q` | 299 passed in 91.05s (0:01:31); без skipped |

Аварии процессов, Redis-loss, recovery и полный pytest запускались строго
последовательно. Сверка SHA256 файлов repository.py, scheduling.py, dispatcher.py
в работающих api/worker/dispatcher подтвердила совпадение с рабочим деревом.
Первую попытку этой вспомогательной сверки преждевременно запустили во время
аварийного runtime: api совпал, exec в остановленный worker вернул ненулевой код.
После восстановления сервисов вся сверка повторена успешно. Это ошибка порядка
вспомогательной проверки, не отказ платёжного сценария. Основные pytest/runtime
в этой повторной валидации прошли с первого запуска.

Блокирующих замечаний к коммиту не обнаружено. Пользовательские изменения
сохранены; Git-коммиты и staging агент не выполнял. PostgreSQL volume сохранён,
сервисы восстановлены healthy. Сведения прежних проверок выше остаются историей.
Чистый checkout/финальная передача, нагрузка и реальные провайдеры этим повтором
не проверялись; гарантия ledger зависит от mock-контракта. SQL count4/deadline
в аварийных граничных fixtures не выдаются за фактические HTTP lookup.


## Аудит документации и докстрингов — 2026-11-12

Самостоятельный аудит по запросу пользователя; финальная передача не выполнялась.
Сверены текущие контракты/README/матрица, исходники платежей и mock, Compose,
Dockerfile, миграции и связанные тесты. Исправлены описание удалённого scheduler,
xread_count, due при expiry, manual marker, health и host API_URL, поведение
таймаута mock-barrier. Дополнены docstrings о commit/claim/HTTP/fencing.
Исторические результаты выше сохранены и не считаются запусками этого аудита.

| Проверка этого аудита | Наблюдаемый результат |
|---|---|
| `poetry run pytest -q` без RUN_INTEGRATION | 175 passed, 124 skipped, 0.98с |
| `poetry run ruff check .` | Успех |
| `poetry run ruff format --check .` | 68 файлов форматированы |
| `poetry check` | Успех |
| `git diff --check` | Успех |
| Сравнение AST изменённых Python-файлов с HEAD после удаления docstrings | Исполняемая структура не изменилась |
| Скриптовая проверка локальных Markdown-ссылок README, docs и plan | Нет отсутствующих файлов |
| Сверка test IDs acceptance с AST тестов, включая сокращённые ссылки | Все указанные функции существуют |

Существование файла/теста не подтверждает успешное исполнение integration.
Integration, Docker build/runtime, crash matrix, clean checkout и нагрузка
в этом аудите не запускались. Код поведения, конфигурация и зависимости не
менялись. Staging и Git-коммиты не выполнялись.

</details>
