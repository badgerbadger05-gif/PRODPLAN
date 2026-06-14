# PRODPLAN Code Review - Codex

Дата: 2026-06-14  
Checkout: `/home/ivan/PRODPLAN/repo`  
Ветка: `feature/purchase-journal`  
Статус worktree на момент ревью: чистый, кроме уже существующего untracked `.docs/code_review_claude.md`

## Scope

Ревью проводилось без внесения изменений в код. Фокус:

- backend-интеграции с 1C;
- production-control и purchase-control;
- MRP reconciliation;
- frontend-контракты и пользовательские сценарии;
- миграции, runtime/deploy риски и тестовые пробелы.

В ревью были подключены три параллельных агента:

- backend / 1C / MRP;
- frontend / React / TypeScript contracts;
- migrations / deploy / runtime operations.

## High Priority

### 1. Production-control export routes bypass production-write guard

Файлы:

- `backend/app/routers/production_control.py:381`
- `backend/app/routers/production_control.py:431`
- `backend/app/routers/production_control.py:511`
- `backend/app/routers/production_control.py:600`
- `backend/app/services/one_c_export_common.py:71`

Проблема: роуты передают в сервисы:

```python
allow_production=bool(payload.allow_production) or not bool(payload.dry_run)
```

Из-за этого любой реальный экспорт с `dry_run=false` автоматически становится `allow_production=True`, даже если пользователь не дал явное разрешение на запись в production 1C.

Задуманный guard в `create_odata_client(..., require_demo_base=True)` фактически обходится на уровне API.

Затронутые операции:

- export manufactures to 1C;
- export piecework to 1C;
- export production orders to 1C;
- export material issues to 1C.

Impact: обычный запрос на выполнение экспорта может писать в реальную 1C без отдельного явного override.

Рекомендация: передавать только `allow_production=bool(payload.allow_production)`. Для real write в production требовать оба условия: `dry_run=false` и `allow_production=true`.

### 2. `/material-issues/{issue_id}/assembled` always permits production posting

Файлы:

- `backend/app/routers/production_control.py:610`
- `backend/app/routers/production_control.py:620`
- `backend/app/services/production_control_material_issues.py:1231`
- `backend/app/services/production_control_material_issues.py:1256`

Проблема: route принимает `AssembleMaterialIssuePayload`, где есть `allow_production`, но игнорирует его и всегда вызывает:

```python
allow_production=True
```

Дальше сервис создаёт 1C client с `require_demo_base=True`, но получает принудительный override, патчит документ и вызывает operational posting.

Impact: default request может провести stock transfer в production 1C без явного `allow_production`.

Рекомендация: передавать `payload.allow_production`; для production-проводки требовать явное подтверждение и покрыть это router/service тестом.

### 3. Purchase export can duplicate supplier orders after partial exports

Файлы:

- `backend/app/services/one_c_purchase_order_export.py:75`
- `backend/app/services/one_c_purchase_order_export.py:100`
- `backend/app/services/one_c_purchase_order_export.py:148`
- `backend/app/services/one_c_purchase_order_export.py:240`

Проблема: `_collect_purchase_groups()` строит группы только из текущего набора `PlannedPurchase`, фильтрует по `purchase_ids`, но перед группировкой не исключает строки, которые уже имеют успешный `SyncLink`.

Номера заказов назначаются по текущему sorted supplier set. Reuse идёт по generated number + supplier + comment, а не по уже связанным `purchase_id`.

Сценарий риска:

1. Пользователь экспортирует только supplier B, он получает номер `...-1`.
2. Позже пользователь экспортирует весь run с suppliers A+B.
3. Supplier A сталкивается с занятым `...-1` и берёт следующий номер.
4. Supplier B может получить новый номер и второй заказ для тех же planned purchases.

Impact: дубли заказов поставщику в 1C по одним и тем же MRP purchase rows.

Рекомендация: перед экспортом исключать `planned_purchase`, уже имеющие successful `SyncLink`, или строить reuse по `source_id`/`target_ref_key`, а не только по номеру документа.

### 4. Hidden purchase selections survive filter/page reloads and are exported

Файлы:

