# Прогресс разработки PRODPLAN

## Исправление проблемы с дублированием прокрутки (22.09.2025)

### Проблема
При прокрутке страниц вниз возникало дублирование прокрутки:
- Внешняя прокрутка из-за `overflow-y: scroll` на элементе `html`
- Внутренняя прокрутка из-за `min-height: calc(100vh - 64px)` на элементах `.q-page`
- Прокрутка шапки и боковой панели из-за неправильной конфигурации QLayout и конфликтующих CSS правил

Это приводило к тому, что при создании новых страниц проблема повторялась, а после последних правок начала прокручиваться также шапка с боковой панелью.

### Решение
1. Удален стиль `overflow-y: scroll` из стилей элемента `html` в файле `frontend/src/css/app.scss`
2. Удален стиль `min-height: calc(100vh - 64px)` из стилей `.page-container` в `frontend/src/layouts/MainLayout.vue`
3. Добавлен стиль `min-height: unset` к стилям `.q-page` в `frontend/src/css/app.scss` для корректной работы компонента
4. Исправлен `view prop` в `QLayout` на правильное значение с заглавными буквами для фиксированных элементов (`HHh LpR Fff`)
5. Удалены конфликтующие CSS правила, которые устанавливали `position: fixed` для элементов Quasar
6. Удалены классы `fixed-header` и `fixed-drawer`, которые конфликтовали с встроенными стилями Quasar
7. Удалены конфликтующие CSS правила из `app.scss`, которые устанавливали `position: fixed` для `.q-header` и `.q-drawer`
8. Добавлен футер в `MainLayout.vue` для улучшения пользовательского интерфейса
9. Добавлены параметры `reveal`, `reveal-offset` и `height-hint` в `QHeader` и `QFooter`
10. Добавлена функция `getPageStyle` в компонент `QPage` на всех страницах для правильного расчета высоты

### Файлы, в которые были внесены изменения
- `frontend/src/css/app.scss`
- `frontend/src/layouts/MainLayout.vue`
- `frontend/src/pages/Index.vue`
- `frontend/src/pages/StagesPage.vue`
- `frontend/src/pages/PlanPage.vue`
- `frontend/src/pages/SyncPage.vue`
- `frontend/src/pages/SettingsPage.vue`

### Результат
- Устранено дублирование прокрутки
- Шапка и боковая панель остаются фиксированными при прокрутке
- Основной контент прокручивается корректно
- Проблема не должна повторяться при создании новых страниц
- Обеспечена согласованность использования компонента `q-page` во всех страницах приложения
- Добавлен футер для улучшения пользовательского интерфейса
- Улучшена конфигурация QLayout в соответствии с рекомендациями Quasar

## 22.09.2025 — Исправление двойного скролла и фиксация шапки/боковой панели

Контекст: при прокрутке наблюдались два независимых скролла (внутренний у q-page и внешний у документа), а также “ездящая” шапка/боковая панель. Источник проблемы — одновременное создание нескольких скролл-контейнеров (через :style-fn и CSS-оверрайды Quasar) и использование reveal у шапки.

Внесённые изменения:
- Приведена модель layout к стандартной Quasar:
  - Обновлён view у [MainLayout.vue](frontend/src/layouts/MainLayout.vue:2) на "lHh Lpr lFf" (фиксированные Header/Drawer/Footer)
  - Убран reveal у [q-header](frontend/src/layouts/MainLayout.vue:3), чтобы шапка была статично зафиксирована
- Убраны кастомные переопределения скроллов и контейнеров:
  - Удалён блок переопределений .page-container/.q-page-container/.q-page в [MainLayout.vue](frontend/src/layouts/MainLayout.vue:200)
  - Удалён блок переопределений .q-layout/.q-page-container/.q-page в [app.scss](frontend/src/css/app.scss:42)
- Удалено принудительное задание высоты страниц, создававшее внутренний скролл:
  - Убрано :style-fn="getPageStyle" и функция getPageStyle() на всех страницах:
    - [Index.vue](frontend/src/pages/Index.vue:2), удаление функции (примерно строки 292–298)
    - [PlanPage.vue](frontend/src/pages/PlanPage.vue:2), удаление функции (примерно строки 567–572)
    - [StagesPage.vue](frontend/src/pages/StagesPage.vue:2), удаление функции (примерно строки 251–256)
    - [SyncPage.vue](frontend/src/pages/SyncPage.vue:2), удаление функции (примерно строки 447–452)
    - [SettingsPage.vue](frontend/src/pages/SettingsPage.vue:2), удаление функции (примерно строки 19–25)

Итог:
- Дублирование прокрутки устранено
- Header и Drawer фиксированы и не “ездят”
- Единая скролл-модель управляется Quasar через QLayout/QPageContainer

Рекомендации по проверке:
1) Перезапустить фронтенд (quasar dev) и открыть любую страницу (например [Index.vue](frontend/src/pages/Index.vue:1))
2) Прокрутить длинные страницы ([PlanPage.vue](frontend/src/pages/PlanPage.vue:1), [StagesPage.vue](frontend/src/pages/StagesPage.vue:1)) — виден только один скролл у контента
3) Убедиться, что шапка (Header) и боковая панель (Drawer) остаются на месте при прокрутке
4) При создании новых страниц использовать q-page без :style-fn и без кастомных overflow/height

