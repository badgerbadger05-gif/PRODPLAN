# Проверка операторских данных в репетиции миграции

Канонический переносимый verifier: `tools/operational_preservation.py`.
Он использует существующий `_table_digest` из `current_execution_migration`,
читает PostgreSQL в `REPEATABLE READ READ ONLY` и не запускает миграции,
refresh, rebase либо запись предметных данных. UTC нормализует timestamp
для одинакового хеша на разных компьютерах. Нужны backend requirements.

## Запуск на свежем локальном восстановлении

Остановить writers **локальной копии** на весь промежуток сравнения. Указать
`DATABASE_URL` этой копии через окружение; в receipt DSN и пароль не попадают.
Далее из корня checkout того же SHA, который будет использоваться в окне:

```powershell
python tools/operational_preservation.py capture-before --output output/rehearsal/operator-before.json
# Выполнить проверенный конвейер миграции на этой же локальной копии:
# Alembic 20260911_01 -> current-owner preflight/apply/postflight -> Alembic head.
python tools/operational_preservation.py verify-after --before output/rehearsal/operator-before.json --output output/rehearsal/operator-after.json
```

Первый шаг принимает только source revision `20260909_02`; второй требует
target `20260925_01` и прежнее имя БД. Проверять следует **до** rebase/refresh,
которые закономерно обновляют ряд operational compatibility fields. Возврат
0 и `status=passed` обязательны. Существующий receipt не перезаписывается.
При переносе результата на сервер нужен отдельный контроль транспортировки
и свежести относительно fenced emergency, этот verifier их не доказывает.

## Интеграция вместо ad-hoc `inventory(before['columns'])`

Готовый tracked runner `tools/local_cutover_migration.py` вызывает тот же
канонический конвейер и этот verifier. Он принимает только loopback PostgreSQL
на явно заданном порту, отличном от 5432, с изолированной ролью и БД с префиксом
`prodplan_weekend_` или `prodplan_cutover_rehearsal_`. Параметры URL query
запрещены, включая обход host/service. Нужен receipt восстановленного дампа:

```json
{
  "receipt_version": 1,
  "status": "restored",
  "verified_backup": true,
  "restore_verified": true,
  "source_revision": "20260909_02",
  "backup_sha256": "64 lowercase hexadecimal characters",
  "source_dump_bytes": 1,
  "target": {
    "host": "127.0.0.1",
    "port": 55441,
    "database": "prodplan_weekend_20261002",
    "user": "weekend_rehearsal"
  }
}
```

`backup_sha256`/`source_dump_bytes` заменяются реальными проверенными hash/size.
Receipt обязан создавать restore-процесс после проверки исходного артефакта
и успешного восстановления. Runner проверяет структуру и совпадение identity,
но не пересчитывает hash дампа и не доказывает свежесть unfenced snapshot.

```powershell
python tools/local_cutover_migration.py --expected-database prodplan_weekend_20261002 --restore-receipt output/rehearsal/restore.json --writers-stopped --dry-run --output output/rehearsal/dry-run.json
python tools/local_cutover_migration.py --expected-database prodplan_weekend_20261002 --restore-receipt output/rehearsal/restore.json --writers-stopped --output output/rehearsal/migration.json
```

`DATABASE_URL` указывает **ту же локальную БД** из receipt. Dry-run выполняет
только identity/settings/source guards и сохранение before; не запускает
Alembic либо canonical publishers. Использовать разные имена receipt для
dry-run и apply. Каждый before сохраняется отдельным неизменяемым файлом
`<output-stem>-before.json` до первой мутации. Основной receipt резервируется
новым файлом и содержит последнюю фазу и безопасный тип ошибки при остановке.
Успех миграции — `status=passed` и exit 0; `dry_run_passed` не является
приёмкой результата миграции. Повтор после частичного failure требует нового
восстановления исходного дампа, а не обхода guard или запуска с новой схемы.

Все local connections получают process/session-only backup guard,
`maintenance_work_mem=2GB` и `Europe/Moscow`; PostgreSQL/production config
runner не меняет. Предусмотренный chain: `20260911_01` → existing R10
preflight/apply/postflight → Alembic head → operational preservation.
Refresh/rebase/физическая GC в этот runner не входят. Его `rehearsal_only=true`
явно запрещает трактовать успех на unfenced дампе как разрешение переноса
подготовленной локальной БД в действующий production.

В tracked runner использовать:

```python
from tools.operational_preservation import capture_before, verify_preservation

before = capture_before(engine)
# Сохранить before в отдельный неизменяемый receipt до любых изменений.
# ... существующий конвейер миграции ...
preservation = verify_preservation(engine, before)
payload['operational_preservation'] = preservation
if preservation['status'] != 'passed':
    raise RuntimeError('operational preservation blocked')
```

CLI и `capture_before` валидируют полную allowlist и source revision при capture;
`verify_preservation` валидирует baseline и revision pair самостоятельно.
Не копировать старый `staged-migrate.py` с серверным абсолютным путём: импорт
этого helper должен ехать с main и использовать изолированную rehearsal БД.

## Разрешённое изменение схемы и границы доказательства

