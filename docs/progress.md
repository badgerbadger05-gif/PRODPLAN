# PRODPLAN — Прогресс сессии от 2025-09-18

Статус: сессия открыта.

## Сделано в этой сессии
- Прочитал все файлы из каталога docs/
- Получил полное представление о проекте PRODPLAN
- Подготовил обновление файла docs/progress.md
- Применены правки к интерфейсу поиска номенклатуры: заменен стандартный текстовый ввод и выпадающий список на компонент `st_searchbox` с автодополнением. Файл правок: [`docs/Правки в интерфейсе.md`](docs/Правки в интерфейсе.md), изменения в [`src/ui.py`](src/ui.py).

- Исправлен вызов функции добавления позиции из поиска: теперь используется корректная функция `_ensure_item_and_add_to_roots(...)` вместо отсутствующей `ensure_item_and_add_to_roots(...)` (см. [src/ui.py](src/ui.py:1416)). Это устраняет исключение при выборе варианта в автодополнении.
- Рефакторинг семантического поиска: удалено дублирование чтения индекса и повторных эмбеддингов, выделён единый строковый фолбэк. Правки в [def _llama_search_nomenclature()](src/ui.py:600). Поиск стабилен даже при недоступности эмбеддингов, код стал короче и предсказуемее по ветвлениям.
- Зафиксирована зависимость компонента автодополнения: добавлен пакет `streamlit-searchbox>=0.4.0` в [requirements.txt](requirements.txt). Это соответствует использованию импорта `from streamlit_searchbox import st_searchbox` в [src/ui.py](src/ui.py:10).
- Напоминание по развёртыванию: выполнить `pip install -r requirements.txt` для установки новой зависимости перед запуском UI.
- Ожидаемый эффект: при вводе «Наименование/Артикул» подсказки снова появляются, выбор элемента не приводит к ошибке, элемент корректно добавляется в план; при недоступности LLM или пустых векторах срабатывает надёжный строковый фолбэк по article/code/name. Сценарий «Г0003216 ↔ G0003216» продолжает находиться за счёт нормализации и «латинизации».
- Удалён блок «Фильтры» на странице «План производства» (левая колонка с контролами); таблица, редактор записи дня и экспорт оставлены без изменений. См. [def plan_page()](src/ui_nicegui/routes.py:62).

### Описание удалённого блока «Фильтры» (для последующего применения)
Место вёрстки (до удаления): левая колонка внутри секции макета страницы рядом с таблицей (бывшие строки 174–187) в [src/ui_nicegui/routes.py](src/ui_nicegui/routes.py:171). Управлял состоянием страницы и обновлением таблицы [def render_table()](src/ui_nicegui/routes.py:105) через [def _apply_filters()](src/ui_nicegui/routes.py:82).

Состав и назначение контролов:
- Дата начала периода — input type=date, обновлял state.start (ISO, по умолчанию сегодня). Инициализация состояния: [def plan_page()](src/ui_nicegui/routes.py:69).
- Горизонт дней — number (мин 1, макс 90), обновлял state.days (по умолчанию 30). См. инициализацию: [def plan_page()](src/ui_nicegui/routes.py:71).
- Этап — select из справочника этапов (stage_map, с ключом 0 = «Все этапы»). Загрузка этапов: [def fetch_stages()](src/ui_nicegui/services/plan_service.py:31), формирование словаря на странице: [src/ui_nicegui/routes.py](src/ui_nicegui/routes.py:97). Значение записывалось в state.stage_id (0|int|None).
- Лимит (устар.) — number; совместимость с ранними версиями выборки, в server-side режиме не используется. Инициализация: [def plan_page()](src/ui_nicegui/routes.py:73).
- Строк на страницу — number (10–500, шаг 10), обновлял state.page_size и сбрасывал state.page в 1.
- Сортировка — select по полям: item_name, item_code, month_plan; обновлял state.sort_by и сбрасывал state.page в 1.
- Направление — select asc|desc; обновлял state.sort_dir и сбрасывал state.page в 1.
- Кнопка «Обновить» — вызывала [def _apply_filters()](src/ui_nicegui/routes.py:82), которая сбрасывала страницу на 1 и выполняла render_table.refresh().

Связи и поток данных:
- Таблица рендерилась в [def render_table()](src/ui_nicegui/routes.py:105), данные получались из [def query_plan_overview_paginated()](src/ui_nicegui/services/plan_service.py:144) с параметрами из state: start, days, stage_id, page, page_size, sort_by, sort_dir. total использовался для вычисления total_pages; управление страницами осуществлялось через кнопки (⏮ ◀ ▶ ⏭) и [def _set_page()](src/ui_nicegui/routes.py:86).
- Экспорт «CSV/Excel» — функция [def _export()](src/ui_nicegui/routes.py:90) открывала /api/plan/export с текущими параметрами (start_date, days, опционально stage_id). Эндпоинт: [def api_plan_export()](src/ui_nicegui/app.py:122), выборка набора для экспорта: [def fetch_plan_dataset()](src/ui_nicegui/services/plan_service.py:230).
- Редактор «Сохранить запись дня» использовал текущий выбранный item_id из таблицы и текущий stage_id (если 0 или None, запись сохранялась без этапа) через [def upsert_plan_entry()](src/ui_nicegui/services/plan_service.py:102).

Значения по умолчанию (после удаления фильтров остаются активными как фиксированные):
- start = сегодня (ISO), см. [def plan_page()](src/ui_nicegui/routes.py:70)
- days = 30, см. [def plan_page()](src/ui_nicegui/routes.py:71)
- stage_id = 0 («Все этапы», трактуется как None при запросах), см. [def plan_page()](src/ui_nicegui/routes.py:72) и использование в запросах [src/ui_nicegui/routes.py](src/ui_nicegui/routes.py:108)
- page = 1, page_size = 50, sort_by = item_name, sort_dir = asc, см. [def plan_page()](src/ui_nicegui/routes.py:74)

API и сервисы, задействованные блоком:
- Серверная выборка страницы данных: [def api_plan_query()](src/ui_nicegui/app.py:106) → [def query_plan_overview_paginated()](src/ui_nicegui/services/plan_service.py:144)
- Экспорт набора: [def api_plan_export()](src/ui_nicegui/app.py:122) → [def fetch_plan_dataset()](src/ui_nicegui/services/plan_service.py:230)
- Справочник этапов: [def fetch_stages()](src/ui_nicegui/services/plan_service.py:31)