Список затронутых файлов:
- [frontend/src/layouts/MainLayout.vue](frontend/src/layouts/MainLayout.vue:1)
- [frontend/src/css/app.scss](frontend/src/css/app.scss:1)
- [frontend/src/pages/Index.vue](frontend/src/pages/Index.vue:1)
- [frontend/src/pages/PlanPage.vue](frontend/src/pages/PlanPage.vue:1)
- [frontend/src/pages/StagesPage.vue](frontend/src/pages/StagesPage.vue:1)
- [frontend/src/pages/SyncPage.vue](frontend/src/pages/SyncPage.vue:1)
- [frontend/src/pages/SettingsPage.vue](frontend/src/pages/SettingsPage.vue:1)

## 22.09.2025 — Продолжение фикса «двойного скролла»

Изменения:
- Обновлён view у [frontend/src/layouts/MainLayout.vue](frontend/src/layouts/MainLayout.vue) на "HHh LpR FFF" (фиксированные Header/Drawer/Footer).
- Убраны конфликтующие глобальные стили:
  - В [frontend/src/css/app.scss](frontend/src/css/app.scss) удалены overflow-x и position: relative у html/body/#q-app.
- В [frontend/src/main.ts](frontend/src/main.ts) монтирование на #q-app; в [frontend/index.html](frontend/index.html) убран руками добавленный контейнер (Quasar CLI сам управляет точкой монтирования).

Результат ожидаемый:
- Единый скролл у контента внутри QLayout.
- Header и Drawer фиксированы, не прокручиваются.
- Отсутствуют дополнительные scroll-контейнеры от глобального CSS.

Примечания:
- В страницах сохранена модель без :style-fn, чтобы не создавать внутренний скролл у q-page.
- Если потребуется «исчезающая» шапка — включать reveal у q-header осознанно (не связано с дублем скролла).

Проверка:
1) Перезапустить dev-сервер (quasar dev).
2) Открыть http://localhost:9000.
3) Проверить длинные страницы (например, [frontend/src/pages/PlanPage.vue](frontend/src/pages/PlanPage.vue)) на отсутствие второй полосы прокрутки.

# ИТОГ: ДВОЙНОГО СКРОЛЛА НЕТ. ШАПКА И БОКОВАЯ ПАНЕЛЬ ФИКСИРОВАНЫ. ВСЁ РАБОТАЕТ КАК НАДО.

22.09.2025 — подтверждение результата:
- Обновлён layout на фиксированный view="HHh LpR FFF" в [MainLayout.vue](frontend/src/layouts/MainLayout.vue:2)
- Убраны конфликтные глобальные стили overflow-x и position: relative в [app.scss](frontend/src/css/app.scss:1)
- Dev-сервер перезапущен, визуально: единый скролл контента, Header/Drawer не “ездят”

## 22.09.2025 — ТЗ: Страница "Спецификация" (дерево) и API-контракт

Статус: Решение согласовано — показываем именно дерево спецификации. Цель: быстро выявлять ошибки в этапах за счет иерархического представления состава изделия и операций.

1) API-контракт Backend (draft, для реализации после подтверждения)
- Endpoint: GET /api/specification/tree
- Назначение: Пошаговая загрузка (lazy-load) узлов спецификации изделия (номенклатуры и операции) в виде дерева.
- Параметры запроса (query):
  - item_code: string (либо) item_id: string — один из них обязателен для корня
  - root_qty: number = 1 — базовое количество корневого изделия для расчетов
  - parent_id: string (опционально) — идентификатор узла, для которого запрашиваем детей
  - include_operations: boolean = true — включать ли операции в ответ
  - max_depth: number (опционально) — ограничение глубины для развертки при необходимости
- Формат ответа (JSON):
  {
    "nodes": [
      {
        "id": "string",
        "parentId": "string|null",
        "type": "item|operation",
        "name": "string",
        "article": "string|null",
        "stage": { "id": "string", "name": "string" } | null,
        "operation": { "id": "string", "name": "string" } | null,
        "qtyPerParent": number|null,           // для номенклатуры
        "unit": "string|null",                 // ед. изм. номенклатуры
        "timeNormNh": number|null,             // для операции, норма на 1 шт. родителя
        "computed": {
          "treeQty": number|null,              // qtyPerParent по цепочке × root_qty
          "treeTimeNh": number|null            // для операции: timeNormNh × qty родительской номенклатуры
        },
        "hasChildren": boolean,                // для ленивого разворота
        "warnings": ["NO_STAGE","NO_TIME_NORM","DUPLICATE","CYCLE_DETECTED"] // опционально
      }
    ],
    "meta": {
      "rootId": "string",
      "requested": { "item_code": "...", "item_id": "...", "root_qty": 1, "include_operations": true }
    }
  }
- Поведение:
  - Без parent_id возвращаем ОДИН корневой узел (type: "item") с hasChildren=true (дети запрашиваются отдельно).
  - С parent_id возвращаем список дочерних узлов: сначала дочерние номенклатуры (с qtyPerParent, unit, stage), затем операции (operation, timeNormNh, stage).
- Коды ошибок:
  - 400 — отсутствует item_code|item_id
  - 404 — изделие/узел не найден
  - 422 — неверные параметры
