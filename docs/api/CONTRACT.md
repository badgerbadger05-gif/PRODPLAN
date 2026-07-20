# PRODPLAN API — контракт (backend ↔ frontend)

Статус: НОРМАТИВНЫЙ. Единственный писатель — backend (Claude). Изменения версионируются и явно анонсируются; фронт (Codex) не меняет контракт молча, а оформляет расхождения отдельно. Связано: `frontend-erp-shell/docs/DOCTYPE.md`, `frontend-erp-shell/docs/FRONTEND-TECHDEBT.md`.

## 0. Источник истины по эндпоинтам

`docs/api/openapi.json` — **авто-сгенерирован** из FastAPI (`app.openapi()`), OpenAPI 3.1, 183 пути / 99 схем. Это машинная истина по request/response каждого эндпоинта; регенерируется backend'ом при изменениях. Фронт **генерирует TS-типы из него** (`npx openapi-typescript docs/api/openapi.json -o src/lib/apiTypes.ts`) и строит на моках, пока backend дописывается. Ручные типы, дублирующие openapi, запрещены.

Этот документ фиксирует то, чего в openapi нет: сквозные конвенции, модель авторизации/прав, правила владения, потребление.

## 1. Транспорт и базовые конвенции

- **База:** всё под `/api`; прикладные эндпоинты — `/api/v1/...`. Фронтовый клиент — единственная точка (`src/lib/api.ts`, `api<T>(path, init, signal)`); `path` даётся БЕЗ `/api` (клиент добавляет префикс).
- **Формат:** JSON, UTF-8. Числа-количества — number; даты — ISO (`YYYY-MM-DD`) или datetime ISO; денормализованные лейблы приходят готовыми где применимо.
- **Ошибки:** FastAPI `HTTPException` → `{ "detail": ... }`. `detail` — строка ИЛИ структурированный объект (напр. 409 при дефиците несёт `deficit_lines`). Клиент бросает `ApiError { status: number, detail: string | object }`. Фронт различает ошибки по `status` + форме `detail`, а не по тексту.
  - Коды: `400` валидация/предусловие; `404` не найдено; `409` конфликт/дефицит (структурированный detail); `422` FastAPI-валидация тела; `500` внутренняя.
- **Пагинация (списки):** query `limit` (деф. 100), `offset` (деф. 0); ответ `{ rows: T[], total: number, limit: number, offset: number, ...доменные_поля }` (напр. журнал заказов добавляет `latest_run_id`). Сортировка — query `sort_by`, `sort_dir` (`asc|desc`) где поддерживается (см. `sortable` колонки).
- **Enum'ы** — строковые коды; человеческие лейблы НЕ в API, а на фронте в `Doctype.options` / `domain/*` (см. DOCTYPE §3). Список допустимых кодов — в схемах openapi.
- **Идемпотентность мутаций:**
  - существующие экспорты в 1С идемпотентны через `sync_link` (повторный экспорт не создаёт дубль документа 1С);
  - для НОВЫХ мутаций-предложений вводится заголовок `Idempotency-Key: <uuid>` — backend хранит результат по ключу; повтор с тем же ключом возвращает тот же результат, не создаёт второе покрытие. Фронт генерит ключ на действие и повторяет его при ретрае.
- **Reconcile/ledger — семантика (важно для фронта):** пересчёт леджера — это **цикл на всю каноническую область**, а не пер-план вызов. Публичный триггер — `POST /api/v1/plan/reconcile` (без run_ids). Фронт НЕ запускает частичный пересчёт одного плана. `executed_qty`/проекции — производные (только для чтения), их нельзя редактировать с фронта. Подробности модели — `mrp-ledger-blueprint-v2.md`.

## 2. Авторизация и права (backend задаёт, фронт гейтит, сервер проверяет)

Сейчас авторизации НЕТ (см. FRONTEND-TECHDEBT P0/P1). Целевой контракт, который реализует backend и на который строит фронт:

### 2.1. Аутентификация
- `POST /api/v1/auth/login` `{login, password}` → `{token, user}` (или httpOnly-cookie сессии).
- `GET /api/v1/auth/me` → `{ id, name, roles: Role[], permissions: string[] }`.
- `POST /api/v1/auth/logout`.
- Клиент шлёт токен в каждом запросе (заголовок `Authorization: Bearer <token>` или cookie). `401` → фронт редиректит на логин. **Заголовок ставит HTTP-клиент один раз, не страницы.**

### 2.2. Роли (первичный набор — уточняется с владельцем)
`admin` · `planner` (планировщик) · `buyer` (закупщик) · `shopfloor` (кладовщик/цех) · `viewer` (наблюдатель).

### 2.3. Разрешения (permissions) — гранулярнее ролей
Строковые коды, приходят в `/me.permissions`. Особо выделены **записи в 1С** (запись в источник правды):
- `plan.run`, `plan.reconcile`, `plan.snapshot.refreeze`
- `purchase.propose`, `purchase.export_1c`   ← экспорт заказа поставщику в 1С
- `production.propose`, `production.produce`, `production.post_1c` ← проведение СборкаЗапасов
- `material_issue.assemble_post_1c`           ← проведение ПеремещениеЗапасов
- `piecework.export_post_1c`                  ← проведение СдельныйНаряд
- `spec.writeback_1c`
- `*.view` на разделы.

### 2.4. Правило гейтинга
- Фронт гейтит **экраны** (навигация/роутинг) и **действия** через `Doctype.permissions` (`view: Role[]|perm`, `actions: {key: perm}`). Кнопка без права — скрыта/заблокирована.
- **Сервер ОБЯЗАН проверять права независимо от UI.** Фронт-гейт — это UX, не безопасность. Каждый мутирующий эндпоинт проверяет соответствующий permission и возвращает `403` при отказе.
- `403` в контракте: `{ detail: "forbidden", required: "<permission>" }`.

