# Матрица приёмки

Матрица связывает гарантии с исполняемыми проверками. Результаты запусков и
ограничения — в [verification.md](verification.md). Контракты —
[decisions.md](decisions.md), [provider-contract.md](provider-contract.md).
Все test IDs ниже существуют; пути без каталога относятся к `tests/integration/`.
Параметризованные IDs означают все варианты данного теста.

## Воспроизводимый запуск и уровни доказательств

Для полного pytest используйте [команду из README](../README.md#проверки).
Нужны запущенные PostgreSQL, Redis, API, worker, dispatcher и mock A/B с
применёнными миграциями. Для host-only runtime нужны также локальные зависимости,
правильный API_URL и demo/test-профиль mock.

[Аварийные проверки и runtime-скрипты](operations.md#аварийные-проверки-d6)
запускаются последовательно, отдельно от pytest. Они управляют процессами;
recovery runtime очищает Redis demo-проекта. Их успешный запуск не следует
из одного запуска pytest.

Unit проверяет точные часы/правила без I/O. DB проверяет SQL, ограничения,
блокировки и fencing, а не факт списания. Transport проверяет доставку/ACK/reclaim.
Финансовые HTTP e2e проверяют независимый provider ledger, create_calls и lookup_calls
по уникальному method_ref/key, а не по общим накопленным stats. Повтор Taskiq
публикуется в настоящий stream. Barriers удерживают наблюдаемую точку до release;
polling ожидает условие с timeout, время sleep не определяет корректность.

`R::<case>` ниже — строка case JSON из
`scripts/check_recovery_runtime.py::run_recovery`; это отдельная host-only аварийная
проверка, не pytest. Настоящие SIGKILL/запуск происходят на сохранённых БД.

## Демонстрации

Команда D1–D5: `docker compose --profile demo run --rm demo <scenario>`.
D6: `poetry run python scripts/demo.py restart-recovery` на хосте.

| ID | scenario / исполняемая проверка | Ожидаемый результат |
|---|---|---|
| D1 | first-success; `test_first_payment.py::test_first_success` | A succeeded, ledger 1/0, amount совпадает, B calls=0 |
| D2 | cascade-success; `test_cascade.py::test_cascade_http` | Подтверждённый retryable A → B, две attempts, ledger 0/1 |
| D3 | all-declined; `test_cascade.py::test_cascade_http` | Два подтверждённых отказа → failed, ledger 0/0 |
| D4 | lost-response; `test_unknown.py::test_charge_then_disconnect` | unknown/null external_id после commit/disconnect, lookup → success, ledger 1/0 |
| D5 | duplicate-delivery; `test_concurrency.py::test_d5_http_taskiq` | 10 POST → один payment; дубль ACK при открытом HTTP; ledger 1/0 |
| D6 | restart-recovery; `R::charge_before_result`, `R::combined_failures` | SIGKILL/restart без нового POST, прежние payment/attempt/key/window, ledger 1/0 |

## Вопросы корректности

| ID | Проверки | Доказательство |
|---|---|---|
| Q1 | D4 | Потерянный external_id восстанавливается lookup старого key |
| Q2 | `test_concurrency.py::test_repository_fencing`; `R::late_owner`; `test_fault_matrix.py::test_stale_result_then_canonical_lookup` | Lease/version ограждают ответ старого владельца; canonical результат сохраняет новый |
| Q3 | D2/D3; `test_unknown.py::test_pending_lookup_final`; `tests/unit/test_policy.py::test_policy` | B только после разрешённого подтверждённого отказа |
| Q4 | `test_unknown.py::test_temporary_not_found_same_key`; `tests/unit/test_reconciliation_limits.py::test_safe_retry_boundary`; `test_reconciliation_db.py::test_retry_checks_saved_window_with_db_time` | Повтор тем же key в окне; на/после границы charge запрещён |
| Q5 | `test_unknown.py::test_budget_manual_no_sixth_lookup`; `R::fifth_claim_manual_restart` | Пять реальных HTTP lookup отдельно от SQL fixture crash5; manual сохраняется |
| Q6 | `test_idempotency.py::test_direct_mock_race`; `scripts/check_first_payment_runtime.py`; D1–D6 | Независимые БД provider/ledger, канонический финал после restart |

## Границы сбоев

| ID | Исполняемая проверка | Ожидаемый результат / уровень |
|---|---|---|
| G1 | `test_unknown.py::test_ambiguous_results`; `tests/unit/test_http_provider.py::test_envelope_and_transport` | Неоднозначный HTTP → unknown, B=0; malformed envelope отдельно unit |
| G2 | `test_unknown.py::test_temporary_not_found_same_key` | Скрывшийся success: create2/ledger1, прежние key/attempt |
| G3 | `test_idempotency.py::test_client_race` | Конфликт payload →409; merchant scope; конкурентный POST |
| G4 | `test_concurrency.py::test_duplicate_cascade_result_transaction`; `scripts/check_idempotency_runtime.py` | DB атомарный B/outbox; HTTP дубль B без третьей попытки/списания |
| G5 | `R::commit_before_publish` | Crash payment/outbox commit → publish; исходный payment |
| G6 | `R::publish_before_mark` | Crash после publish до mark, ledger1 |
| G7 | `R::published_redis_loss` | Redis flush/recreate после published, PostgreSQL recovery |
| G8 | `R::claim_before_http` | Crash до HTTP, expiry/lookup/not_found/retry старого key |
| G9 | `R::late_owner`; `test_fault_matrix.py::test_stale_result_then_canonical_lookup`; `test_reconciliation_db.py::test_reconciliation_rejects_stale_foreign_expired_and_manual_results` | Старый ответ не меняет canonical; DB success/decline без противоречивого provider финала |
| G10 | `R::fifth_claim_manual_restart`; `scripts/check_reconciliation_runtime.py` | Manual после Redis/process/mock restart, без HTTP, next=NULL |
| G11 | `test_first_payment.py::test_negative_jwt_and_validation_http`; `test_first_success`; `scripts/check_first_payment_runtime.py` | JWT отрицательные случаи, merchant404, безопасные ответы/логи |
| G12 | `test_first_payment.py::test_valid_input_limits`; `test_negative_jwt_and_validation_http`; `tests/unit/test_domain.py::test_strict_amount`, `test_strict_ref` | BIGINT max/1, ref1/255, строгие типы и границы; недопустимое422 |
| G13 | `test_mock_faults.py::test_pending_resolve_final_is_immutable_and_lookup_counter_is_exact`; `test_existing_operation_bypasses_changed_fixture_and_barrier`; `scripts/check_reconciliation_runtime.py` | Неизменные success/decline/tombstone при повторе, fixture и restart; конфликт409 |
| G14 | `R::combined_failures` | 10 POST + три дубля Taskiq при удержанном HTTP + SIGKILL/recovery: одна A/key/window, ledger1, B calls0 |
| G15 | `tests/unit/test_domain.py::test_terminal_cannot_be_overwritten`; `test_succeeded_rejects_every_late_outcome`; `tests/unit/test_policy.py::test_inactive_attempt_cannot_create_next` | Домен запрещает перезапись terminal всеми поздними outcome |
| G16 | `R::fifth_claim_manual_restart`; `test_reconciliation_transport.py::test_due_selection_preserves_live_owner_and_selects_exhausted_before_due` | SQL count4, реальный claim5 commit/SIGKILL до lookup, expiry/manual; не остаётся due |
| G17 | `test_reconciliation_db.py::test_fifth_live_owner_saves_final_after_deadline_atomically`; D4 | DB пятый owner сохраняет финал до expiry после deadline; `R::fifth_owner_success_after_deadline` подтверждает настоящий lookup/ledger1 |
| G18 | `test_reconciliation_db.py::test_fifth_nonfinal_saves_manual_without_sixth_lookup`; `test_unknown.py::test_budget_manual_no_sixth_lookup` | DB pending/unknown/not_found →manual; HTTP пять lookup и ни одного шестого |
| G19 | `test_reconciliation_db.py::test_reconciliation_rejects_stale_foreign_expired_and_manual_results`; `test_live_lease_precedes_limits_then_expiry_marks_manual`; `test_fault_matrix.py::test_fifth_expired_response_before_recovery_keeps_version_then_manual` | Expired прежняя version отвергается, manual без нового lookup |
| G20 | `test_reconciliation_db.py::test_fifth_live_owner_saves_final_after_deadline_atomically`; `test_deadline_precedes_due_and_no_new_claim` | Активный lease важнее deadline, новый claim после deadline запрещён |
| G21 | `R::claim_before_ack_reclaim`; `R::commit_before_ack_reclaim`; `test_bootstrap.py::test_stream_reclaim_without_publications` | Исходный stream ID reclaimed/ACK без новых сообщений/DB recovery; после commit дубль без HTTP |
| G22 | `tests/unit/test_reconciliation_limits.py::test_initial_due_and_deadline_are_bounded`; `test_backoff_from_result_preserves_deadline_and_count`; `test_backoff_never_exceeds_deadline`; `test_claim_order_and_boundaries`; `test_safe_retry_boundary`; `tests/unit/test_recovery_policy.py::test_expiry_preserves_identity_window_budget_and_deadline` | 1/2/4/8/16, точные границы lease/deadline/window; crash не сбрасывает историю |
| G23 | `test_initial_window_db.py::test_expired_created_transition_is_atomic_and_duplicate_is_noop`; `test_expired_created_process_commits_without_calling_provider`; `test_first_window_checked_after_waiting_for_payment_lock` | Истёкшее окно первой отправки → unknown/outbox без charge/B; проверка времени после lock, дубль безопасен |
| G24 | `test_recovery.py::test_unknown_recovery_readiness`; `test_new_attempt_initialization_and_saved_window_survive_cascade`; `test_reconciliation_transport.py::test_readiness_exact_db_time_and_explicit_limit` | Общий SQL-отбор учитывает точные границы и явный лимит; новая A/B получает отдельный key/window, повтор сохраняет их |

## Подготовительные fixtures и ограничения

DB-тесты используют rollback/временные таблицы либо уникальные UUID и удаляют
только собственные данные. Низкоуровневые SQL fixtures задают count, due, lease,
deadline для точной границы; это не выполненные HTTP-вызовы.
`R::fifth_claim_manual_restart` задаёт count4; реальные lookup=0, crash происходит
после сохранения claim5. `R::fifth_owner_success_after_deadline` тоже задаёт count4 и прошедший deadline
через SQL; реальный lookup один, pending разрешён в success ровно один раз.
Пять реальных lookup подтверждают отдельный HTTP e2e
и reconciliation runtime. Эти уровни не складываются в вымышленную историю.

Ни одного reset общей provider БД. Гарантия одного ledger относится к согласованному
mock-контракту долговечной идемпотентности/неизменного финала; не к любому реальному
провайдеру. Транспорт допускает дубли; reclaim требует сохранности Redis, восстановление
после потери Redis требует PostgreSQL. Потеря PostgreSQL volume, нагрузка, SLA,
реальные деньги и exactly-once транспорта не проверяются и не обещаются.