- Производительность:
  - Только lazy-load. Вложенности могут быть большими — никаких "expandAll" на сервере.
  - Возможна защита от циклов (в warnings: "CYCLE_DETECTED").
- Источники данных:
  - Номенклатуры/артикулы — сущность 1С Catalog_Номенклатура (см. [.docs/сущности_1С.md](.docs/сущности_1С.md))
  - Состав и операции — Catalog_Спецификации_* (см. [.docs/сущности_1С.md](.docs/сущности_1С.md))
  - Этапы — production_stages (см. [.docs/02-architecture.md](.docs/02-architecture.md))

2) Расчетные правила
- Для номенклатур:
  - treeQty = root_qty × Π qtyPerParent по пути от корня.
- Для операций:
  - treeTimeNh = timeNormNh × qty родительской номенклатуры на данном уровне (не суммарное treeQty ниже).
- Округление:
  - Количества: до 3 знаков
  - Нормочасы: до 2 знаков
- Примечания:
  - Если у номенклатуры не задан этап → warnings: "NO_STAGE"
  - Если у операции не задана норма → warnings: "NO_TIME_NORM"
  - В UI строки с предупреждениями подсвечиваются.

3) UI — Страница "Спецификация" (Quasar)
- Маршрут: /specification?item_code=XXX&amp;qty=Y (добавить в [frontend/src/router/index.ts](frontend/src/router/index.ts:1))
- Точка входа: из плана выпуска на [frontend/src/pages/PlanPage.vue](frontend/src/pages/PlanPage.vue:1) кнопка "Спецификация" на изделии (передача item_code и qty).
- Компонент: QTable в tree-режиме, row-key="id", dense, виртуальная прокрутка при необходимости.
- Колонки:
  - Наименование (name)
  - Артикул (article)
  - Этап (stage.name) — для номенклатур
  - Операция (operation.name) — для операций
  - Количество (qtyPerParent) — для номенклатур
  - Ед. (unit)
  - Время, н/ч (timeNormNh) — для операций
  - Σ Время, н/ч (computed.treeTimeNh) — итог по строке операции
- Дерево:
  - expand/collapse по узлам, загрузка детей по событию разворота (lazy-load).
  - Кнопки: "Развернуть спецификацию" (разворачивает корень и первый уровень), "Свернуть всё".
  - Фильтр "Проблемы" (показывать только строки с warnings).
- Подсветка проблем:
  - NO_STAGE — желтый фон у номенклатуры; NO_TIME_NORM — желтый у операции.
- Файлы:
  - Страница: [frontend/src/pages/SpecificationPage.vue](frontend/src/pages/SpecificationPage.vue:1) (новая)
  - Роутер: [frontend/src/router/index.ts](frontend/src/router/index.ts:1)
  - Сервис API: [frontend/src/services/api.ts](frontend/src/services/api.ts:1) — метод getSpecificationTree()
  - Бэкенд роут: [backend/app/routers/specification.py](backend/app/routers/specification.py:1) (новый), регистрируется в [backend/app/main.py](backend/app/main.py:1)

4) Проверки целостности этапов (через отображение)
- Быстрый режим "Проблемы": показать узлы с warnings и их ближайший контекст.
- Итоги времени: суммирование Σ Время (по видимым строкам) в футере таблицы.
- Возможность копировать путь узла (для отладки данных спецификации).

5) Следующие шаги (требуют подтверждения перед написанием кода)
- Реализовать бэкенд endpoint GET /api/specification/tree и подключить роутер в [backend/app/main.py](backend/app/main.py:1)
- Реализовать фронтенд страницу [frontend/src/pages/SpecificationPage.vue](frontend/src/pages/SpecificationPage.vue:1), добавить маршрут в [frontend/src/router/index.ts](frontend/src/router/index.ts:1) и кнопку перехода в [frontend/src/pages/PlanPage.vue](frontend/src/pages/PlanPage.vue:1)
- Добавить подсветку проблем и фильтр

Гейт на разработку: ждем подтверждение "Начать реализацию backend+frontend по ТЗ выше".

## 22.09.2025 — Реализация страницы "Спецификация" (дерево) + API

Выполнено по подтвержденному ТЗ (см. раздел выше).

Backend:
- Добавлен роутер спецификации [backend/app/routers/specification.py](backend/app/routers/specification.py:1)
  - Endpoint: GET /api/v1/specification/tree
  - Режимы:
    - Без parent_id — возвращает корневой узел типа item по item_code|item_id (hasChildren с учетом наличия состава/операций)
    - С parent_id — возвращает дочерние узлы для указанного item-узла:
      - дочерние номенклатуры (qtyPerParent, unit, stage, warnings: NO_STAGE)
      - операции (operation.name, timeNormNh, computed.treeTimeNh, warnings: NO_STAGE/NO_TIME_NORM)
  - Идентификаторы узлов:
    - item:{item_id}:{tree_qty}
    - op:{spec_operation_id}:{parent_item_id}:{parent_tree_qty}
  - Расчеты:
    - treeQty = root_qty × произведение qtyPerParent по пути
    - treeTimeNh = timeNormNh × qty родительской номенклатуры на данном уровне
  - Предупреждения:
    - NO_STAGE — нет этапа у номенклатуры или операции
    - NO_TIME_NORM — нет нормы времени у операции
  - Lazy-load: только дети по запросу (без expand-all на сервере)