## 3. Владение и версионирование

- Контракт (этот файл + openapi.json + permission-модель) — **владеет backend**. Любое изменение: обновить openapi.json (регенерацией) + отметить в CHANGELOG-секции ниже + анонсировать.
- `/api/v1` — стабильный контур; **ломающие изменения** (переименование/удаление поля, смена формы) — только через новый путь/версию или явное согласование, не молча.
- Backend и frontend живут в непересекающихся директориях (`backend/` vs `frontend-erp-shell/`) — общих файлов в параллель нет. Интеграционная ветка мержит после тестов.

## 4. Как фронт потребляет (Codex)

1. `openapi-typescript docs/api/openapi.json → src/lib/apiTypes.ts` — типы request/response.
2. Сервисы (`src/services/*`) типизируются этими типами; страницы данные берут только через сервисы (DOCTYPE MUST).
3. Пока backend дописывает эндпоинт — мок на границе сервиса (тип известен из openapi/этого контракта), фронт не блокируется.
4. Экраны строятся как Doctype (см. DOCTYPE.md) — рантайм + определения; consistency и права падают из контракта.

## 5. Карта разделов (endpoint-группы → экраны)

| Раздел (префикс) | Назначение | Doctype-экран |
|---|---|---|
| `/api/v1/production-control/*` | журнал заказов на производство, материалы, выдача, проведение | production_order (пример §6) |
| `/api/v1/purchase-control/*` | журнал закупок | purchase_order |
| `/api/v1/plan/*` (56 эндпоинтов) | планы, прогоны, результаты MRP, reconcile, **будущие ledger-экраны** | plan_run / mrp_result / (проводки, происхождение, сверки — по мере backend) |
| `/api/v1/specification/*` | спецификации/BOM, дерево, качество | specification |
| `/api/v1/dbr/*` | DBR (барабан/питатель/сигналы) | dbr_* |
| `/api/v1/resources/*`, `/stages` | участки/этапы/виды производства | resources |
| `/api/v1/nomenclature|items|units|employees/*` | справочники (карточки элементов) | item_card (витрина по pool_key — ledger) |
| `/api/v1/sync/*`, `/odata/*` | синхронизация с 1С (read-only) | sync |

Полный список — `openapi.json`. Ledger-специфичные экраны (проводки/происхождение расчёта/сверки) добавятся в контракт по мере реализации backend-леджера (Фазы 3–6, blueprint-v2) — они НЕ блокируют старт фронта: Doctype-рантайм и миграция существующих экранов от них не зависят.

## 6. Пример: Doctype ↔ endpoint (production_order)

Показывает, как экран отображается на контракт (Codex делает так для остальных по DOCTYPE.md).

```ts
// data: services/productionControl.ts → GET /api/v1/production-control/orders?limit&offset&status&search&sort_by&sort_dir
//       → { rows: OrderRow[], total, limit, offset, latest_run_id }
export const productionOrderDoctype: Doctype<OrderRow, ProductionFilters, OrderDetail> = {
  meta: { name: 'production_order', title: 'Журнал заказов на производство',
          subtitle: '…', idField: 'product_id' },
  dataSource: {
    list:   listProductionOrders,                 // service
    detail: (id) => getOrderMaterials(id, false), // GET …/orders/{id}/materials
  },
  columns: [
    { key: 'select', type: 'select-checkbox' },
    { key: 'order',  title: 'Заказ', type: 'text' },
    { key: 'item',   title: 'Деталь', type: 'text', grow: true },
    { key: 'quantity', title: 'Кол-во', type: 'qty' },
    { key: 'planned_start_date', title: 'План', type: 'date', sortable: true },
    { key: 'status', title: 'Статус', type: 'status',
      options: { assembled:{label:'Собран',tone:'ok'}, shortage:{label:'Дефицит',tone:'warn'}, /*…*/ } },
    { key: 'coverage', title: 'Обеспечение', type: 'enum', options: coverageLabels },
  ],
  filters: [
    { kind:'search', field:'search' },
    { kind:'select', field:'status', label:'Статус', options: statusFilterOptions, allowEmpty:true },
  ],
  actions: [
    { key:'export_1c', label:'Запустить в 1С', scope:'selection', tone:'primary',
      enabled: c => c.selection.length>0 && c.selection.every(r=>r.coverage_status==='assembled'),
      run: exportMaterialIssuesTo1C /* POST …/material-issues/export-to-1c */ },
    { key:'produce', label:'Произвести', scope:'selection', open:{ dialog:'produce' } },
    { key:'delete', label:'Удалить', scope:'selection', tone:'danger', confirm:'Удалить локальный заказ?',
      enabled: c => c.selection.length===1 && !c.selection[0].order_ref1c, run: deleteProductionOrder },
  ],
  detail: { sections: [{ title:'Материалы', table: { rows: d=>d.components, columns: materialColumns } }] },
  permissions: { view:['planner','shopfloor','admin'],
    actions:{ export_1c:'material_issue.assemble_post_1c', produce:'production.produce', delete:'production.propose' } },
}
```

## CHANGELOG
- 2026-07-20 v2: удалён недельный выпуск, ввод факта и закрытие дня; openapi.json сокращён до 183 путей / 99 схем.
- 2026-07-20 v1: первичный контракт — openapi.json (187 путей), сквозные конвенции, целевая модель авторизации/прав, правила владения, пример production_order.
