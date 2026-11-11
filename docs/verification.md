# Проверка окружения — 2026-11-11

Результат относится только к bootstrap. Платёжные сценарии D1–D6 и гарантии
из acceptance.md пока не реализованы и не объявляются пройденными.

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