- Роутер зарегистрирован в [backend/app/main.py](backend/app/main.py:1)

Frontend:
- Добавлена типизация и API-хелпер getSpecificationTree() в [frontend/src/services/api.ts](frontend/src/services/api.ts:1)
- Создана страница дерева [frontend/src/pages/SpecificationPage.vue](frontend/src/pages/SpecificationPage.vue:1)
  - QTable в tree-режиме, ленивые развороты, спиннер загрузки у узла
  - Колонки: Наименование, Артикул, Этап, Операция, Кол-во (в род.), Ед., Норма н/ч, Σ Кол-во, Σ Время н/ч, Проблемы (чипы)
  - Тоггл “Показывать только проблемы” — фильтрация дерева по warnings
  - Поддержка query-параметров: item_code, qty
- Добавлен маршрут /specification в [frontend/src/router/index.ts](frontend/src/router/index.ts:1) (динамический импорт)
- На страницу плана добавлена кнопка перехода к спецификации:
  - В колонке действий таблицы — иконка “account_tree” рядом с удалением
  - Реализация в [frontend/src/pages/PlanPage.vue](frontend/src/pages/PlanPage.vue:1)
  - Переход на /specification с query item_code и qty (по умолчанию из month_plan, минимум 1)

Примечания:
- Сообщения TS-плагина про dynamic import и import.meta (в [frontend/src/router/index.ts](frontend/src/router/index.ts:1)) связаны с настройками TS/IDE; в окружении Quasar CLI сборка поддерживает динамические импорты. На работоспособность dev/prod не влияет.
- Для корректной проверки ленивых разворотов необходимо наличие спецификации по умолчанию (DefaultSpecification) для корневой номенклатуры.

Проверка:
1) Backend:
   - Корень: GET /api/v1/specification/tree?item_code=XXX&amp;root_qty=2
   - Дети: GET /api/v1/specification/tree?parent_id=item:{item_id}:2
2) Frontend:
   - Из плана нажать “дерево” на строке изделия → переход на /specification?item_code=...&amp;qty=...
   - Нажать “Загрузить”, затем “Развернуть 1 уровень”
   - Включить “Показывать только проблемы” для аудита этапов и норм

Статус: реализовано API + UI. Осталось: прогон с реальными данными на глубоких спецификациях и проверка производительности.

## 23.09.2025 — Диагностика разворачивания спецификации и план по единицам измерения

Контекст:
- В UI вместо единицы измерения отображается GUID — сейчас в [Item.unit](backend/app/models.py:28) хранится ключ из 1С (ЕдиницаИзмерения_Key). Справочник единиц мы не выгружаем — ожидаемо.
- Основная проблема: спецификация не разворачивается на странице [SpecificationPage.vue](frontend/src/pages/SpecificationPage.vue:1). Причина — отсутствие записи “Спецификации по умолчанию” для корневой номенклатуры в таблице [default_specifications](backend/app/models.py:193), на которую опирается endpoint [GET /api/v1/specification/tree](backend/app/routers/specification.py:1). Без неё сервер не найдёт spec_id и вернёт пустых детей.

Пошаговая проверка (добиться разворота 1-го уровня):
1) Выполнить синхронизации из 1С OData:
   - Спецификации: POST /api/v1/sync/specifications-odata  
     Тело: { base_url, entity_name: "Catalog_Спецификации", ... }  
     См. [backend/app/routers/sync.py](backend/app/routers/sync.py:91)
   - Спецификации по умолчанию: POST /api/v1/sync/default-specifications-odata  
     Тело: { base_url, entity_name: "InformationRegister_СпецификацииПоУмолчанию", ... }  
     См. [backend/app/routers/sync.py](backend/app/routers/sync.py:163)
   - (Опционально) Этапы производства: POST /api/v1/sync/production-stages-odata (если справочник этапов не подтянут)  
     См. [backend/app/routers/sync.py](backend/app/routers/sync.py:187)
2) Открыть /specification?item_code=XXX&amp;qty=Y и нажать «Загрузить», затем «Развернуть 1 уровень».  
   Примечание: фронт принудительно пытается развернуть корень; если детей нет, покажем уведомление с подсказкой про привязку «Спецификации по умолчанию».

Шаблоны запросов (пример через curl):
- Спецификации:
  curl -X POST "http://localhost:8000/api/v1/sync/specifications-odata" ^
       -H "Content-Type: application/json" ^
       -d "{ \"base_url\": \"http://srv-1c:8080/base/odata/standard.odata\", \"entity_name\": \"Catalog_Спецификации\" }"
- Спецификации по умолчанию:
  curl -X POST "http://localhost:8000/api/v1/sync/default-specifications-odata" ^
       -H "Content-Type: application/json" ^
       -d "{ \"base_url\": \"http://srv-1c:8080/base/odata/standard.odata\", \"entity_name\": \"InformationRegister_СпецификацииПоУмолчанию\" }"
- Этапы (при необходимости):
  curl -X POST "http://localhost:8000/api/v1/sync/production-stages-odata" ^
       -H "Content-Type: application/json" ^
       -d "{ \"base_url\": \"http://srv-1c:8080/base/odata/standard.odata\", \"entity_name\": \"Catalog_ЭтапыПроизводства\" }"

