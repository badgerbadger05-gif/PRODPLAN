# Doctype — единый контракт страниц фронтенда

Статус: НОРМАТИВНЫЙ (обязательный к соблюдению). Версия 1.

## 0. Зачем это существует

Сегодня каждая страница (`ProductionControlPage`, `PurchaseControlPage`, `TransferRequestsPage`, DBR-страницы…) пишется **с нуля**: своё состояние `rows/activeId/detail/filters/offset/total/loading/error/message`, свой эффект загрузки, своя сборка `DocumentWindow` + таблица + деталь + `StatusBar`, свои мапы лейблов, свои действия. Результат — **каждая страница разная**, дублируется ~200 строк однотипной механики, поведение (пагинация, сортировка, ошибки, горячие клавиши) расходится от экрана к экрану, тестировать нечего.

Текущий `src/ui/tableDoctype.ts` описывает **только колонки таблицы**. Этого мало.

**Doctype** — это декларативное описание ЦЕЛОЙ страницы-справочника/журнала (аналог Frappe DocType, но на нашем React + services-слое). Из одного объекта-определения рендерится вся страница по фиксированному скелету. Цель: **новый экран = один Doctype-объект + сервис данных, без ручной механики страницы.**

Это же несущая абстракция для «карточек элементов» из ledger-проекта (витрина по `pool_key`): карточка = Doctype.

## 1. Анатомия страницы (ФИКСИРОВАНА — не изобретать заново)

Каждая list-страница состоит строго из этих слоёв, сверху вниз:

```
DocumentWindow                 // заголовок + подзаголовок + хоткеи (есть)
  ├─ CommandBar                // действия (кнопки): глобальные + над выбором
  ├─ FilterBar                 // фильтры: поиск, селекты статусов/складов, даты
  ├─ DoctypeTable              // таблица по колонкам doctype + сортировка + выбор строк
  ├─ DetailPane (optional)     // деталь активной строки / форма-карточка
  └─ StatusBar (footer)        // счётчики, пагинация, сообщение/ошибка (есть)
```

Слои `CommandBar`, `FilterBar`, `DoctypeTable`, `DetailPane` — ОБЩИЕ компоненты, управляемые метаданными Doctype. Страница НЕ верстает их руками.

## 2. Определение Doctype (схема)

Файл `src/ui/pages/<area>/<name>Doctype.ts`. Определение — чистые данные (без JSX, без хуков):

```ts
export type Doctype<Row, Filters, Detail = never> = {
  meta: {
    name: string            // машинное имя, напр. 'production_order'
    title: string           // «Журнал заказов на производство»
    subtitle: string
    hotkeys?: string        // строка подсказки хоткеев
    idField: keyof Row      // уникальный ключ строки (напр. 'product_id')
  }

  // ИСТОЧНИК ДАННЫХ — только через services-слой, никаких inline api()
  dataSource: {
    list(params: ListParams<Filters>, signal?: AbortSignal): Promise<ListResult<Row>>
    detail?(id: RowId, signal?: AbortSignal): Promise<Detail>
  }

  // КОЛОНКИ — расширение текущего TableColumnDoctype: + рендер/значение
  columns: Array<DoctypeColumn<Row>>

  // ФИЛЬТРЫ — декларативно, а не хардкод JSX
  filters?: Array<FilterDef<Filters>>   // search | select | dateRange | toggle

  // ДЕЙСТВИЯ — декларативно; enabled/visible как функции от выбора
  actions?: Array<ActionDef<Row>>       // global | selection-scoped | row-scoped

  // ДЕТАЛЬ/КАРТОЧКА — секции полей; используется DetailPane/FormRenderer
  detail?: DetailLayout<Detail | Row>

  // ПРАВА (техдолг, см. FRONTEND-TECHDEBT) — роли, при которых действие/экран доступны
  permissions?: { view?: Role[]; actions?: Record<string, Role[]> }
}
```

### 2.1. Колонка (расширяет текущий `TableColumnDoctype`)

```ts
export type DoctypeColumn<Row> = TableColumnDoctype & {
  // значение ячейки из строки; по умолчанию row[key]
  value?: (row: Row) => unknown
  // тип форматирования — определяет выравнивание и формат ПО УМОЛЧАНИЮ
  type?: FieldType     // 'text'|'number'|'qty'|'money'|'date'|'datetime'|'enum'|'ref'|'status'|'bool'|'select-checkbox'
  // для enum/status — карта код→лейбл (+ цвет для status)
  options?: Record<string, { label: string; tone?: Tone }>
  // кастомный рендер — ТОЛЬКО когда type недостаточно (редко!)
  render?: (row: Row) => ReactNode
}
```