Как восстановить блок при необходимости:
1) Вернуть левую колонку с карточкой «Фильтры» в [def plan_page()](src/ui_nicegui/routes.py:62) сразу после секции «Макет страницы» — рядом с колонкой таблицы. 
2) Контролы должны обновлять соответствующие поля состояния (start, days, stage_id, limit, page_size, sort_by, sort_dir) и сбрасывать page в 1 там, где это предусмотрено.
3) Кнопка «Обновить» должна вызывать [def _apply_filters()](src/ui_nicegui/routes.py:82) для refresh таблицы.
4) Для экспорта опционально передавать stage_id (преобразуя 0 → None) через [def _export()](src/ui_nicegui/routes.py:90).
## Добавлено: страница «Настройки синхронизации 1С» и пункт меню
- В левом меню добавлен пункт «Настройки синхронизации 1С» → маршрут /settings/odata. Реализовано в [src/ui_nicegui/components/layout.py](src/ui_nicegui/components/layout.py).
- Создана страница настроек с полями ввода:
  - base_url
  - username
  - password
  - Страница и обработчики: [def odata_settings_page()](src/ui_nicegui/routes.py:224)
- Реализованы кнопки:
  - «Сохранить настройки» — сохраняет параметры в [config/odata_config.json](config/odata_config.json) (plain JSON, без шифрования).
  - «Тест подключения» — обращается к API [def api_odata_test()](src/ui_nicegui/app.py:239), выполняет запрос к $metadata и возвращает статус/размер ответа.
  - «Выгрузить метаданные» — обращается к API [def api_odata_metadata()](src/ui_nicegui/app.py:262), сохраняет:
    - XML: output/odata_metadata.xml
    - Summary JSON: output/odata_metadata_summary.json
  - «Принудительная индексация номенклатуры» — обращается к API [def api_odata_reindex()](src/ui_nicegui/app.py:301); сейчас возвращает not_implemented (ожидает интеграцию с ensure_llama_index_daily в NiceGUI).
- Серверная часть (FastAPI внутри NiceGUI):
  - Модель запроса: [class ODataConfigReq](src/ui_nicegui/app.py:163)
  - Нормализация base_url: отрезание «/$metadata», приведение хвостового «/».
  - Эндпоинты:
    - POST /api/odata/test → [def api_odata_test()](src/ui_nicegui/app.py:239)
    - POST /api/odata/metadata → [def api_odata_metadata()](src/ui_nicegui/app.py:262)
    - POST /api/odata/reindex → [def api_odata_reindex()](src/ui_nicegui/app.py:301)
    - Дополнительно реализован вспомогательный API для сохранения конфига (не используется на странице, т.к. сохранение делается напрямую в файл): POST /api/odata/config → [def api_odata_save_config()](src/ui_nicegui/app.py:227)
- Клиентская логика страницы:
  - Поля формы предзаполняются из [config/odata_config.json](config/odata_config.json) при наличии файла.
  - Кнопки вызывают fetch к соответствующим эндпоинтам; результат отображается через ui.notify.
  - При сохранении настроек проводится перенос старых совместимых полей (entity_name, select_fields) из существующего файла, если они были.
- Навигация:
  - Подсветка активного раздела «Настройки» в shell работает через параметр active='settings' в [def shell()](src/ui_nicegui/components/layout.py:17).

Замечание по безопасности:
- Пароль сохраняется в открытом виде в [config/odata_config.json](config/odata_config.json). Для продакшна рекомендуется:
  - использовать переменные окружения или шифрование секрета;
  - ограничить права доступа к папке config/;
  - рассмотреть интеграцию с секрет-хранилищем.

Пути сохранения результатов «Выгрузить метаданные»:
- XML: output/odata_metadata.xml
- Summary JSON: output/odata_metadata_summary.json
## Миграция на NiceGUI — Шаг 1 (каркас) выполнен
- Создан минимальный каркас приложения NiceGUI:
  - Точка входа: [src/ui_nicegui/app.py](src/ui_nicegui/app.py)
  - Регистрация страниц: [src/ui_nicegui/routes.py](src/ui_nicegui/routes.py)
  - Общий layout: [src/ui_nicegui/components/layout.py](src/ui_nicegui/components/layout.py)
  - Скрипт запуска (dev): [run_ui_nicegui.bat](run_ui_nicegui.bat)
- Зависимости:
  - Добавлены: nicegui&gt;=1.4.0, uvicorn[standard]&gt;=0.30.0 в [requirements.txt](requirements.txt)
  - Зафиксирована совместимая версия: streamlit-searchbox==0.1.23 (для текущего Streamlit UI)
- API и маршруты:
  - Смонтирован FastAPI под /api; доступен health-check /api/health (см. [src/ui_nicegui/app.py](src/ui_nicegui/app.py))
  - Страницы: /, /plan, /stages (см. [src/ui_nicegui/routes.py](src/ui_nicegui/routes.py))
- Исправления совместимости NiceGUI:
  - Монтирование FastAPI: использовано ng_app.mount вместо устаревшего native.mount (см. [src/ui_nicegui/app.py](src/ui_nicegui/app.py))
  - Заменён ui.splitter.before/after на layout через ui.row/ui.column из-за отсутствия атрибутов before/after в текущей версии (см. [src/ui_nicegui/routes.py](src/ui_nicegui/routes.py))
- Проверка:
  - UI запущен командой: `python -m src.ui_nicegui.app` или [run_ui_nicegui.bat](run_ui_nicegui.bat)
  - http://localhost:8080/ и http://localhost:8080/plan открываются; отображаются заглушки таблиц и панель фильтров
- Следующие шаги (Шаг 2):
  - Подключить реальную БД через [src/database.py](src/database.py), отрисовать «План производства» из production_plan_entries
  - Добавить действия sync/generate через фоновый вызов Python-функций модулей: [src/odata_stock_sync.py](src/odata_stock_sync.py), [src/planner.py](src/planner.py)
  - Подготовить компоненты таблиц (ui.aggrid/ui.table) с пагинацией и фильтрами
# PRODPLAN — Прогресс сессии от 2025-09-17