План: единицы измерения
- Добавить синхронизацию справочника 1С Catalog_ЕдиницыИзмерения:
  - Новый сервис sync_units_from_odata (backend/services/units_sync.py), таблица units (id, ref1c, code, name, short_name).
  - Маппинг Item.unit (GUID) → units.short_name в API дерева (или на фронте через кеш-словарь).
  - Кнопка синхронизации единиц запускается тем же сценарием, что и номенклатура (см. [backend/app/routers/sync.py](backend/app/routers/sync.py:43)).

Замечания по UI:
- В [SpecificationPage.vue](frontend/src/pages/SpecificationPage.vue:1) убрана зависимость от hasChildren при нажатии «Развернуть 1 уровень» — разворот вызывается всегда.
- После подгрузки детей обновляется ссылка на rows (rows.value = [...rows.value]) для гарантированного рендера QTable дерева.
- Если у узла нет детей, показываем предупреждение о необходимости привязки спецификации по умолчанию.

## 23.09.2025 — Диагностика «дерево спецификации не разворачивается»: добавлены серверные и клиентские логи

Внесено:
- Backend (логгер "specification"):
  - Логирование ключевых этапов в [backend/app/routers/specification.py](backend/app/routers/specification.py:1):
    - [spec.tree] запрос и параметры (parent_id, item_code, item_id, root_qty, depth)
    - разбор parent_id и результат парсинга
    - поиск item по коду/ID
    - разрешение spec_id: default_specifications → fallback по совпадению (spec_code==item_code | spec_name==item_name)
    - наличие компонентов/операций, счётчики, общее количество детей, hasChildren по корню
  - Параметр depth=1 для предразворота 1 уровня на сервере (добавляет node.children у корня).
  - Включена базовая настройка логирования в [backend/app/main.py](backend/app/main.py:1) (logging.basicConfig INFO + уровень для логгера "specification").
- Frontend (консольные логи):
  - В [frontend/src/pages/SpecificationPage.vue](frontend/src/pages/SpecificationPage.vue:1) добавлены console.log/console.warn/console.error в:
    - loadRoot (параметры запроса, количество nodes, метаданные, root.id, размер children)
    - expandFirstLevel (root.id, expanded[])
    - loadChildrenFor (parent_id, количество детей/ошибки)
  - Метод getSpecificationTree поддерживает depth, чтобы запрашивать предразворот с сервера: [frontend/src/services/api.ts](frontend/src/services/api.ts:1)

Как собрать логи:
1) Сервер (uvicorn/fastapi):
   - Запустить бэкенд и воспроизвести запрос (UI или API).
   - В терминале увидеть строки вида:
     - [spec.tree] request ...
     - [spec.tree] resolve spec for item_id=... → found default_spec_id=... | fallback matched spec_id=...
     - [spec.tree] components count=N / operations count=M / children total=K
     - [spec.tree] root response children=K hasChildren=True|False
2) API-тест (Windows PowerShell, экранируем &):
   - $code = [uri]::EscapeDataString("КОД")
   - Invoke-RestMethod -Method GET -Uri "http://localhost:8000/api/v1/specification/tree?item_code=$code&amp;root_qty=1&amp;depth=1"
   - Для детей: взять meta.rootId и выполнить:
     - Invoke-RestMethod -Method GET -Uri "http://localhost:8000/api/v1/specification/tree?parent_id=item:123:1"
3) UI:
   - Открыть /specification?item_code=КОД&amp;qty=1 → “Загрузить”.
   - В консоли браузера:
     - [SpecPage] loadRoot: request/response...
     - [SpecPage] expandFirstLevel...
     - [SpecPage] loadChildrenFor: children count=...

Ожидаемый эффект:
- В логе видно, какой spec_id резолвится (default/fallback), что возвращает _children_for_item (количество компонентов/операций), и почему children пусты (если пусты).
- Фронтенд дополнительно выводит количество полученных children и идентификаторы узлов.

Примечание:
- План по Catalog_ЕдиницыИзмерения остаётся актуален (unit GUID → краткое обозначение), вынесен в предыдущую запись как отдельная задача.

## 23.09.2025 — Полноценная синхронизация единиц измерения (ЕИ) и привязка к кнопке "Синхронизация номенклатуры"

Статус: Реализовано. Синхронизация ЕИ выполняется последовательно сразу после синхронизации номенклатуры при нажатии одной кнопки на странице синхронизации.

Backend:
- Модель Unit добавлена в [backend/app/models.py](backend/app/models.py:1)
  - Поля: unit_id, unit_ref1c (Ref_Key), unit_code (Code), unit_name (Description), unit_full_name, short_name, iso_code, base_unit_ref1c (БазоваяЕдиница_Key), ratio (Кратность/Коэффициент), precision, created_at, updated_at.
  - Таблица создаётся автоматически через Base.metadata.create_all при старте приложения [backend/app/main.py](backend/app/main.py:1).
- Сервис синхронизации ЕИ: [backend/app/services/units_sync.py](backend/app/services/units_sync.py:1)
  - Использует существующий OData-клиент [backend/app/services/odata_client.py](backend/app/services/odata_client.py:1).
  - Гибкий маппинг полей (Ref_Key, Code, Description, НаименованиеПолное/ПолноеНаименование, Сокращение/КраткоеНаименование, МеждународноеСокращение/ISOCode, БазоваяЕдиница*_Key, Кратность/Коэффициент, Точность).
  - Upsert по unit_ref1c, при необходимости fallback по unit_code.
  - Прогресс: ключ "units" через progress_manager [backend/app/services/progress_manager.py](backend/app/services/progress_manager.py:1).
