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