Revision `20260911_01_r9_purchase_current_anchor.py` добавляет к
`purchase_export_batch` поля `current_execution_scope_id` и
`current_execution_source_revision`. Revision
`20260911_02_r10_purchase_anchor_cutover.py` удаляет
`planning_read_snapshot_id`, проверив отсутствие legacy anchors и наличие
обоих новых anchors. Verifier разрешает ровно эту комбинацию изменения
полей только при **нуле строк до и после**. Во всех остальных таблицах все
исходные поля, типы/nullability/primary key, количество и хеш строк обязаны
совпасть. Оба новых anchor поля должны иметь типы и NOT NULL из миграции.
Даже другое изменение
пустого поля блокируется. `production_order_lines` — явно зафиксированное
отсутствующее имя из старой allowlist; его появление тоже блокируется.

Непустые purchase batches нельзя "исключить из хеша": их migration изменяет
anchor values, поэтому нужен отдельный доказанный mapping export history.
Этот случай пока fail closed, даже если строки внешне сохранены.

Старый `production-migration.json` завершился ошибкой SELECT удалённой
колонки, не содержит успешного after inventory и использует другой хеш
(сортированный JSON без length prefix). Его нельзя преобразовать в passed
или сравнить хеши с новым inventory. Успешный R10 postflight и последующий
rebase не доказывают сохранность всех операторских строк всей Alembic chain.
Для доказательства повторить восстановление исходного дампа и оба шага.

Verifier доказывает сохранность только перечисленных operational tables и
предусмотренное изменение их полей. Он не подменяет R10 publication
postflight, канон, физический Ledger fold, проверку всего дампа, refresh,
UI smoke или метрики размера БД.

## Обычные физические циклы после подготовки

`tools/local_cutover_refresh.py` вызывает существующий canonical physical
refresh на изолированной копии. Он не запускает миграцию, historical replay,
discard или запись в 1С. Перед запуском установить `TZ` и `PGTZ` в
`Europe/Moscow`, указать local `DATABASE_URL`; runtime OData config хранится
вне Git. Конфигурация закрепляется отдельным SHA256, пароль в receipts не пишется.

```powershell
python tools/local_cutover_refresh.py --database prodplan_weekend_20261002 --port 55441 --writers-stopped --expected-parent <accepted-id> --cutoff <ISO-time-with-offset> --generation-key local-rehearsal-cycle-1 --odata-config <private-config.json> --config-sha256 <sha256> --max-duration-seconds <upfront-budget> --max-replayed-rows <upfront-budget> --output output/rehearsal/cycle-1.json
```

Положительные бюджеты фиксируются в receipt до мутаций. Канонический R11
порог 600 секунд остаётся виден отдельно и не отменяется пользовательским
бюджетом. Cutoff должен быть реальным прошедшим временем, не более суток от
parent; discovery lookback равен нулю, полный аудит старых документов выключен.
Индивидуальные SQL имеют statement/lock timeout; общий бюджет времени является
гейтом приёмки, а не принудительным убийством всей транзакционной цепочки.

Второй запуск использует следующий cutoff, новый key и receipt, актуальный
parent. Нулевой вход отмечается `no_op` и не засчитывается как изменяющий цикл.
Для приёмки нужны два `counts_as_changing_cycle=true`, корректный accepted
pointer, balance convergence, нулевой custody-хвост и 11 готовых scopes.
BUILDING или foreign terminal до запуска требуют отдельной поддержанной
recovery-процедуры на локальной копии; runner не очищает их автоматически.

## §58, физический fold и API после миграции

После подтверждённого `verify-after`, head `20260925_01` и необходимого
physical refresh использовать `tools/local_cutover_postflight.py`:

```powershell
python tools/local_cutover_postflight.py rebase --database prodplan_weekend_20261002 --port 55441 --generation <accepted-id> --writers-stopped --max-seconds <upfront-budget> --output output/rehearsal/rebase.json
python tools/local_cutover_postflight.py fold --database prodplan_weekend_20261002 --port 55441 --generation <accepted-id> --writers-stopped --max-seconds <upfront-budget> --output output/rehearsal/physical-fold.json
python tools/local_cutover_postflight.py api --database prodplan_weekend_20261002 --port 55441 --generation <accepted-id> --writers-stopped --max-seconds <upfront-budget> --output output/rehearsal/api.json
```

`rebase` использует только существующий canonical allocator и обязательно
republish в той же транзакции, даже при нуле changed pairs. Изменение frozen
или operator fingerprint и превышение бюджета откатывают транзакцию.
`fold` читает каноническую physical visibility и fold с full key и последним
SLE witness; `api` исполняет GET без lifespan, с SQL/ORM/network guards.
Эти receipts не заменяют визуальный smoke и проверку переноса fenced источника.

Инвентаризация R10 сохраняет прежний row-JSON SHA и порядок, но читает
доказанно ограниченные scalar строки пакетами. Малый JSON в стабильном
snapshot передаётся как ограниченный raw text и декодируется по одной строке
перед хешем. Wide/unknown/custom-codec и небезопасный JSON primary key остаются
в native one-row режиме. Это не исключение строк или колонок из проверки.