Существующий `productionOrderColumns` мигрирует сюда: `key/title/width/align/sortable` остаются, добавляется `type` и (где нужно) `value`/`options`. `tableColumnStyle`, `tableMinWidth`, `sortGlyph` переиспользуются как есть.

### 2.2. Фильтры

```ts
type FilterDef<F> =
  | { kind: 'search';   field: keyof F; placeholder?: string; debounceMs?: number }
  | { kind: 'select';   field: keyof F; label: string; options: {value:string;label:string}[]; allowEmpty?: boolean }
  | { kind: 'dateRange';fieldFrom: keyof F; fieldTo: keyof F; label: string }
  | { kind: 'toggle';   field: keyof F; label: string }
```

Фильтры сериализуются в `ListParams.filters` и уходят в `dataSource.list`. Дебаунс поиска, сброс `offset` при смене фильтра — обеспечивает `useDoctypeList` (см. §4), не страница.

### 2.3. Действия

```ts
type ActionDef<Row> = {
  key: string
  label: string
  scope: 'global' | 'selection' | 'row'
  tone?: 'primary'|'default'|'danger'
  enabled?: (ctx: ActionCtx<Row>) => boolean   // напр. selection.length>0 && !some(exported)
  confirm?: string                             // текст подтверждения (window.confirm)
  run(ctx: ActionCtx<Row>): Promise<ActionResult>  // вызывает сервис, возвращает message/reload
}
```

`ActionResult` стандартизирован: `{ message?: string; error?: string; reload?: boolean; open?: DialogRequest }`. Диалоги (produce, chain-close, warehouse-picker) объявляются через `open` и рендерятся общим `DialogHost`, а не инлайном в странице.

### 2.4. Деталь / карточка (form)

```ts
type DetailLayout<T> = {
  sections: Array<{
    title?: string
    fields: Array<{ key: keyof T; label: string; type?: FieldType; options?: ...; span?: 1|2 }>
    // вложенная таблица (напр. состав/материалы) — тот же DoctypeColumn
    table?: { rows: (t:T)=>any[]; columns: DoctypeColumn<any>[] }
  }>
}
```

Это и есть «полноценная карточка элемента»: секции полей + вложенные таблицы, отрендеренные единообразно `FormRenderer`.

## 3. Типы полей и форматирование (ЕДИНЫЕ ПРАВИЛА)

`type` колонки/поля задаёт выравнивание и формат ПО УМОЛЧАНИЮ. Форматирование — ТОЛЬКО через `src/lib/format.ts` (`qty`, `dateRu`, деньги…). Запрещено форматировать числа/даты вручную в странице.

| type | выравнивание | формат | источник |
|---|---|---|---|
| text | left | как есть | — |
| number | right | группировка | format.number |
| qty | right | qty() | format.qty |
| money | right | 2 знака + валюта | format.money |
| date | left | dateRu | format.dateRu |
| datetime | left | dd.mm hh:mm | format.dateTimeRu |
| enum/status | left | options[code].label (+ tone-бейдж) | doctype.options |
| ref | left | name || ref || '—' | стандартный refLabel |
| bool | center | ✓ / — | — |
| select-checkbox | center | чекбокс выбора строки | DoctypeTable |

Мапы лейблов (`transferStatusLabels`, `coverageLabels`…) переезжают в `options` соответствующей колонки/поля Doctype — не как отдельные объекты в странице.

## 4. Рантайм: общий хук и компоненты

Механику владеет ОДИН хук — страница его только вызывает:

```ts
const dt = useDoctypeList(productionOrderDoctype)
// dt даёт: rows, activeRow, detail, filters+setFilter, sort+setSort,
//          selection+toggle, paging {offset,total,next,prev}, loading, error, message,
//          runAction(key), reload()
```

`useDoctypeList` инкапсулирует то, что сейчас копипастится: состояние, `AbortController`, дебаунс поиска, сброс offset при фильтре, загрузку детали активной строки, обработку `ApiError` в `error`, `message` от действий. **Ни одна страница больше не держит это руками.**

Общие компоненты (управляются метаданными + `dt`): `CommandBar`, `FilterBar`, `DoctypeTable`, `DetailPane`/`FormRenderer`, `DialogHost`. `DocumentWindow` и `StatusBar` уже есть — используются как есть.