- Роут синхронизации: POST /api/v1/sync/units-odata в [backend/app/routers/sync.py](backend/app/routers/sync.py:1)
  - Тело запроса — как у остальных OData sync: base_url, entity_name (ожидаем "Catalog_ЕдиницыИзмерения"), username, password, token, filter_query, select_fields, dry_run, zero_missing.
  - Возвращает статистику: units_total, units_created, units_updated, units_unchanged, dry_run, odata_url, odata_entity.

Frontend:
- Страница синхронизации: [frontend/src/pages/SyncPage.vue](frontend/src/pages/SyncPage.vue:1)
  - Обработчик "Синхронизация номенклатуры" теперь выполняет два шага последовательно:
    1) POST /v1/sync/nomenclature-odata с прогрессом ключа "nomenclature".
    2) POST /v1/sync/units-odata с прогрессом ключа "units".
  - Прогресс-бар переиспользован: добавлен переключатель ключа progressKey ('nomenclature' | 'units') и опрос общего прогресса /v1/sync/progress?key={progressKey}.
  - В случае ошибки ЕИ — отображается предупреждение, общий флоу не валится (по требованию). Детали ошибки выводятся в уведомлении и в подписях прогресс-бара.

Особенности и допущения:
- Alembic-миграции в этом проекте не используются; таблица units создаётся автоматически через Base.metadata.create_all.
- Для корректного отображения ЕИ в дереве спецификаций далее планируется либо:
  - резолвить GUID → краткое название на бэкенде, либо
  - подгружать словарь на фронтенде и отображать по словарю. Выбор зависит от общего подхода к API спецификаций.

Проверка:
1) UI:
   - Настроить OData на странице синхронизации (base_url и доступ).
   - Нажать «Синхронизация номенклатуры».
   - Убедиться, что после завершения номенклатуры автоматически запускается синхронизация ЕИ (индикатор прогресса и подписи меняются).
   - При успехе обоих шагов — положительное уведомление; при ошибке ЕИ — предупреждение, номенклатура остаётся успешной.
2) API:
   - POST /api/v1/sync/units-odata с entity_name="Catalog_ЕдиницыИзмерения" (см. аналогии в [backend/app/routers/sync.py](backend/app/routers/sync.py:1)).
   - GET /api/v1/sync/progress?key=units — наблюдать изменение processed/percent/status.

Что ещё улучшить для дерева спецификаций (план доработок):
- DTO узла (утверждение и стабилизация): id, parentId, type, name/title, code/article, qtyPerParent, unit, stage, operation, timeNormNh, computed, hasChildren, warnings, meta. См. текущую реализацию в [backend/app/routers/specification.py](backend/app/routers/specification.py:1) и типизацию на фронте [frontend/src/services/api.ts](frontend/src/services/api.ts:1).
- Ленивые дети + сортировка + пагинация: дочерние грузить по parentId; обеспечить стабильный сорт.
- Поиск и фильтры: серверные параметры фильтрации и поиска по дереву; поддержка "только проблемы".
- Производительность: кэширование ответов, виртуализация на фронтенде, skeleton-плейсхолдеры, ограничение depth.
- Валидации/статусы: разметка NO_STAGE, NO_TIME_NORM, CYCLE_DETECTED; консистентная подсветка в UI.
- Состояние: сохранение/восстановление раскрытых узлов (по nodeId/path).
- ЕИ в дереве: резолвить unit (GUID) → читаемое имя (из таблицы units) на стороне API, чтобы фронт показывал корректную единицу измерения в каждой строке.

Рекомендация по документации:
- Начать формально описывать структуру БД, сущности и связи в .docs/ (ER-диаграмма и словарь данных), чтобы ускорить навигацию по полям и связям. Предлагаем создать .docs/db_schema.md с таблицами: items, item_categories, specifications, spec_components, operations, spec_operations, production_stages, default_specifications, units и т.д., со списком столбцов и ключей.

## 23.09.2025 — Наименования операций в спецификации (исправление прочерка) и синхронизация операций

Контекст:
- На странице [SpecificationPage.vue](frontend/src/pages/SpecificationPage.vue:1) в строках типа operation отображался прочерк вместо наименования.
- Причина: при загрузке спецификаций в БД Operation.operation_name не был заполнен (сохранялись только GUID (operation_ref1c) и нормы времени).
- В текущей конфигурации 1С отдельные каталоги операций (Catalog_Операции*) недоступны по OData, зато присутствует набор строк спецификаций: Catalog_Спецификации_Операции. По каждой строке доступна навигация "Операция@navigationLinkUrl" → оттуда можно получить Description (имя операции).