Статус: сессия закрыта.

## Сделано в этой сессии
- Добавлен health-check и автозапуск LLM (Ollama) при старте UI: проверка `/api/tags`, запуск `ollama serve` при недоступности, ожидание готовности.
- Реализована локальная индексация справочника номенклатуры из 1С OData (без внешнего API индексации):
  - Загрузка Catalog_Номенклатура (Code, Description, Артикул) постранично.
  - Вычисление эмбеддингов через Ollama (модель `nomic-embed-text`).
  - Сохранение индекса с векторами и метаданными в [`output/nomenclature_index.json`](output/nomenclature_index.json).
- В UI добавлен поиск с автодополнением по индексу (семантический), фолбэк — строковый поиск по БД.
- Добавлены прогресс и статус в UI для двух фаз:
  - fetch (загрузка из OData): счётчик «обработано X из Y», текущая позиция.
  - embed (эмбеддинги): отдельный счётчик/прогресс для реально пересчитываемых записей.
- Ускорение последующих индексаций:
  - Инкрементальная индексация: переиспользование неизменённых позиций, пересчёт только новых/изменённых.
  - Параллельные эмбеддинги (пул потоков, по умолчанию `max_workers=4`).
- Хранилище индекса: [`output/nomenclature_index.json`](output/nomenclature_index.json).
- Изменения в UI выполнены в [`src/ui.py`](src/ui.py). OData-клиент используется из [`src/odata_client.py`](src/odata_client.py). Схема БД — см. [`src/database.py`](src/database.py).

Дополнительно реализовано в рамках доработок этой сессии
- Форсированная переиндексация индекса номенклатуры без проверки 24 часов:
  - Кнопка на странице «Параметры синхронизации БД»: [`src/ui.py`](src/ui.py:1800)
  - Поддержка параметра `force` в индексации: [`def ensure_llama_index_daily(..., force: bool = False)`](src/ui.py:372) и проверка окна 24ч с `force`: [`src/ui.py`](src/ui.py:388)
- Улучшен фолбэк‑поиск по БД для артикула:
  - Поиск по item_article с нормализацией (удаление пробелов/дефисов/подчёркиваний, upper) и «латинизацией» кириллических символов (Г↔G и пр.): [`def _db_search_nomenclature()`](src/ui.py:623)
  - Маппинг похожих символов (Кириллица→Латиница): [`_CYR_TO_LAT_MAP`](src/ui.py:165), «латинизация» запроса: [`_augment_query_for_article()`](src/ui.py:197)
- Улучшен семантический поиск:
  - Запрос для эмбеддингов дополняется латинским вариантом строки, чтобы «Г0003216» и «G0003216» давали совпадение: [`_llama_search_nomenclature()`](src/ui.py:521)
  - При сбое/несовпадении размерностей работает строковый фолбэк по индексу (article/code/name): [`_llama_search_nomenclature()`](src/ui.py:554)
- Индекс теперь содержит запись даже при отсутствии вектора (vector=[]), чтобы строковый фолбэк по индексу мог находить позицию: сборка final_items в [`ensure_llama_index_daily()`](src/ui.py:451)
- Загрузка каталога из 1С:
  - Используется подсказка `$count` при наличии, чтобы выгружать все записи; добавлен `$orderby=Code`: [`_fetch_nomenclature_from_1c()`](src/ui.py:250), параметры запроса: [`src/ui.py`](src/ui.py:282)
- Синхронизация артикула в БД (items.item_article) из локального индекса:
  - Идемпотентная синхронизация при старте и после индексации: [`_sync_item_articles_from_index()`](src/ui.py:115), вызовы — [`main()`](src/ui.py:826), [`ensure_llama_index_daily()`](src/ui.py:475)
- UI/подписи подсказок:
  - Теперь «Наименование, Артикул» (если артикула нет — показываем код): формирование label в добавлении номенклатуры: [`src/ui.py`](src/ui.py:1147)
- План производства (внутренние ключи/отображение):
  - Внутренние операции и привязки к БД ведутся по «Код изделия» (служебная колонка), отображение пользователю — «Артикул изделия»:
    - Использование «Код изделия» в вычислениях и сохранении: [`src/ui.py`](src/ui.py:1027), [`src/ui.py`](src/ui.py:1053)

## Обнаруженная проблема (зафиксировано)
Требование: «подбор должен выполняться по Наименованию и Артикулу». Важно: Артикул ≠ Код (разные сущности; см. [`docs/сущности_1С.md`](docs/сущности_1С.md)). Включение Кода в текст эмбеддингов и в инкрементальный хэш — ожидаемое поведение, т.к. Код уникален в 1С и у части позиций Артикул может отсутствовать.

Статус исправления
- Подписи в UI приведены к виду «Наименование, Артикул» (при отсутствии артикула — показывается код): [`src/ui.py`](src/ui.py:1147).
- Фолбэк‑поиск по БД расширен: учитывает `item_article` (LIKE), «латинизацию» артикула и нормализованный артикул, а также имя; см. [`_db_search_nomenclature()`](src/ui.py:623).
- Семантический слой учитывает латинский «двойник» запроса для устойчивости к «Г/G»: [`_augment_query_for_article()`](src/ui.py:197), [`_llama_search_nomenclature()`](src/ui.py:534).
- Индекс хранит запись даже при отсутствии вектора (vector=[]), что позволяет строковому фолбэку по индексу находить позицию: [`ensure_llama_index_daily()`](src/ui.py:451).
- Проверена конкретная позиция из 1С OData и наличие в индексе:
  - Code: 00-00003216, Description: «Мотобуксировщик IKUDZO 2.0 K15 Lifan 1450/500 Чёрно-красный», Артикул: Г0003216 — присутствует в [`output/nomenclature_index.json`](output/nomenclature_index.json:2374579).

### Доп. архитектурный нюанс
В текущей схеме `items` отсутствует отдельная колонка «Артикул» в исходной миграции; добавлена идемпотентная синхронизация `items.item_article` из индекса при старте и после индексации: [`_sync_item_articles_from_index()`](src/ui.py:115).

## Что необходимо изменить (план правок)

Выполнено
1) Семантический слой:
   - Формула индекса: `name | article | code` сохранена; поиск усиливается запросом по паре `name | article` с «латинизацией» ввода: [`_augment_query_for_article()`](src/ui.py:197), [`_llama_search_nomenclature()`](src/ui.py:534).
