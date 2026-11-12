# HTTP-контракт mock-провайдеров A и B

Статус: нормативная спецификация. Реализованы success/lookup, долговечная идемпотентность, fixtures success/declined_retryable/declined_terminal/not_charged_final и stats. Реализованы два barrier, описанные ниже. Все перечисленные ниже fixture modes и защищённый resolve реализованы. Фактические проверки — в [verification.md](verification.md). Локальные переходы описаны в [decisions.md](decisions.md), проверки — в [acceptance.md](acceptance.md).

## Изоляция и долговечность

Два процесса из одного образа с разными provider names/base URL/credentials и отдельными БД mock_a/mock_b, ролями и миграциями PostgreSQL. Допустим один сервер PostgreSQL с устойчивым volume. Приложение не читает provider DB, провайдер не читает payments DB. Истина о списании — provider ledger через защищённый test-only HTTP stats.

Namespace ключа — конкретный provider; ключ — UUID попытки, сохранённый до HTTP, в каноническом lowercase UUID-формате с дефисами. Все операции аутентифицируются `Authorization: Bearer <service credential>` из env; это отдельный credential, не merchant JWT. Отсутствующий/неверный — 401 `{"error":{"code":"unauthorized"}}`, без изменения операции. Все ответы JSON UTF-8, кроме намеренного разрыва test-fixture. External ID — непрозрачный UUID, выдаваемый provider, не нужен для поиска.

Ключи, операции, отказы и ledger живут весь срок provider данных, без TTL/eviction. Очищать provider данные отдельно от payment данных запрещено. Финальные записи неизменяемы, включая отказ без списания. Pending может перейти только в один канонический финал. На один key максимум одна операция и одна ledger-запись; отказ исключает любое будущее списание по этому key, включая уже задержанный конкурентный запрос.

## Создание операции

`POST /charges`, тело содержит ровно:

```json
{"key":"<UUID попытки>","amount_minor":100,"currency":"USD","method_ref":"demo-ref"}
```