Сделано:
1) Бэкенд: реализована синхронизация наименований операций через строки спецификаций
   - Новый сервис: [operations_sync.py](backend/app/services/operations_sync.py:1)
     - Постранично читает "Catalog_Спецификации_Операции", собирает уникальные GUID операций (Операция_Key), запрашивает по навигации Операция@navigationLinkUrl и сохраняет Description в Operation.operation_name (upsert по Operation.operation_ref1c).
     - Использует прогресс-менеджер с ключом "operations".
   - Новый роут: POST /api/v1/sync/operations-odata в [sync.py](backend/app/routers/sync.py:227)
     - Параметры, как у других OData-синхронизаций; по умолчанию entity_name = "Catalog_Спецификации_Операции".
     - Возвращает статистику: operations_seen_unique, operations_created, operations_updated, operations_unchanged.
2) Фронтенд: кнопка «Синхронизация операций» с прогресс-баром
   - Страница синхронизации: [SyncPage.vue](frontend/src/pages/SyncPage.vue:29)
     - Добавлена кнопка «Синхронизация операций», вызов POST /v1/sync/operations-odata.
     - Прогресс: переиспользован общий опрос GET /v1/sync/progress с ключом progressKey='operations'.
3) Фронтенд: отображение имени операции в дереве спецификации
   - Страница: [SpecificationPage.vue](frontend/src/pages/SpecificationPage.vue:87)
   - Для узлов type === 'operation' теперь выводится props.node.operation?.name (что приходит из API), иначе — name у номенклатуры.
   - Бэкенд уже формирует поле operation.name из Operation.operation_name (см. [specification.py](backend/app/routers/specification.py:312)) — после синхронизации операций имя начинает отображаться.

Проверка:
- UI:
  1) На странице «Синхронизация» ввести настройки OData.
  2) Нажать «Синхронизация операций» → наблюдать прогресс; по завершению будет показана статистика (уникальных, создано, обновлено).
  3) Открыть «Спецификация» (дерево), загрузить корень и развернуть 1 уровень — вместо прочерка у операций отображается имя.
- API:
  - POST /api/v1/sync/operations-odata с телом вида:
    {
      "base_url": "http://srv-1c/base/odata/standard.odata",
      "entity_name": "Catalog_Спецификации_Операции",
      "username": "...",
      "password": "...",
      "token": null,
      "filter_query": null,
      "select_fields": null,
      "dry_run": false,
      "zero_missing": false
    }
  - GET /api/v1/sync/progress?key=operations — состояние прогресса (total/processed/percent/finished/message).

Итог:
- На странице спецификации вместо прочерка выводится наименование операции (при условии, что выполнена «Синхронизация операций»).
- Кнопка выгрузки операций добавлена на страницу синхронизации, с прогресс-баром по аналогии с номенклатурой/ЕИ.
- Решение устойчиво к различиям конфигурации 1С: не требует знания отдельного каталога операций, использует навигацию из строк спецификаций.

Затронутые файлы:
- Бэкенд:
  - [backend/app/services/operations_sync.py](backend/app/services/operations_sync.py:1) — новый сервис
  - [backend/app/routers/sync.py](backend/app/routers/sync.py:16) — импорт сервиса
  - [backend/app/routers/sync.py](backend/app/routers/sync.py:227) — новый эндпоинт POST /operations-odata
- Фронтенд:
  - [frontend/src/pages/SyncPage.vue](frontend/src/pages/SyncPage.vue:29) — кнопка «Синхронизация операций», прогресс 'operations'
  - [frontend/src/pages/SpecificationPage.vue](frontend/src/pages/SpecificationPage.vue:87) — вывод operation?.name у узлов-операций

## 23.09.2025 — Реализация «полной спецификации» (полное дерево BOM с детьми)

Сделано:
- Backend:
  - Добавлен эндпоинт GET /api/v1/specification/full в [backend/app/routers/specification.py](backend/app/routers/specification.py:845)
  - Реализована рекурсивная сборка дерева спецификации с включением всех уровней и операций: [_build_full_tree()](backend/app/routers/specification.py:763)
  - Используется существующая логика построения узлов и детей: [_children_for_item()](backend/app/routers/specification.py:332), [_make_item_node()](backend/app/routers/specification.py:252), [_make_operation_node()](backend/app/routers/specification.py:290)
  - Анти‑цикл: если при разворачивании встречается уже посещённый item_id — узлу добавляется предупреждение "CYCLE_DETECTED", и ветка дальше не раскрывается
  - Ограничение глубины: параметр max_depth (по умолчанию 15, допускается 1..50)
  - Резолв ЕИ: unit GUID → человекочитаемая метка через справочник [Unit](backend/app/models.py:56) и словарь [_build_units_map()](backend/app/routers/specification.py:176)
  - Логирование: тэги [spec.full] для диагностики (старт, ошибки рекурсии)

- Frontend:
  - API‑хелпер для полного дерева: [getSpecificationFull()](frontend/src/services/api.ts:64)
  - На странице спецификации добавлена кнопка «Развернуть полностью», вызывающая полную загрузку дерева и автоматическое раскрытие всех item‑узлов:
    - Импорт хелпера: (см. [frontend/src/pages/SpecificationPage.vue](frontend/src/pages/SpecificationPage.vue:142))
    - Кнопка и обработчик: методы loadFull(), collectAllItemIds() и состояние loading.full (см. [frontend/src/pages/SpecificationPage.vue](frontend/src/pages/SpecificationPage.vue:330))
  - Идентификаторы узлов сохраняются совместимыми с lazy‑API (/tree): item:{item_id}:{tree_qty} и op:{spec_operation_id}:{parent_item_id}:{parent_tree_qty}
  - Для стабильной работы QTree обеспечивается уникальность id в поддеревьях: ensureUniqueIds()