Идеальная страница после миграции:

```tsx
export function ProductionControlPage() {
  const dt = useDoctypeList(productionOrderDoctype)
  return <DoctypePage dt={dt} doctype={productionOrderDoctype} />
}
```

`DoctypePage` собирает фиксированный скелет §1 из метаданных. Кастом нужен — прокидывается слотами (`renderExtraToolbar`, `renderAside`), а не форком всей страницы.

## 5. Файловая структура и именование (ОБЯЗАТЕЛЬНО)

```
src/domain/<name>.ts          // чистые типы Row/Detail/Filters + enum-лейблы (уже есть паттерн)
src/services/<name>.ts        // ВСЕ вызовы api<> по этому домену (уже есть паттерн; см. productionControl.ts)
src/ui/pages/<area>/<name>Doctype.ts   // объект Doctype (данные, без JSX)
src/ui/pages/<Name>Page.tsx   // тонкая обёртка над <DoctypePage>
src/ui/doctype/               // ОБЩИЙ рантайм: useDoctypeList, DoctypePage, CommandBar, FilterBar, DoctypeTable, FormRenderer, DialogHost, fieldFormat
```

## 6. Жёсткие правила (MUST / MUST NOT)

- MUST: каждый список/журнал/справочник определяется как `Doctype`. Новых страниц «с нуля» не создаём.
- MUST: данные — только через `src/services/*` (сервисная функция на эндпоинт). Инлайновые `api()`/`fetch()` в странице ЗАПРЕЩЕНЫ (см. долг «productionControl уже вынесен»).
- MUST: колонки — только через `DoctypeColumn`; форматирование — только через `lib/format` по `type`.
- MUST: состояние загрузки/пагинации/ошибок/сообщений — только через `useDoctypeList`. Дублировать `rows/loading/error/offset` в странице ЗАПРЕЩЕНО.
- MUST: действия — декларативные `ActionDef`; диалоги — через `DialogHost`. Инлайновые кнопки с бизнес-логикой в JSX ЗАПРЕЩЕНЫ.
- MUST: лейблы enum/status — в `options` doctype, не отдельными мапами в странице.
- MUST NOT: форк общего скелета ради мелкой кастомизации — использовать слоты.
- MUST: у каждого Doctype заполнен `permissions` (даже если пока `view: [ALL]`) — задел под роли (см. FRONTEND-TECHDEBT).
- SHOULD: на каждый Doctype — характеристический тест (рендер + один флоу), по образцу `ProductionControlPage.test.tsx`.

## 7. Рецепт миграции существующей страницы

1. Вынести типы в `domain/<name>.ts`, вызовы — в `services/<name>.ts` (если ещё не вынесены).
2. Описать `Doctype`: meta, columns (из существующего `*Doctype.ts` + `type`), filters, actions, detail.
3. Заменить ручное состояние на `useDoctypeList(doctype)`.
4. Заменить верстку на `<DoctypePage>` (+ слоты для реального кастома).
5. Перенести enum-мапы в `options`. Удалить дубли механики.
6. Добавить/сохранить характеристический тест (поведение не меняется).

Порядок миграции по стоимости: сперва простые (`TransferRequestsPage`, `PurchaseControlPage`) — обкатать рантайм; god-компоненты (`PeriodPlanPage`, `ProductionControlPage`) — после стабилизации `useDoctypeList`/`DoctypePage`.

## 8. Дорожная карта внедрения (инкрементально, на существующем `tableDoctype`)

1. Расширить `tableDoctype` → `DoctypeColumn` (+ `type`, `value`, `options`, `render`) и `fieldFormat` по §3. Обратная совместимость: старые `*Doctype.ts` продолжают работать.
2. Написать `useDoctypeList` + `DoctypeTable` (колонки + сортировка + выбор) — покрыть текущей механикой одной простой страницы.
3. Добавить `FilterBar`, `CommandBar`, `StatusBar`-интеграцию, `DialogHost`.
4. `FormRenderer`/`DetailPane` для карточек.
5. `permissions` + гейт по ролям (совместно с бэкендом — см. FRONTEND-TECHDEBT).

Каждый шаг — под тестами; миграция страниц — по одной, поведение сохраняется.

---
Связано: карточка элемента ledger-проекта = Doctype (витрина по `pool_key`); техдолг фронта и роли — `FRONTEND-TECHDEBT.md`.