Key — UUID; amount_minor и method_ref имеют те же пределы и строгие типы, что публичные amount_minor/payment_method_ref из [входного контракта](decisions.md#публичный-api-и-идемпотентность). Currency здесь уже строго `USD`. Лишние поля, null, невалидные типы — 422 `{"error":{"code":"validation_error","fields":["<имя поля>"]}}`, без списания и без эха значений. Вход key не является частью финансового payload. Provider хранит проверенный payload (amount_minor,currency,method_ref) и SHA-256 canonical JSON по правилам decisions; сравнивает и поля.

Сначала provider учитывает аутентифицированный валидный HTTP-вызов отдельным commit, до barrier/fault. Затем в финансовой транзакции получает/создаёт operation с UNIQUE(key), сохраняет payload, выбирает результат fixture, при success создаёт ledger с UNIQUE(operation_id), затем commit. Проигравший `INSERT ON CONFLICT(key) DO NOTHING` не вызывает UNIQUE-исключение; отдельный SELECT при READ COMMITTED читает победителя. Проверяются hash и поля. При действительной SQL-ошибке требуется rollback/savepoint, чтение из aborted transaction запрещено ([PostgreSQL 17](https://www.postgresql.org/docs/17/transaction-iso.html#XACT-READ-COMMITTED)). Если payload равен — каноническая операция; если иной — 409 `{"error":{"code":"idempotency_conflict"}}`, существующая операция не меняется и дополнительного списания нет. Конфликт не является разрешённым отказом текущего платежа: адаптер переводит его в unknown и сигнализирует проблему.

Любой корректный результат (новый или повтор) — HTTP 200, в том числе бизнес-отказ. Порядок JSON-полей незначим. Ответ содержит ровно `key, external_id, amount_minor, currency, method_ref, status, error_code`:

```json
{"key":"<UUID попытки>","external_id":"<UUID операции>","amount_minor":100,"currency":"USD","method_ref":"demo-ref","status":"succeeded","error_code":null}
```

| status | error_code | Ledger и дальнейшая судьба key |
|---|---|---|
| succeeded | null | ровно одна запись на всю жизнь операции |
| declined_retryable | issuer_unavailable | 0; финальный запрет списания по key, локально разрешён следующий provider |
| declined_terminal | invalid_payment_method | 0; финальный запрет списания, каскад запрещён |
| not_charged_final | not_charged_final | 0; терминальная tombstone-запись, исключающая позднее списание, каскад разрешён |
| pending | null | исход ещё не окончателен; следующий provider запрещён |

Все перечисленные операции имеют non-null external_id. Даже если ответ не дошёл, provider сохраняет его до разрыва. Повтор финальной операции возвращает те же поля/значения и external_id, не выполняет fixture заново. Повтор pending возвращает актуальное каноническое состояние той же операции. Метод и сумма после создания неизменны. Не допускается partial charge.

## Lookup без external_id

`GET /charges/by-key/{key}` с service credential, тела нет. Существующая операция — HTTP 200 с тем же envelope, что POST (включая pending и финальные отказы). Невалидный UUID — 422. Обычное отсутствие — HTTP 404 `{"key":"<UUID попытки>","status":"not_found"}`. Оно не создаёт tombstone, не доказывает отсутствие запроса в сети и не блокирует его позднее исполнение. При временном скрытии записи fixture возвращает тот же 404, но charge всё равно дедуплицируется по существующей операции.

Not_charged_final доступен только как сохранённая терминальная операция HTTP 200 с согласованным payload; это не синоним 404. Создание tombstone сериализуется с любым charge по key: если success уже существует, заменить его tombstone нельзя. Ни lookup, ни изменение fixture не меняют финальную запись.

Адаптер принимает финал только после проверки envelope: key, сумма, USD, method_ref совпадают с сохранёнными, external_id валиден, пара status/error_code известна. Любые неожиданные коды, поля, неверный JSON, несовпадения и HTTP-ошибки (включая 4xx/5xx) дают unknown. Не сохранять непроверенный external_id. Валидный 404 применяется только к lookup как not_found. Таймаут/разрыв может произойти до или после commit, поэтому не означает отказа.

По not_found разрешён charge той же попытки тем же payload/key до safe_retry_until, в рамках одного ограниченного цикла сверки. Это не переход к B и не новая attempt. Перед каждой отправкой повторно проверяются локальное владение и окно; если времени не хватает, charge пропускается. После окна — только lookup в оставшемся бюджете или ручной разбор. Защита от повторного списания при временном not_found обеспечивается provider UNIQUE/ledger, а не самим 404.

## Test/demo controls и независимые доказательства

Включаются только профилем test/demo и требуют service credential; вне профиля пути ниже — 404. Публичное payment API не содержит выбора fault-mode. На каждый тест/демо — уникальный method_ref и keys. Операции controls не сбрасывают ledger или финальные записи.

`PUT /__test/fixtures/{method_ref}` принимает JSON: `charge_mode` (обязателен), `lookup_mode` (default normal), `lookup_not_found_count` (default 0, целое >=0), `barrier` (default null). Возвращает 200 с сохранённой fixture. Method_ref валидируется как в charge, URL percent-encoded. Charge modes: success, declined_retryable, declined_terminal, not_charged_final, pending, charge_then_disconnect, timeout_before_result, malformed_response, http_5xx. Lookup modes: normal, temporary_not_found (ровно заданное число вызовов), unavailable (503). Режим влияет только на новые операции/представление ответа; существующие финалы не изменяются.

Success/declines/tombstone фиксируются в provider DB; pending создаёт pending. Charge_then_disconnect коммитит success + ledger и обрывает TCP/HTTP без полноценного ответа (если нужно — test-only TCP proxy). Malformed_response коммитит success, затем возвращает 200 с невалидным JSON; http_5xx коммитит success, затем отдаёт 503. Timeout_before_result удерживает запрос за barrier до истечения клиентских 2 с, затем обычный success с дедупликацией: тест не должен предполагать отсутствие списания из-за таймаута. Pending можно завершить через `POST /__test/operations/{key}/resolve` с `{"status":"succeeded","error_code":null}` либо одной из согласованных финальных пар; 200 с envelope, конфликт с иным уже финальным исходом — 409. Success и ledger фиксируются атомарно.

`barrier` = null либо `before_charge_commit`/`after_charge_commit_before_response`; состояние barrier наблюдается `GET /__test/barriers/{key}` → 200 `{"key":"<UUID>","point":"<имя>","reached":true,"released":false}` (до достижения reached=false). `POST /__test/barriers/{key}/release` с пустым объектом — 200 с released=true; повтор безопасен. Реализованный before_charge_commit останавливает обработчик **до начала атомарной транзакции charge**; after_charge_commit_before_response — после её commit. Оба не удерживают транзакцию/row lock во время ожидания. Состояние достигнутой точки хранится в собственной БД mock (миграции mock_0003/mock_0004); до достижения point=null/reached=false. Release для достигнутой точки идемпотентен; для ещё не достигнутого ключа состояние остаётся reached=false/released=false. Демо и тесты ждут reached=true, освобождают в finally; ожидание сервером ограничено 30 с. Polling опирается на наблюдаемое состояние, интервалы не определяют момент освобождения. Barrier для одного key после release не взводится заново. В production сохранённая fixture с barrier также игнорируется. Это синхронизация проверки; timeout_before_result автоматически использует before_charge_commit. Для границ worker/outbox/result commit применяются отдельные локальные failpoints процесса; в payment HTTP API они не выставляются.

`GET /__test/stats?key=<UUID>` или `?method_ref=<urlencoded ref>` — ровно один фильтр, иначе 422. Ответ 200:

```json
{"operation_count":1,"ledger_count":1,"ledger_amount_minor":100,"create_calls":2,"lookup_calls":1,"operations":[{"key":"<UUID>","external_id":"<UUID>","status":"succeeded"}]}
```

Нулевые показатели при отсутствии операций. create_calls/lookup_calls учитывают аутентифицированные валидные вызовы, включая дубли, временные 404 и моделируемые ошибки, до fault-ответа; хранятся устойчиво. ledger_amount_minor — точная целая сумма, агрегат NUMERIC без переполнения BIGINT. Счётчики вызовов и ledger различаются: два HTTP-запроса допустимы при одном списании. Stats только читают собственную БД provider; общий ledger через A/B вычисляет проверяющий CLI. Обычный stop/restart не очищает данные.

## Обязательства для реального адаптера

Наличие локального lease не заменяет provider идемпотентность. Без долговечного ключа, lookup без external_id и финального отказа, исключающего позднее списание, гарантию отсутствия повторного списания и безопасный каскад обещать нельзя. Реальные платёжные ref между провайдерами автоматически не переносятся.


## Реальный disconnect и повтор финала

Launcher `python -m mock_provider.server` в test/demo использует TCP proxy на
публичном mock-порту и loopback backend. После commit charge_then_disconnect
backend посылает внутренний marker; proxy закрывает клиентский socket без
HTTP-ответа. В production proxy выключен, новые операции игнорируют fixtures.

После учёта HTTP-вызова существующая операция проверяется до fixture/barrier:
равный payload сразу возвращает canonical, другой — 409. Поэтому смена fixture
на timeout/barrier не останавливает повтор финала/pending. Для новых конкурентных
операций сохраняются INSERT ON CONFLICT и отдельный SELECT победителя.
Lookup temporary_not_found уменьшает сохранённый счётчик под row lock fixture;
ровно указанное число lookup скрывает запись, включая конкурентные вызовы.
Lookup faults определяются по method_ref существующей операции; полностью
отсутствующий key возвращает обычный 404. Это не создаёт tombstone.
