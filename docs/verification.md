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