- `frontend-erp-shell/src/ui/pages/PurchaseControlPage.tsx:48`
- `frontend-erp-shell/src/ui/pages/PurchaseControlPage.tsx:93`
- `frontend-erp-shell/src/ui/pages/PurchaseControlPage.tsx:154`

Проблема: `selectedPurchaseIds` живёт независимо от текущих `rows`. `load()` заменяет строки после смены фильтра/страницы, но не очищает и не пересекает selection с видимыми строками. `orderTo1C()` отправляет все выбранные ids.

Impact: пользователь может выбрать строки "К заказу", сменить фильтр/страницу и нажать "Заказать в 1C"; в 1C уйдут позиции, которых уже нет на экране.

Рекомендация: при reload/filter/page change чистить selection или сохранять только ids из нового набора `rows`. Перед POST дополнительно пересекать selection с текущими `toOrderRows`.

### 5. Purchase table row keys are not globally unique

Файлы:

- `backend/app/services/purchase_control_journal.py:197`
- `frontend-erp-shell/src/domain/purchaseControl.ts:11`
- `frontend-erp-shell/src/ui/pages/PurchaseControlPage.tsx:73`
- `frontend-erp-shell/src/ui/pages/purchase-control/PurchaseOrdersTable.tsx:42`

Проблема: backend отдаёт для supplier-order rows:

```python
"row_key": f"line:{int(line.item_id)}"
```

`SupplierOrderItem.item_id` здесь является id строки таблицы, но имя поля легко спутать с номенклатурой. Если фактически в данных/контракте появится неуникальный key, React начнёт переиспользовать строки некорректно, а `activeRow = rows.find(row_key)` может открыть не ту строку.

Impact: неверное выделение, неверная detail pane, React key collision.

Рекомендация: сделать key явно стабильным и уникальным по доменной сущности, например `line:{order_id}:{line_id}` или `supplier-order-item:{line.item_id}`. Добавить тест на две строки с одинаковой номенклатурой в разных заказах.

## Medium Priority

### 6. MRP reconciliation can rewrite fixed snapshot using current BOM/spec

Файлы:

- `backend/app/services/mrp_reconciliation.py:393`
- `backend/app/services/mrp_reconciliation.py:408`
- `backend/app/services/mrp_reconciliation.py:455`
- `backend/app/services/mrp_reconciliation.py:623`

Проблема: `_current_snapshot_gross_by_item()` берёт root requirements из snapshot, но затем взрывает потребности через текущие `DefaultSpecification` и `SpecComponent`. Требования, которых текущий BOM больше не достигает, получают gross 0.

Impact: после изменения BOM/default specification reconcile уже зафиксированного snapshot может создать новые production/purchase needs и занулить старые компоненты. Это спорит с идеей frozen snapshot.

Рекомендация: явно определить семантику. Если snapshot должен быть frozen, reconcile должен использовать сохранённую структуру snapshot или не пересчитывать BOM. Если нужен "актуализировать по текущему BOM", это должно быть отдельным действием с понятным названием и audit trail.

### 7. API startup mutates schema outside Alembic

Файл:

- `backend/app/main.py:23`

Проблема:

```python
Base.metadata.create_all(bind=engine)
```

на старте API может создать отсутствующие таблицы напрямую, минуя Alembic.

Impact: плохой deploy может выглядеть "живым", но schema окажется без нужных миграционных индексов, constraints, backfill и корректного `alembic_version`.

Рекомендация: убрать `create_all` из production startup. Для dev/test оставить отдельную команду или feature flag. На старте production лучше fail fast при migration mismatch.

### 8. `docker-compose.test.yml` exposes Postgres with known fallback password

Файлы:

- `docker-compose.test.yml:11`
- `docker-compose.test.yml:17`
- `docker-compose.test.yml:38`

Проблема: fallback password:

```yaml
POSTGRES_PASSWORD: ${PRODPLAN_POSTGRES_PASSWORD:-prodplan_password_change_me}
```

и Postgres публикуется на host port.

Impact: если `.env` отсутствует или слабый, а host доступен из LAN, БД доступна напрямую с предсказуемыми credential. Backend port также опубликован.

Рекомендация: не публиковать Postgres наружу по умолчанию или bind to localhost; требовать non-default password; добавить deploy checklist для `.env`.

### 9. Purchase-control API has no auth/access dependency

