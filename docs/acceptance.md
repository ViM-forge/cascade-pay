# Критерии приёмки и будущие проверки

Статус: контракт проверки; D1–D3 реализованы и проверены, D4–D6 пока будущие. Фактические результаты отдельно в [verification.md](verification.md). Указанные test IDs — обязательные будущие имена; менять их можно только вместе с этой матрицей. Нормативные [решения](decisions.md) и [HTTP provider contract](provider-contract.md) самодостаточны.

## Метод доказательства

Unit проверяет переходы и часы; integration/e2e использует настоящие PostgreSQL, Redis/Taskiq и HTTP без подмены repository/provider. CLI получает свежий merchant JWT, использует уникальные key/ref, polling с общим timeout и завершается ненулевым кодом при провале assertion. Число списаний берётся из устойчивого provider ledger через HTTP stats, не из статуса payment; create_calls и lookup_calls отдельны. A/B счётчики проверяются по ref текущего сценария. Барьеры/failpoints детерминируют порядок вместо случайных sleep. Обычный restart сохраняет volumes. Нагрузочная проверка не обещана.

## Шесть демонстраций

D1–D3 доступны по README и фактически запускались. Команды D4–D6 — будущий интерфейс.

| ID / сценарий / будущая команда | Контракт и ожидаемый результат | Будущий тест |
|---|---|---|
| D1 A success; `docker compose --profile demo run --rm demo first-success` | POST 202; GET succeeded; одна A succeeded с external_id; A ledger_count=1 и ledger_amount_minor=amount, B create_calls=0 | `tests/integration/test_first_payment.py::test_first_success` |
| D2 A decline → B success; `docker compose --profile demo run --rm demo cascade-success` | A declined_retryable/issuer_unavailable сохранён до создания B; GET succeeded, sequence 1/2; ledger A/B=0/1 | `tests/integration/test_cascade.py::test_cascade_http` |
| D3 оба decline; `docker compose --profile demo run --rm demo all-declined` | два подтверждённых retryable отказа; GET failed, ровно две declined, ledger 0/0; нет третьей попытки | `tests/integration/test_cascade.py::test_cascade_http` |
| D4 списание и потеря ответа; `docker compose --profile demo run --rm demo lost-response` | Реальный разрыв после provider commit; наблюдаемый unknown, external_id=NULL; lookup по key → succeeded; A ledger=1, B create_calls=0 | `tests/integration/test_unknown.py::test_charge_then_disconnect` |
| D5 повтор клиента/задачи; `docker compose --profile demo run --rm demo duplicate-delivery` | 10 одинаковых конкурентных POST → один UUID/202; настоящие дубли Taskiq до/во время/после HTTP; одна A, ledger=1, B calls=0 | `tests/integration/test_concurrency.py::test_duplicate_delivery` |
| D6 restart; `sh scripts/demo-recovery.sh` | Wrapper останавливает реальный worker на barrier после списания до result commit, перезапускает с теми же данными. Без нового POST тот же payment/attempt/key → succeeded, ledger=1 | `tests/integration/test_recovery.py::test_restart_after_charge` |

D4/D6 обязаны показывать достигнутую промежуточную точку, а не только быстрый succeeded. D5 публикует существующий payment_id через Taskiq, не вызывает handler напрямую. CLI выводит UUID, статус и агрегаты без токенов/секретов/ref. Не требует ручного SQL или скрытой подготовки.

## Шесть вопросов корректности

| ID / вопрос | Нормативное решение, поле/контракт | Будущая проверка и наблюдение |
|---|---|---|
| Q1 Потерян external_id | Provider lookup по key; key=UUID attempt сохраняется до HTTP, external_id nullable | D4; `test_unknown.py::test_lookup_without_external_id`: списание найдено, B=0 |
| Q2 Одновременно worker/recovery | Decisions: lock payment → attempt, lease_until/claim_version, commit до HTTP, условное сохранение ответа | `test_concurrency.py::test_active_lease_noop`, `test_recovery.py::test_stale_owner`: дубли не вызывают HTTP; поздний ответ не меняет состояние |
| Q3 Когда разрешён каскад | Таблица результатов decisions; provider финальный отказ исключает позднее списание | D2/D3; `test_cascade.py::test_terminal_decline`, `test_unknown.py::test_lookup_final_decline`: B только после retryable/not_charged_final |
| Q4 Когда допустим повтор | Неизменные payload/key, safe_retry_until=attempt created_at+24ч; provider хранит ключи весь срок данных | `test_unknown.py::test_not_found_same_key`, `tests/unit/test_reconciliation_limits.py::test_safe_retry_boundary`: до окна прежний key; на границе/после — charge=0 |
| Q5 Как остановить сверку | Persisted check_count/deadline/next_check_at/manual_review_required; неизвестность не failed | `test_unknown.py::test_manual_review`, `test_fault_matrix.py::test_limits_survive_restart`: не более 5 циклов; unknown/manual; Redis/restart не оживляет |
| Q6 Чем доказано списание | Отдельные provider DB, UNIQUE(key), UNIQUE(operation_id), atomic operation/ledger, защищённый HTTP stats | `test_idempotency.py::test_mock_race_and_restart`: конкурентный charge и restart → тот же external_id, ledger=1; D1–D6 |