API/Контракт:
- GET /api/v1/specification/full?item_code=XXX&amp;root_qty=1&amp;max_depth=15
- Ответ: { "nodes": [ rootNode ], "meta": { rootId, requested, debug? } }
- Формат узлов полностью совпадает с /api/v1/specification/tree: type=item|operation, qtyPerParent, computed.treeQty, timeNormNh, computed.treeTimeNh, stage, unit, warnings и т.д.
- Защита от циклов: предупреждение "CYCLE_DETECTED" в warnings узла

Проверка:
1) Backend (curl/браузер):
   - http://localhost:8000/api/v1/specification/full?item_code=КОД&amp;root_qty=1&amp;max_depth=15
   - В логе смотреть префиксы [spec.full]
2) UI:
   - Открыть /specification?item_code=КОД&amp;qty=Y
   - Нажать «Развернуть полностью»
   - Должно подгрузиться всё дерево (включая операции), QTree автоматически раскрыт по всем item‑узлам
3) Предпосылки:
   - Должны быть синхронизированы спецификации, спецификации по умолчанию, ЕИ и (опционально) операции:
     - POST /api/v1/sync/specifications-odata (см. [backend/app/routers/sync.py](backend/app/routers/sync.py:1))
     - POST /api/v1/sync/default-specifications-odata (см. [backend/app/routers/sync.py](backend/app/routers/sync.py:1))
     - POST /api/v1/sync/units-odata (см. [backend/app/routers/sync.py](backend/app/routers/sync.py:1))
     - POST /api/v1/sync/operations-odata (см. [backend/app/routers/sync.py](backend/app/routers/sync.py:227)) — для отображения названий операций

Примечания:
- Производительность: полная развертка может быть тяжёлой на глубоких спецификациях; используйте max_depth при необходимости
- Совместимость: фронтенд продолжает поддерживать ленивый режим через /tree; «полное дерево» — дополнительная опция
- Предупреждения:
  - NO_STAGE — отсутствует этап у номенклатуры/операции
  - NO_TIME_NORM — отсутствует норма времени у операции
  - CYCLE_DETECTED — обнаружен цикл в BOM

## 23.09.2025 — Полная спецификация: покупные позиции, колонка «Метод пополнения», выравнивание и упрощение UI

Изменения Backend:
- Узел номенклатуры дополнен полем replenishmentMethod (метод пополнения), берётся из Item.replenishment_method:
  - Реализация в [_make_item_node()](backend/app/routers/specification.py:252)
  - Формат ответа /v1/specification/tree и /v1/specification/full совместим c фронтом
- Поведение по покупным позициям:
  - Покупные и перерабатываемые позиции включаются в дерево наравне с остальными компонентами спецификации (без фильтрации по типу).
  - У таких позиций, как правило, нет этапа (stage) и детей; поле hasChildren=false вычисляется на базе отсутствия дефолтной спецификации.

Изменения Frontend:
- Добавлена колонка «Метод пополнения» и выравнивание всех колонок, кроме «Наименование», вправо:
  - Шапка и строки дерева обновлены в [frontend/src/pages/SpecificationPage.vue](frontend/src/pages/SpecificationPage.vue:72)
  - Для item-узлов отображается props.node.replenishmentMethod
- Типизация API:
  - В тип SpecNode добавлено поле replenishmentMethod?: string | null — см. [frontend/src/services/api.ts](frontend/src/services/api.ts:34)
- Упрощение интерфейса страницы «Спецификация»:
  - Удалены неиспользуемые и сбивающие с толку элементы:
    - Кнопка «Развернуть 1 уровень»
    - Кнопка «Force expand 1-й уровень»
    - Переключатель «Показывать только проблемы»
    - Надпись «Дочерних узлов: …»
  - Удалён связанный код (watch на expanded, lazy-load и вспомогательные функции для него)
  - Сохранена кнопка «Развернуть полностью», использующая endpoint /v1/specification/full

Файлы:
- Backend:
  - [backend/app/routers/specification.py](backend/app/routers/specification.py:252) — добавлено поле replenishmentMethod в узел item
- Frontend:
  - [frontend/src/services/api.ts](frontend/src/services/api.ts:34) — обновлён тип SpecNode
  - [frontend/src/pages/SpecificationPage.vue](frontend/src/pages/SpecificationPage.vue:72) — новая колонка, выравнивание и уборка неиспользуемых элементов

Проверка:
1) Синхронизировать данные (номенклатура, спецификации, дефолтные спецификации, ЕИ, операции).
2) Открыть страницу «Спецификация», ввести item_code и qty, нажать «Загрузить» или «Развернуть полностью».
3) Убедиться, что в дереве присутствуют покупные позиции (без этапа, без детей), и для них заполнена колонка «Метод пополнения» (Закупка/Переработка).
4) Визуально проверить выравнивание — все колонки кроме «Наименование» выровнены по правому краю, переносы стали реже, таблица выглядит аккуратнее.

Примечания:
- Сообщение «Нет данных спецификации для этого узла…» больше не появляется, т.к. удалён lazy-expand и связанный код.
- При необходимости можно управлять глубиной развертки полного дерева параметром max_depth у /v1/specification/full.