2) Подписи автодополнения:
   - Варианты отображаются как «Наименование, Артикул»; при отсутствии артикула — показывается код: [`src/ui.py`](src/ui.py:1147).
3) Инкрементальная индексация:
   - Хеш `SHA1(name|article|code)` сохранён; записи без вектора попадают в индекс с `vector=[]` для строковых фолбэков: [`ensure_llama_index_daily()`](src/ui.py:451).
4) Фолбэк‑поиск по БД:
   - Добавлена поддержка `item_article` + нормализация/«латинизация» артикула; см. [`_db_search_nomenclature()`](src/ui.py:623).
   - Идемпотентная синхронизация `items.item_article` из индекса при старте/после индексации: [`_sync_item_articles_from_index()`](src/ui.py:115), вызовы — [`main()`](src/ui.py:826), [`ensure_llama_index_daily()`](src/ui.py:475).
5) UI/терминология:
   - Подписи стандартизованы («Наименование», «Артикул»).
6) Форсированная переиндексация:
   - Добавлена кнопка «Переиндексация номенклатуры (форс, без проверки 24ч)» на странице параметров: [`_render_sync_settings_page()`](src/ui.py:1800).
   - Добавлен параметр `force` в индексацию: [`ensure_llama_index_daily(..., force=True)`](src/ui.py:372), обход окна 24ч: [`src/ui.py`](src/ui.py:388).
7) Загрузка каталога:
   - Использование `$count` (если доступно) и `$orderby=Code` для полной и детерминированной выгрузки: [`_fetch_nomenclature_from_1c()`](src/ui.py:250), параметры: [`src/ui.py`](src/ui.py:282).
8) План:
   - Внутренний ключ — «Код изделия», отображение — «Артикул изделия»; см. использование «Код изделия» в расчетах: [`src/ui.py`](src/ui.py:1027), [`src/ui.py`](src/ui.py:1053).

Осталось/Рекомендации
- Вынести проверочные кейсы (unit) для нормализации артикула и фолбэк‑поиска: `_normalize_for_match()`, `_to_lat_lookalike()`, `_db_search_nomenclature()`.
- Провести выборочную сверку плана/экспорта Excel на больших объёмах, оценить время пересборки индекса и при необходимости добавить уведомления в UI.
- Зафиксировать в `docs/03-api-reference.md` описание кнопки форс‑индексации и параметра `force` в индексации.

## Риски и совместимость
- Изменение хеша приведёт к одноразовому пересчёту индекса (по времени — пропорционально числу позиций и скорости эмбеддингов).
- При добавлении колонки `item_article` требуется аккуратная миграция (идемпотентная) и обратная совместимость со старыми БД.

## Предлагаемые шаги (следующая сессия)
- Внести точечные правки в [`src/ui.py`](src/ui.py):
  - Формула текста для эмбеддингов (только name+article).
  - Подпись вариантов «Наименование, Артикул».
  - Хеш инкрементальной индексации — по паре (name, article).
- Подготовить миграцию БД для `item_article` (если «Артикул» не тождественен `item_code`) и обновить фолбэк-поиск.
- Перегенерировать индекс один раз («переиндексировано = ~8461», в дальнейшем — быстрый инкремент).
- Верифицировать в UI: подсказки по мере ввода корректно соответствуют Наименование/Артикул.

## Примечания
- Хранилище индекса: [`output/nomenclature_index.json`](output/nomenclature_index.json).
- Изменённые/созданные компоненты в сессии: [`src/ui.py`](src/ui.py).
## Миграция на NiceGUI — Шаг 2 (подключение БД, таблица плана, редактирование) — выполнено (MVP)
- Каркас страницы «План производства» подключен к реальным данным SQLite: агрегированный план за период по таблице `production_plan_entries` с привязкой к `items`.
- Реализован сервис доступа к данным: [src/ui_nicegui/services/plan_service.py](src/ui_nicegui/services/plan_service.py)
  - Агрегированный выбор плана (по периодам и этапам): fetch_plan_overview(...)
  - Получение справочника этапов: fetch_stages(...)
  - Идемпотентная запись дневного плана: upsert_plan_entry(...)
- Инициализация БД теперь выполняется при старте NiceGUI: [src/ui_nicegui/app.py](src/ui_nicegui/app.py)
- UI обновления:
  - Фильтры: дата начала, горизонт дней, этап, лимит строк.
  - Таблица с выбором строки (single selection), ключ `row_key=item_id`.
  - Редактор «дневной записи плана»: ввод даты и количества, кнопка «Сохранить запись дня». После сохранения — refresh таблицы.
  - Для совместимости с текущей версией NiceGUI заменен `ui.date` на `ui.input(type=date)` в фильтрах и редакторе.
- Проверка (локально):
  - Запуск: `python -m src.ui_nicegui.app` или [run_ui_nicegui.bat](run_ui_nicegui.bat)
  - Открыть http://localhost:8080/plan — отображаются данные из БД; возможно много строк (используется лимит 200 по умолчанию).
- Замечания/ограничения текущего MVP:
  - Таблица отображает агрегат «План на месяц» (сумма по дням в указанном окне); редактирование происходит для выбранного изделия на конкретную дату/этап.
  - Отображение и обновление построчно без постраничной навигации; для больших объемов рекомендуется перейти на AG-Grid (server-side) на следующем шаге.
  - Кнопки «Экспорт CSV/Excel» — заглушки (будет реализовано на следующем шаге).
- Следующий шаг (Шаг 2.1):
  - Подключить AG-Grid для таблицы плана (виртуальный скролл, пагинация, сорт/фильтр).
  - Вынести длительные операции в фон (FastAPI) для кнопок «Обновить остатки», «Обновить спецификации», «Сгенерировать план» с прогрессом/нотификациями.
  - Добавить в таблицу отображение текущего выбранного этапа и визуальную индикацию успешной записи.
## Миграция на NiceGUI — Шаг 2.1 (AG-Grid + фоновые операции через FastAPI) — выполнен
- Таблица на странице «План производства» переведена на AG-Grid:
  - Конфиг клиентской пагинации/сортировки/фильтрации (50 строк на страницу), выбор одной строки для редактирования.
  - Реализация в [src/ui_nicegui/routes.py](src/ui_nicegui/routes.py:61) — блок render_table(), создание грида через `ui.aggrid(...)` и обработчик выбора.