Пути test_*.py без префикса в этой таблице относятся к `tests/integration/`.

## Дополнительные гарантии и границы сбоев

Все строки ниже — будущие проверки, не текущие результаты.

| ID | Будущий test ID (каталог tests/integration/, если не указано иное) | Требуемый результат |
|---|---|---|
| G1 | `test_unknown.py::test_ambiguous_results` (timeout/pending/5xx/malformed/неожиданный 4xx/неизвестный code/неверный envelope) | unknown, никакого B; исчерпание не превращает в failed |
| G2 | `test_unknown.py::test_temporary_not_found` | Даже если lookup скрывает списание, повтор charge тем же key даёт ledger=1; новый key/attempt не появляется |
| G3 | `test_idempotency.py::test_payload_conflict` / `test_merchant_scope` | Другие данные под merchant/key →409, включая гонку; другой merchant с тем же key →другой payment |
| G4 | `test_concurrency.py::test_duplicate_cascade` | Дубли перехода A→B не создают третью attempt, B ledger<=1 |
| G5 | `test_recovery.py::test_outbox_commit_before_publish` | Сбой после payment/outbox commit, до publish: без нового POST восстановление исходного ID |
| G6 | `test_recovery.py::test_publish_before_mark` | Сбой после публикации до published_at: допустим дубль сообщения, ledger=1 |
| G7 | `test_recovery.py::test_redis_loss_after_published` | Утрата Redis после published_at, PostgreSQL сохранён: recovery ставит задачу заново; тот же payment/attempt/key, ledger=1 |
| G8 | `test_recovery.py::test_claim_before_http` | Crash после claim до HTTP: expired lease → lookup, при not_found повтор старого key в окне; ledger=1 |
| G9 | `test_recovery.py::test_stale_owner` | Ответ с прежней version или истёкшим lease не меняет состояние; новый lookup сохраняет канонический исход |
| G10 | `test_fault_matrix.py::test_manual_stays_stopped` | unknown/manual после restart Redis/worker/dispatcher остаётся без HTTP; next_check_at=NULL |
| G11 | `test_auth.py::test_jwt_validation` / `test_merchant_isolation` | Нет/неверный/просроченный JWT, issuer/audience/claims/algorithm/sub →401; чужой ID →404; секреты не выходят в ответ/лог |
| G12 | `test_validation.py::test_strict_inputs` | Граничные входы согласно decisions, сохранение допустимого максимума в настоящей БД без переполнения; недопустимое →422 без payment/outbox |
| G13 | `test_provider_contract.py::test_final_records_immutable` | Повторы и смена fixture не меняют success/decline/tombstone, после разрешённого отказа ledger=0 навсегда; разные payload →409 |
| G14 | `test_fault_matrix.py::test_combined_failures` | Конкурентные POST + дубли Taskiq + задержанный ответ + recovery: один payment, нет B до финального разрешения, ledger=1 |
| G15 | `tests/unit/test_domain.py::test_terminal_cannot_be_overwritten` | Нельзя заменить succeeded/declined поздним результатом; проверка без ORM/HTTP |

### Последняя сверка и транспорт: обязательные граничные тесты

Все IDs в `tests/integration/test_fault_matrix.py`, кроме явно указанного unit.

| ID / будущий тест | Управляемая граница и ожидаемый результат |
|---|---|
| G16 `test_fifth_check_crash_before_http` | Зафиксировать count=5 и lease, убить worker до HTTP; до истечения lease no-op, после — unknown/manual, lease=NULL, next=NULL, version увеличена. Lookup шестой раз не выполняется; запись больше не застревает в due-выборке |
| G17 `test_fifth_check_owner_success` | При count=5 пятый владелец получает success и коммитит до конца lease; конкурирующий recovery, включая наступивший deadline, не мешает succeeded; ledger=1 |
| G18 `test_fifth_check_owner_unknown` | Пятый владелец сохраняет pending/error/not_found без разрешённого результата →unknown/manual; дальнейшего HTTP нет |
| G19 `test_fifth_check_expired_response` | Count=5, lease истёк; ответ прежнего владельца, даже до запуска recovery, отвергается; recovery включает manual без шестого lookup |
| G20 `test_deadline_owner_and_new_claim` | Действующий владелец сохраняет финал после deadline до lease expiry; новый владелец при now>=deadline не получает claim, даже если count<5 |
| G21 `test_stream_redelivery_without_new_messages` | Изолировать DB recovery, убить consumer до ACK, оставить stream без новых публикаций; periodic reclaim возвращает исходное сообщение другому consumer и после обработки снимает pending. Отдельно crash после DB commit до ACK →дубль без второго списания |
| G22 `tests/unit/test_reconciliation_limits.py::test_backoff_and_boundaries` | 1с до первой, затем 2/4/8/16с от результата; due=min(backoff,deadline). На lease/deadline/window равенство уже истечение; crash не сбрасывает count/deadline/window |

