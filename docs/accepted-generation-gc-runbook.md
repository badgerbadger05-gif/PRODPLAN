# Accepted-generation GC and physical reclaim runbook

Этот контур предназначен только для локальной PostgreSQL-копии. Он не
является процедурой развёртывания на production и не должен запускаться с
production DSN.

## Политика

По умолчанию сохраняются две accepted generation (`retain_accepted=2`), exact
`PlanningTruthState.current_generation_id`, все `BUILDING` generations и любые
generation, на которые указывают неразрешённые FK/активные ссылки. Порядок
retention — явный `accepted_at DESC, id DESC`; это не выбор бизнес-владельца и
не fallback для чтения.

## Обязательная последовательность

1. Остановить writers и сделать verified custom-format backup с проверкой
   restore.
2. Сначала построить read-only manifest:

   ```powershell
   python tools/accepted_generation_gc.py `
     --database-url $env:DATABASE_URL `
     --phase dry-run `
     --retain-accepted 2 `
     --output gc-manifest.json
   ```

   `status` должен быть `ready`. `cleanup_candidate_generation_ids` — полный
   старый accepted-кандидат; `deletable_generation_ids` — только те, чью
   metadata можно удалить; `retained_metadata_generation_ids` и
   `metadata_blockers_by_generation` объясняют сохранённые generation. Unknown
   dependency блокирует только metadata; evidence cleanup остаётся разрешённым
   лишь для таблиц без blocker в `evidence_delete_generation_ids`. Manifest
   fingerprint нельзя редактировать.
3. После повторной проверки manifest применить GC только в loopback-сессии.
   Guard передаётся именно в session через `PGOPTIONS` и не является
   заменой backup:

   ```powershell
   $env:PGOPTIONS = "-c prodplan.execution_projection_backup_ready=on"
   python tools/accepted_generation_gc.py `
     --database-url $env:DATABASE_URL `
     --phase apply `
     --manifest gc-manifest.json `
     --writers-stopped
   ```

   Reservation events сначала группируются set-based в
   `reservation_event_archive` с `ON CONFLICT` aggregation. Current owners и
   current events не архивируются и не удаляются. Затем удаляются только
   проверенные historical rows; generation metadata удаляется последней и
   только при отсутствии RESTRICT dependencies.
4. Physical reclaim — отдельная операция. Сначала только план:

   План оценивает `pg_total_relation_size`; свободное место сервера Docker
   нужно передать явно:

   ```powershell
   python tools/accepted_generation_gc.py `
     --database-url $env:DATABASE_URL `
     --phase reclaim-plan `
     --available-free-bytes 50000000000 `
     --output reclaim-plan.json
   ```

   Если доступен
   `pg_repack`, он предпочтителен; иначе потребуется `VACUUM FULL` по одной
   allowlisted table (включая `ledger_build_batch`, где после GC могут
   оставаться live build-batch rows и крупный dead space). Required free space
   считается по крупнейшей отдельной операции, с conservative factor, а не суммой
   таблиц. Только при осознанном
   окне простоя запускается:

   ```powershell
   python tools/accepted_generation_gc.py `
     --database-url $env:DATABASE_URL `
     --phase reclaim `
     --plan reclaim-plan.json `
     --writers-stopped
   ```

## Предупреждения

`DELETE` и `VACUUM FULL` не являются мгновенным rollback: они могут держать
длинные блокировки, создать WAL/временные копии и временно потребовать
свободное место порядка размера таблицы (для `pg_repack` — до двух размеров).
Unknown dependency блокирует удаление соответствующей generation metadata;
evidence target с неизвестным FK сохраняется, остальные доказанно GC-owned
evidence могут быть очищены. При нехватке места команда блокируется. Не следует
освобождать место удалением accepted physical facts, `stock_ledger_entry` или
архивов внешних действий.