- Реализован API-слой фоновых операций (FastAPI) и подключен в UI:
  - Эндпоинты:
    - POST /api/generate/plan → генерация Excel через [src/planner.py](src/planner.py:127) ([api_generate_plan()](src/ui_nicegui/app.py:57)).
    - POST /api/sync/stock-history → синхронизация остатков + история через [src/stock_history.py](src/stock_history.py:223) ([api_sync_stock_history()](src/ui_nicegui/app.py:73)).
    - POST /api/sync/specs → заглушка (ожидается модуль импорта спецификаций) ([api_sync_specs()](src/ui_nicegui/app.py:85)).
  - Модели запросов: [GeneratePlanReq](src/ui_nicegui/app.py:30), [SyncStockHistoryReq](src/ui_nicegui/app.py:36), [SyncSpecsReq](src/ui_nicegui/app.py:41).
  - Монтаж FastAPI в NiceGUI: [app.ng_app.mount('/api', ...)](src/ui_nicegui/app.py:49).
  - Инициализация БД при старте UI: [init_database()](src/ui_nicegui/app.py:52).
  - Кнопки в header вызывают эндпоинты (через fetch) с нотификациями: [src/ui_nicegui/components/layout.py](src/ui_nicegui/components/layout.py:21).
- Проверка:
  - Запуск: `python -m src.ui_nicegui.app` или [run_ui_nicegui.bat](run_ui_nicegui.bat).
  - UI: http://localhost:8080/plan — доступна таблица AG-Grid, фильтры, выбор строки и сохранение дневной записи.
  - Кнопки в шапке:
    - «Проверка» — GET /api/health.
    - «Обновить остатки» — POST /api/sync/stock-history (фон).
    - «Обновить спецификации» — POST /api/sync/specs (заглушка).
    - «Сгенерировать план» — POST /api/generate/plan (фон), результат — файл Excel в output/.
- Ограничения на сейчас:
  - Таблица AG-Grid использует клиентскую пагинацию; для очень больших наборов данных планируется server-side режим.
  - «Обновить спецификации» — заглушка до подключения импорта спецификаций.
## Миграция на NiceGUI — Шаг 2.2 (server-side таблица + экспорт) — выполнен
- Backend
  - Добавлен эндпоинт POST /api/plan/query для серверной пагинации/сортировки/фильтрации результатов на странице «План производства» (см. [src/ui_nicegui/app.py](src/ui_nicegui/app.py)). Логика выборки реализована в сервисе [src/ui_nicegui/services/plan_service.py](src/ui_nicegui/services/plan_service.py) функцией query_plan_overview_paginated(...): возвращает rows, total, page, page_size с безопасным whitelist-сортировкой (item_name, item_code, month_plan).
  - Добавлен эндпоинт GET /api/plan/export?format=csv|excel&amp;start_date=...&amp;days=...&amp;stage_id=... для экспорта всего набора (с учетом фильтров) в CSV/Excel (см. [src/ui_nicegui/app.py](src/ui_nicegui/app.py)). Под капотом используется pandas+openpyxl.
- UI
  - Страница «План производства» переведена на серверную загрузку данных (см. [src/ui_nicegui/routes.py](src/ui_nicegui/routes.py)):
    - Состояние дополнено полями page, page_size, sort_by, sort_dir и total.
    - AG-Grid рендерит текущую страницу (без клиентской пагинации); добавлена отдельная панель управления страницами (⏮ ◀ ▶ ⏭) и индикация «Страница X из Y • Всего записей: N».
    - Добавлены элементы управления сортировкой и количеством строк на страницу.
    - Кнопки «Экспорт CSV» и «Экспорт Excel» вызывают /api/plan/export и отдают файл.
  - Редактирование дневной записи плана (upsert) сохранено без изменений и триггерит refresh таблицы.
- Зависимости
  - pandas и openpyxl уже присутствуют в [requirements.txt](requirements.txt); дополнительных изменений не требуется.
- Тестирование
  - Запуск: python -m src.ui_nicegui.app или [run_ui_nicegui.bat](run_ui_nicegui.bat)
  - Проверка:
    - http://localhost:8080/plan — таблица с пагинацией, сортировкой, фильтрами.
    - Экспорт: кнопки «Экспорт CSV/Excel» скачивают файл с текущими фильтрами.
- Ограничения и заметки
  - Подсчет total сейчас основан на количестве изделий (таблица items), чтобы отображать все изделия, включая те, у которых сумма плана за период равна 0 при выбранном этапе/фильтрах. Это согласуется с SQL (LEFT JOIN) и текущей моделью overview.
  - Для очень больших объемов данных возможно последующее переключение на «чистый» server-side режим AG-Grid (через datasource) — в текущем MVP применена серверная пагинация на уровне приложения, что упрощает интеграцию.
## OData — выборки по 7 сущностям и расхождения (2025‑09‑18)

Источник конфигурации: [config/odata_config.json](config/odata_config.json)