Файлы:

- `backend/app/main.py:49`
- `backend/app/routers/purchase_control.py:14`
- `backend/app/routers/purchase_control.py:56`
- `backend/app/routers/purchase_control.py:67`

Проблема: router смонтирован без auth/access dependency, endpoints зависят только от `get_db`.

Impact: любой, кто достучался до backend port, может читать supplier names, order state, refs, amounts и pending MRP purchase needs.

Рекомендация: если это осознанно закрытая LAN-модель, зафиксировать это в deploy docs и закрыть сетевой периметр. Практичнее: хотя бы простой internal API key / reverse-proxy auth для опасных и чувствительных endpoints.

### 10. Period plan action ignores visible journal filters

Файлы:

- `frontend-erp-shell/src/ui/pages/PeriodPlanPage.tsx:742`
- `frontend-erp-shell/src/ui/pages/PeriodPlanPage.tsx:942`
- `frontend-erp-shell/src/ui/pages/PeriodPlanPage.tsx:1540`

Проблема: client-side фильтры применяются в `filteredJournalRows`, но действие "Создать заказы производства" строит `reqIds` из raw `journal.rows`.

Impact: пользователь фильтрует видимые строки, но действие может захватить скрытые production rows.

Рекомендация: action должен использовать тот же filtered dataset, который видит пользователь, либо UI должен явно показывать, что операция применяется ко всем строкам.

### 11. Purchase-control deep links with `?search=` only apply on initial mount

Файлы:

- `frontend-erp-shell/src/ui/pages/PurchaseControlPage.tsx:43`
- `frontend-erp-shell/src/ui/pages/PurchaseControlPage.tsx:58`
- `frontend-erp-shell/src/ui/pages/PurchaseControlPage.tsx:108`
- `frontend-erp-shell/src/ui/pages/PeriodPlanPage.tsx:1047`

Проблема: `focusSearch` инициализирует `filters.search`, но subsequent route/search-param changes не обновляют `filtersRef` и не перезагружают журнал. Если `/purchase-control` уже mounted, переход по новой ссылке может оставить старый поиск.

Impact: пользователь приходит из period plan в purchase journal и видит stale search/filter state.

Рекомендация: добавить effect на `searchParams`/`focusSearch` и синхронизировать filters + reload.

## Test And Coverage Gaps

### Missing HTTP/router tests for purchase-control

Файл:

- `tests/services/test_purchase_control.py`

Сейчас покрыты сервисы `list_journal`, `get_order_card`, `list_filters`, но нет `TestClient` тестов на `/api/v1/purchase-control/*`.

Что стоит покрыть:

- mount path `/api/v1/purchase-control/orders`;
- query validation: `limit`, `offset`, bool parsing;
- 404 mapping for order card;
- `include_to_order`, `line_status`, `state`;
- поведение auth/access policy, если она будет добавлена.

### Missing tests for production write guard

Нужны router-level тесты на:

- `dry_run=false`, `allow_production=false` against non-demo base must return 403;
- `dry_run=false`, `allow_production=true` allowed;
- `/material-issues/{issue_id}/assembled` respects payload.

### Missing tests for partial purchase export idempotency

Нужен сценарий:

1. export selected supplier/purchase subset;
2. export full run;
3. assert existing successful `SyncLink` rows are not duplicated and no second 1C order is created for same `purchase_id`.

## Verification Performed

Commands run:

```bash
git status --short
git branch --show-current
npm run build
npm run lint
.venv/bin/pytest -q tests/services/test_purchase_control.py tests/services/test_one_c_purchase_order_export.py
../.venv/bin/alembic heads
python -m compileall backend/app
```

Results:

- `npm run build`: passed.
- `npm run lint`: passed with one existing warning in `RootProductFilterDialog.tsx`.
- targeted backend tests: `11 passed`, warnings only.
- `alembic heads`: passed, single head `20260611_01`.
- `python -m compileall backend/app`: passed.
- `alembic current`: not verified; local DB connection failed.

## Notes

One initial quick regex check suggested two Alembic heads, but the real Alembic command showed one head. Treat the regex result as false positive.

The highest-risk fixes are the production 1C guard issues. They are small code changes with high safety value and should be fixed before any further real 1C operations through these endpoints.