G12 включает: 1 и BIGINT max проходят API/БД/provider без потери точности; 0, -1, max+1, true, 1.0, 1e0, "1", null →422; ref 1/255 символов принят, 0/256/whitespace/control/non-string отвергнут; лишние поля запрещены; usd нормализуется в USD и повтор даёт тот же id. Не менять ref и проверять 255 Unicode-кодовых точек, а не байт.

G21 проверяет именно транспорт при отключённом recovery; G7 — восстановление из PostgreSQL при утрате Redis. Один тест не заменяет другой.

## Статус текущей проверки

D1 подтверждён `tests/integration/test_first_payment.py::test_first_success` и CLI
first-success: настоящий HTTP/Taskiq, ledger A=1, B calls=0. В том же файле проверены
базовый repeat/conflict, JWT/изоляция/строгий ввод и исключение до commit без ACK.
Domain terminal invariant проверен test_terminal_cannot_be_overwritten. Runtime
script отдельно подтвердил mock restart/lookup/ledger, unknown при недоступном A,
production controls и безопасные логи. Полные G/Q и D2–D6 не объявляются пройденными:
например, базовый повтор задачи не заменяет D5 с конкуренцией, а mock restart не
заменяет recovery D6. Конкретные команды и границы — в verification.md.
Защита от повторного списания зависит от provider contract и сохранности PostgreSQL;
exactly-once транспорт, реальный процессинг, SLA и нагрузка не доказаны.


## Проверено для последовательного каскада

`tests/integration/test_cascade.py` через настоящий HTTP/API/Redis/Taskiq проверяет
D1–D3, terminal decline A без B и not_charged_final A → B. Списания доказываются
HTTP stats независимого ledger: 1/0, 0/1, 0/0; сумма 321 в e2e. Проверяются
sequence/UUID/key, число outbox, история GET, повтор терминальной задачи,
повтор отказа и смена fixture, конфликт payload без ledger.

`tests/unit/test_policy.py` проверяет все нормализованные результаты на обеих
позициях маршрута, запреты для неактивных payment/attempt и неверные пары кодов.
`tests/unit/test_http_provider.py` проверяет adapter настоящим локальным HTTP:
полный envelope, B credential/payload, pending/not_found, повреждённый JSON,
неожиданные HTTP-коды, disconnect и timeout. Это проверка adapter отдельно от
платёжного e2e; она не объявляется финансовой fault-matrix.

Автоматический lookup/reconciliation не реализован: обычный not_found проверен
как запрет в policy/adapter, а не как завершённый сценарий автоматической сверки.
Детали и ограничения фактических запусков — [verification.md](verification.md).


Граница snapshot согласована: маршрут остаётся фиксированным A → B, изменяемая
настройка не добавлена. Проверяются сохранённый JSONB snapshot, round-trip
доменной модели, запрет изменения и выбор следующего provider из snapshot.


## Проверка конкурентной идемпотентности

- `test_idempotency.py::test_client_race`: 10 POST, одинаковые/конфликтующие данные,
  нормализация USD, terminal repeat, merchant scope, одна payment/attempt/outbox,
  фактический READ COMMITTED.
- `test_idempotency.py::test_direct_mock_race`: оба независимых mock, success и все
  три финальных отказа; 10 конфликтующих + 10 одинаковых HTTP, canonical external_id,
  одна operation, ledger отдельно от create_calls/lookup_calls.
- `test_concurrency.py::test_d5_http_taskiq`: CLI D5 через настоящий HTTP/Taskiq на
  обеих barrier-точках, ACK дубля до освобождения исходного запроса, вызовы/ledger.
- `test_http_worker_rejects_stale_result`: принудительная смена version/expiry lease
  во время настоящего HTTP; после ACK устаревший ответ не сохранён, ledger=1.
- `test_repository_fencing`, `test_lease_checked_after_waiting_for_payment_lock`,
  `test_duplicate_cascade_result_transaction`, `test_fields_are_compared_even_when_hash_equal`:
  дополнительные DB-тесты владельца/времени после lock, повторного результата,
  неизменности terminal и сравнения полей при искусственно равном hash.
- `test_barrier_credentials`: отсутствие/credential другого provider даёт 401.
- `scripts/check_idempotency_runtime.py`: queued duplicates до первого HTTP,
  конкурентный дубль B, разные PID worker, HTTP calls 1/1 и ledger 0/1.
- `scripts/check_first_payment_runtime.py`: canonical success/отказы после restart,
  controls (включая barrier) возвращают 404 в production, логи безопасны.

D5 реализован; D4/D6, автоматическая сверка и DB recovery остаются будущим объёмом.
Низкоуровневые DB-проверки дополняют финансовые e2e, не подменяют их.