Сняты выборки ($top=5) и сохранены образцы ответов в output/*.json по перечню из [docs/сущности_1С.md](docs/сущности_1С.md). Сводная машина‑читаемая сводка: [output/odata_probe_summary.json](output/odata_probe_summary.json)

Артефакты (по одному файлу на сущность):
- output/odata_sample_Catalog_Номенклатура.json
- output/odata_sample_Catalog_КатегорииНоменклатуры.json
- output/odata_sample_AccumulationRegister_ЗапасыНаСкладах.json
- output/odata_sample_Catalog_Спецификации.json
- output/odata_sample_Catalog_Спецификации_Состав.json
- output/odata_sample_Catalog_Спецификации_Операции.json
- output/odata_sample_Document_ЗаказНаПроизводство.json
- output/odata_sample_Document_ЗаказНаПроизводство_Продукция.json
- output/odata_sample_Document_ЗаказНаПроизводство_Запасы.json
- output/odata_sample_Document_ЗаказНаПроизводство_Операции.json
- output/odata_sample_Document_ЗаказПоставщику.json
- output/odata_sample_Document_ЗаказПоставщику_Запасы.json
- output/odata_sample_InformationRegister_СпецификацииПоУмолчанию.json

Ключевые наблюдения и расхождения (по [output/odata_probe_summary.json](output/odata_probe_summary.json)):

1) Catalog_Номенклатура
- Ожидаемые поля из документа: Code, Description, Ref_Key, Артикул, ЕдиницаИзмерения_Key, КатегорияНоменклатуры_Key, СпособПополнения, СрокПополнения, ТипНоменклатуры — присутствуют.
- В ответе много дополнительных полей и навигаций (@navigationLinkUrl); это нормально для 1С UNF.
- Вывод: описание в [docs/сущности_1С.md](docs/сущности_1С.md) актуально, можно дополнить примечанием о множестве полей и навигациях.

2) Catalog_КатегорииНоменклатуры
- Базовые поля (Ref_Key, Code, Description, Parent_Key, IsFolder, Predefined, PredefinedDataName, DataVersion, DeletionMark) — присутствуют.
- Дополнительные поля присутствуют (связанные настройки/политики); допустимо.
- Вывод: описание актуально.

3) AccumulationRegister_ЗапасыНаСкладах — РАСХОЖДЕНИЕ
- Фактические поля в образце: RecordSet, Recorder, Recorder_Type.
- Ожидались: Номенклатура_Key, Склад_Key, КоличествоОстаток.
- Причина: для накопительных регистров UNF остатки читаются через ресурс "Остатки" (Balance), а не через сам набор записей регистра.
- Предложение: в документации и коде запросов использовать ресурс остатков, например:
  - GET {base}/AccumulationRegister_ЗапасыНаСкладах/Balance?$select=Номенклатура_Key,Склад_Key,КоличествоОстаток&amp;$top=5
  - либо (в русской локали) ресурс "Остатки" — точное имя ресурса/параметров подтверждается $metadata.
- Следствие: текущая реализация [src/odata_client.py](src/odata_client.py) для остатков должна запрашивать баланс, иначе мы видим «тело движения» (Recorder) вместо среза остатков.

4) Catalog_Спецификации и поднаборы _Состав/_Операции
- Catalog_Спецификации: Code, Description, Ref_Key присутствуют; также есть навигации: Состав, Операции, Сопоставления и др.
- Catalog_Спецификации_Состав: поля (Номенклатура_Key, Количество, Этап_Key, ТипСтрокиСостава) — присутствуют. Много доп. полей (формулы, линк‑поля).
- Catalog_Спецификации_Операции: поля (Операция_Key, НормаВремени, Этап_Key) — присутствуют. Есть доп. поля (формулы, линк‑поля).
- Вывод: описание актуально; стоит уточнить, что детали состав/операции приходят отдельными наборами, а внутри Catalog_Спецификации это навигации.

5) Document_ЗаказНаПроизводство и табличные части
- Документ: ключевые поля (Ref_Key, Number, Date, Posted) — присутствуют.
- Продукция: ожидаемый Этап_Key В ПРИМЕРЕ ОТСУТСТВУЕТ (missing), при этом есть СтруктурнаяЕдиница_Key и др.
- Запасы: ожидаемые поля (Номенклатура_Key, Количество, Спецификация_Key, Этап_Key) — присутствуют.
- Операции: ожидаемые поля (Операция_Key, КоличествоПлан, НормаВремени, Нормочасы, Этап_Key) — присутствуют.
- Вывод: в табличной части «Продукция» Этап_Key может быть необязателен/отсутствовать в конкретной базе. Документацию стоит пометить: «Этап_Key — опционально».

6) Document_ЗаказПоставщику и _Запасы
- Документ: (Ref_Key, Number, Date, Posted, Контрагент_Key, СуммаДокумента) — присутствуют; множество доп. полей — норма.
- Запасы: (Номенклатура_Key, Количество, Цена, Сумма, ДатаПоступления) — присутствуют.
- Вывод: описание актуально.

7) InformationRegister_СпецификацииПоУмолчанию
- (Номенклатура_Key, Характеристика_Key, Спецификация_Key) — присутствуют.
- Вывод: описание актуально.

Предлагаемые правки для [docs/сущности_1С.md](docs/сущности_1С.md) — как патч (к применению в отдельной сессии):
- Раздел «AccumulationRegister_ЗапасыНаСкладах»:
  - Изменить описание механики выборки: «Остатки читаются через ресурс регистра “Остатки” (Balance), а не через набор записей регистра».
  - Добавить пример запроса:
    - GET {base}/AccumulationRegister_ЗапасыНаСкладах/Balance?$select=Номенклатура_Key,Склад_Key,КоличествоОстаток&amp;$top=5
  - Сноска: для получения кодов/наименований по Номенклатура_Key требуется $expand или отдельный батч к Catalog_Номенклатура (см. [OData1CClient.get_nomenclature_codes()](src/odata_client.py:135)).
- Раздел «Document_ЗаказНаПроизводство → Продукция»:
  - Пометить поле Этап_Key как «опционально (может отсутствовать)».
- Раздел «Catalog_Спецификации»:
  - Явно указать, что состав и операции доступны в наборах Catalog_Спецификации_Состав и Catalog_Спецификации_Операции; внутри основной сущности — навигации.

Подготовка БД для полного сопоставления (DDL‑план, без применения):
1) Дополнения к items:
```sql
ALTER TABLE items ADD COLUMN item_ref1c TEXT UNIQUE;           -- Ref_Key 1С
ALTER TABLE items ADD COLUMN replenishment_method TEXT;         -- СпособПополнения
ALTER TABLE items ADD COLUMN replenishment_time INTEGER;        -- СрокПополнения (дни)
-- Уже есть: unit TEXT, item_article TEXT, stock_qty REAL
CREATE INDEX IF NOT EXISTS ix_items_ref1c ON items(item_ref1c);
```

2) Склады и текущие остатки:
```sql
CREATE TABLE IF NOT EXISTS warehouses (
  warehouse_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  warehouse_ref1c  TEXT UNIQUE NOT NULL,
  warehouse_code   TEXT,
  warehouse_name   TEXT
);

CREATE TABLE IF NOT EXISTS stock (
  item_id      INTEGER NOT NULL,
  warehouse_id INTEGER NOT NULL,
  quantity     REAL NOT NULL DEFAULT 0.0,
  PRIMARY KEY (item_id, warehouse_id),
  FOREIGN KEY(item_id)      REFERENCES items(item_id) ON DELETE CASCADE,
  FOREIGN KEY(warehouse_id) REFERENCES warehouses(warehouse_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_stock_item ON stock(item_id);
CREATE INDEX IF NOT EXISTS ix_stock_wh   ON stock(warehouse_id);
```
Примечание: поле items.stock_qty можно оставить как агрегат по всем складам; таблица stock — детализированная по складам.

3) Спецификации и их состав/операции:
```sql
CREATE TABLE IF NOT EXISTS specifications (
  spec_ref1c  TEXT PRIMARY KEY,     -- Ref_Key 1С
  spec_code   TEXT,
  spec_name   TEXT,
  owner_item_id INTEGER,            -- опционально: владелец/продукт
  FOREIGN KEY(owner_item_id) REFERENCES items(item_id) ON DELETE SET NULL
);

-- Укажем привязку 1С стадий к нашим этапам:
ALTER TABLE production_stages ADD COLUMN stage_ref1c TEXT UNIQUE; -- GUID 1С (если отсутствует)

CREATE TABLE IF NOT EXISTS spec_components (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  spec_ref1c        TEXT NOT NULL,
  parent_item_id    INTEGER,                 -- владелец (изделие)
  component_item_id INTEGER NOT NULL,
  quantity          REAL NOT NULL,
  stage_ref1c       TEXT,                    -- Этап_Key 1С (сырой GUID)
  stage_id          INTEGER,                 -- сопоставленный этап нашей БД (если есть)
  component_type    TEXT,                    -- ТипСтрокиСостава
  FOREIGN KEY(spec_ref1c)        REFERENCES specifications(spec_ref1c) ON DELETE CASCADE,
  FOREIGN KEY(parent_item_id)    REFERENCES items(item_id)            ON DELETE SET NULL,
  FOREIGN KEY(component_item_id) REFERENCES items(item_id)            ON DELETE CASCADE,
  FOREIGN KEY(stage_id)          REFERENCES production_stages(stage_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS spec_operations (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  spec_ref1c  TEXT NOT NULL,
  operation_ref1c TEXT,             -- Операция_Key 1С (GUID)
  time_norm   REAL,                 -- НормаВремени
  stage_ref1c TEXT,
  stage_id    INTEGER,
  FOREIGN KEY(spec_ref1c) REFERENCES specifications(spec_ref1c) ON DELETE CASCADE,
  FOREIGN KEY(stage_id)   REFERENCES production_stages(stage_id) ON DELETE SET NULL
);
```
Примечание: существующую таблицу bom можно заполнять из активной спецификации (по умолчанию) как производную (для планирования), сохраняя при этом «источник правды» в spec_components/spec_operations.

4) Заказы на производство:
```sql
CREATE TABLE IF NOT EXISTS production_orders (
  order_ref1c TEXT PRIMARY KEY,   -- Ref_Key 1С
  order_number TEXT,
  order_date   TEXT,
  is_posted    INTEGER            -- 0/1
);

CREATE TABLE IF NOT EXISTS production_products (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  order_ref1c  TEXT NOT NULL,
  item_id      INTEGER NOT NULL,
  quantity     REAL NOT NULL,
  spec_ref1c   TEXT,
  stage_ref1c  TEXT,
  stage_id     INTEGER,
  FOREIGN KEY(order_ref1c) REFERENCES production_orders(order_ref1c) ON DELETE CASCADE,
  FOREIGN KEY(item_id)     REFERENCES items(item_id)                ON DELETE CASCADE,
  FOREIGN KEY(spec_ref1c)  REFERENCES specifications(spec_ref1c)    ON DELETE SET NULL,
  FOREIGN KEY(stage_id)    REFERENCES production_stages(stage_id)   ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS production_components (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  order_ref1c  TEXT NOT NULL,
  item_id      INTEGER NOT NULL,
  quantity     REAL NOT NULL,
  spec_ref1c   TEXT,
  stage_ref1c  TEXT,
  stage_id     INTEGER,
  FOREIGN KEY(order_ref1c) REFERENCES production_orders(order_ref1c) ON DELETE CASCADE,
  FOREIGN KEY(item_id)     REFERENCES items(item_id)                ON DELETE CASCADE,
  FOREIGN KEY(spec_ref1c)  REFERENCES specifications(spec_ref1c)    ON DELETE SET NULL,
  FOREIGN KEY(stage_id)    REFERENCES production_stages(stage_id)   ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS production_operations (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  order_ref1c      TEXT NOT NULL,
  operation_ref1c  TEXT,        -- GUID операции 1С
  planned_quantity REAL,
  time_norm        REAL,
  standard_hours   REAL,
  stage_ref1c      TEXT,
  stage_id         INTEGER,
  FOREIGN KEY(order_ref1c) REFERENCES production_orders(order_ref1c) ON DELETE CASCADE,
  FOREIGN KEY(stage_id)    REFERENCES production_stages(stage_id)   ON DELETE SET NULL
);
```

5) Заказы поставщикам:
```sql
CREATE TABLE IF NOT EXISTS supplier_orders (
  order_ref1c   TEXT PRIMARY KEY,
  order_number  TEXT,
  order_date    TEXT,
  is_posted     INTEGER,
  supplier_ref1c TEXT,           -- Контрагент_Key (GUID 1С)
  document_amount REAL
);

CREATE TABLE IF NOT EXISTS supplier_order_items (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  order_ref1c   TEXT NOT NULL,
  item_id       INTEGER NOT NULL,
  quantity      REAL NOT NULL,
  price         REAL,
  amount        REAL,
  delivery_date TEXT,
  FOREIGN KEY(order_ref1c) REFERENCES supplier_orders(order_ref1c) ON DELETE CASCADE,
  FOREIGN KEY(item_id)     REFERENCES items(item_id)              ON DELETE CASCADE
);
```

6) Спецификации по умолчанию:
```sql
CREATE TABLE IF NOT EXISTS default_specifications (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id          INTEGER NOT NULL,
  characteristic_ref1c TEXT,   -- Характеристика_Key
  spec_ref1c       TEXT NOT NULL,
  FOREIGN KEY(item_id)    REFERENCES items(item_id)           ON DELETE CASCADE,
  FOREIGN KEY(spec_ref1c) REFERENCES specifications(spec_ref1c) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_defspec_item_char
  ON default_specifications(item_id, characteristic_ref1c);
```

Применение этого плана обеспечит полное сопоставление записей, указанное в [docs/сущности_1С.md](docs/сущности_1С.md), при этом:
- GUID 1С хранятся в текстовых колонках *_ref1c (или *_Key), не ломая текущие integer PK.
- Связь этапов с 1С обеспечивается через production_stages.stage_ref1c.
- Текущие агрегаты (items.stock_qty) сохраняются; деталь по складам — в новой таблице stock.

Дальнейшие шаги:
- Актуализировать [docs/сущности_1С.md](docs/сущности_1С.md) по пунктам 3 и 5 выше (в отдельной сессии).
- Уточнить и протестировать точное имя ресурса остатков («Balance»/«Остатки») по $metadata для регистра ЗапасыНаСкладах и обновить клиент [src/odata_client.py](src/odata_client.py) для работы через ресурс остатков.
- Согласовать и утвердить схему БД из этого плана, затем подготовить миграции.
## 2025-09-18 — Выгрузка групп номенклатуры (NiceGUI) и окно выбора групп

Сделано:
- Реализован backend-эндпоинт POST /api/odata/categories/export_groups в файле [`src/ui_nicegui/app.py`](src/ui_nicegui/app.py). Эндпоинт выполняет запрос к 1С OData:
  - URL: {base}/Catalog_Номенклатура?$format=json&amp;$filter=IsFolder eq true
  - Аутентификация: Basic (параметры берутся из [`config/odata_config.json`](config/odata_config.json))
  - Fallback декодирования cp1251 при некорректной кириллице; результат сериализуется в UTF‑8.
  - Результат сохраняется в [`output/odata_groups_nomenclature.json`](output/odata_groups_nomenclature.json).
  - Возвращается JSON со статистикой: {status, output, total, groups_count}.

- На странице «Настройки синхронизации 1С» добавлена кнопка запуска выгрузки и индикация прогресса в файле [`src/ui_nicegui/routes.py`](src/ui_nicegui/routes.py):
  - Кнопка «Выгрузить группы номенклатуры»: вызывает POST /api/odata/categories/export_groups и показывает диалог с ui.linear_progress (indeterminate).
  - Уведомление по завершении с количеством выгруженных записей.

- Добавлен раздел «Группы номенклатуры (сохранённые)» на странице настроек: отображает загруженные группы и позволяет выбрать отмеченные галочками группы для индексации.
  - Источник данных: [`output/odata_groups_nomenclature.json`](output/odata_groups_nomenclature.json).
  - Управление выбором:
    - «Выбрать все» / «Снять все».
    - «Сохранить выбор» — записывает выбранные Ref_Key в [`config/odata_groups_selected.json`](config/odata_groups_selected.json).
  - Список отсортирован по Code → Description.

Примечания по кодировке:
- На части серверов 1С OData ответы JSON приходят в кодировке Windows‑1251. Эндпоинт выполняет попытку r.json(); при ошибке — принудительное декодирование в cp1251 и повторный разбор с сохранением результата в UTF‑8.

Как пользоваться:
1) Откройте «Настройки синхронизации 1С» (/settings/odata) — файл страницы [`src/ui_nicegui/routes.py`](src/ui_nicegui/routes.py).
2) Нажмите кнопку «Выгрузить группы номенклатуры».
3) После успешной выгрузки разверните блок «Группы номенклатуры (сохранённые)», отметьте нужные группы и нажмите «Сохранить выбор» для записи в [`config/odata_groups_selected.json`](config/odata_groups_selected.json).

Артефакты:
- JSON с группами: [`output/odata_groups_nomenclature.json`](output/odata_groups_nomenclature.json)
- Выбранные группы (Ref_Key): [`config/odata_groups_selected.json`](config/odata_groups_selected.json)
## 2025-09-18 — UI NiceGUI: прогресс-бар выгрузки групп 1С и обновление раздела

Сделано:
- Прогресс-бар при запуске «Выгрузить группы номенклатуры» на странице «Настройки синхронизации 1С» переведен на детерминированный HTML progress с процентами и гарантированным автозакрытием диалога по завершении. Реализация: обновлен блок диалога и JS-цепочка в [_export_groups()](src/ui_nicegui/routes.py:356), диалог создается в [odata_settings_page()](src/ui_nicegui/routes.py:350). Ключевые изменения: [замена ui.linear_progress на &lt;progress&gt; + %](src/ui_nicegui/routes.py:350) и [пошаговое обновление 5%→15%→60%→90%→100% c двойным закрытием (event + timeout)](src/ui_nicegui/routes.py:359).
- Переименован раздел со списком сохраненных групп на «Группы номенклатуры для индексации»: [expansion-заголовок](src/ui_nicegui/routes.py:392).
- Отрисовка списка групп сделана жестко в одну колонку для удобства восприятия (ui.column + gap): [контейнер списка](src/ui_nicegui/routes.py:455).

Пояснения:
- Бэкенд-эндпоинт выгрузки групп не изменялся: [api_odata_export_groups](src/ui_nicegui/app.py:303) — логика обращения к 1С и сохранение файла остаются прежними.
- При ручном тесте наблюдалась разовая ошибка на странице «TypeError: Cannot read properties of undefined (reading 'notify')» — вероятно, в окружении теста отсутствует глобальный объект `window.$nicegui`. На штатном запуске NiceGUI тосты работают. При необходимости возможна точечная доработка JS-уведомлений: заменить вызовы на `window.$nicegui?.notify?.(...) ?? console.log(...)`.

Проверка:
- Локальный запуск: `python -m src.ui_nicegui.app` → http://localhost:8080/settings/odata
- Нажать «Выгрузить группы номенклатуры»:
  - Появляется диалог «Выгрузка групп номенклатуры…» с прогрессом и процентами.
  - Прогресс достигает 100% и диалог закрывается автоматически.
  - Раздел «Группы номенклатуры для индексации» доступен ниже; список — в одну колонку.

Файлы затронуты:
- [src/ui_nicegui/routes.py](src/ui_nicegui/routes.py:350)