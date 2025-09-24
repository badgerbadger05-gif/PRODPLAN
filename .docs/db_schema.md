# Схема базы данных PRODPLAN

**Версия:** 1.0  
**Дата:** 2025-09-24

## Описание

Этот документ описывает схему базы данных PostgreSQL для проекта PRODPLAN. Система мигрирует с SQLite на PostgreSQL с разделением на бэкенд (FastAPI) и фронтенд (Quasar).

## ER-диаграмма (описание связей)

```
┌─────────────────────┐    ┌─────────────────────┐
│   production_stages │    │        items        │
│                     │    │                     │
│ stage_id (PK)       │◄───┤ stage_id (FK)       │
│ stage_name          │    │ item_id (PK)        │
│ stage_order         │    │ item_code (UNIQUE)  │
└─────────────────────┘    │ item_name           │
                           │ stock_qty           │
                           │ status              │
                           │ created_at          │
                           │ updated_at          │
                           └─────────────────────┘
                                    │
                                    │
                                    ▼
┌─────────────────────┐    ┌─────────────────────┐
│      units          │    │         bom         │
│                     │    │                     │
│ unit_id (PK)        │    │ bom_id (PK)         │
│ unit_ref1c          │◄───┤ parent_item_id (FK) │
│ unit_code           │    │ child_item_id (FK)  │
│ unit_name           │    │ quantity            │
│ unit_full_name      │    │ link_stage_id (FK)  │
│ short_name          │    │ created_at          │
│ iso_code            │    │ updated_at          │
│ base_unit_ref1c (FK)│    └─────────────────────┘
│ ratio               │
│ precision           │
│ created_at          │
│ updated_at          │
└─────────────────────┘

┌─────────────────────┐    ┌─────────────────────┐
│   production_plan_  │    │   stock_history     │
│      entries        │    │                     │
│                     │    │ id (PK)             │
│ id (PK)             │    │ item_code (FK)      │
│ item_id (FK)        │    │ stock_qty           │
│ stage_id (FK)       │    │ recorded_at         │
│ date                │    └─────────────────────┘
│ planned_qty         │
│ completed_qty       │    ┌─────────────────────┐
│ status              │    │  default_specifica- │
│ notes               │    │      tions          │
│ updated_at          │    │                     │
└─────────────────────┘    │ id (PK)             │
                           │ item_ref1c          │
                           │ spec_ref1c          │
                           │ created_at          │
                           │ updated_at          │
                           └─────────────────────┘

┌─────────────────────┐    ┌─────────────────────┐
│   specifications    │    │   spec_operations   │
│                     │    │                     │
│ spec_id (PK)        │    │ spec_op_id (PK)     │
│ spec_code           │    │ spec_id (FK)        │
│ spec_name           │    │ operation_ref1c     │
│ item_ref1c          │    │ stage_id (FK)       │
│ created_at          │    │ time_norm_nh        │
│ updated_at          │    │ created_at          │
│                      │    │ updated_at          │
└─────────────────────┘    └─────────────────────┘

┌─────────────────────┐    ┌─────────────────────┐
│    operations       │    │   spec_components   │
│                     │    │                     │
│ operation_id (PK)   │    │ comp_id (PK)        │
│ operation_ref1c     │    │ spec_id (FK)        │
│ operation_name      │    │ item_ref1c          │
│ created_at          │    │ quantity            │
│ updated_at          │    │ unit_ref1c          │
└─────────────────────┘    │ link_stage_id (FK)  │
                           │ created_at          │
                           │ updated_at          │
                           └─────────────────────┘
```

## Таблицы

### 1. production_stages — Этапы производства
```sql
CREATE TABLE production_stages (
  stage_id     SERIAL PRIMARY KEY,
  stage_name   VARCHAR(255) UNIQUE NOT NULL,
  stage_order  INTEGER
);
```
Словарь возможных этапов производства.

### 2. items — Номенклатура
```sql
CREATE TABLE items (
  item_id          SERIAL PRIMARY KEY,
  item_code        VARCHAR(255) UNIQUE NOT NULL,
  item_name        VARCHAR(500) NOT NULL,
  stage_id         INTEGER,
  stock_qty        DECIMAL(15,3) DEFAULT 0.0,
  status           VARCHAR(50) DEFAULT 'active',
  replenishment_method VARCHAR(50), -- Закупка, Переработка и т.д.
  created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(stage_id) REFERENCES production_stages(stage_id)
);
```
Справочник номенклатуры из 1С.

### 3. units — Единицы измерения
```sql
CREATE TABLE units (
  unit_id          SERIAL PRIMARY KEY,
  unit_ref1c       VARCHAR(255) UNIQUE NOT NULL,
  unit_code        VARCHAR(50),
  unit_name        VARCHAR(255),
  unit_full_name   VARCHAR(500),
  short_name       VARCHAR(50),
  iso_code         VARCHAR(20),
  base_unit_ref1c  VARCHAR(255),
  ratio            DECIMAL(10,6),
  precision        INTEGER,
  created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(base_unit_ref1c) REFERENCES units(unit_ref1c)
);
```
Справочник единиц измерения из 1С.

### 4. bom — Спецификации (Bill of Materials)
```sql
CREATE TABLE bom (
  bom_id          SERIAL PRIMARY KEY,
  parent_item_id INTEGER NOT NULL,
  child_item_id   INTEGER NOT NULL,
  quantity        DECIMAL(15,3) NOT NULL,
  link_stage_id   INTEGER,
  created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(parent_item_id) REFERENCES items(item_id),
  FOREIGN KEY(child_item_id)  REFERENCES items(item_id),
  FOREIGN KEY(link_stage_id)  REFERENCES production_stages(stage_id)
);
```
Структура спецификаций (состав изделий).

### 5. specifications — Спецификации (из 1С)
```sql
CREATE TABLE specifications (
  spec_id         SERIAL PRIMARY KEY,
 spec_code       VARCHAR(255),
  spec_name       VARCHAR(500),
  item_ref1c      VARCHAR(255),  -- Ссылка на изделие в 1С
  created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```
Спецификации из 1С (Catalog_Спецификации).

### 6. spec_components — Компоненты спецификаций
```sql
CREATE TABLE spec_components (
  comp_id         SERIAL PRIMARY KEY,
  spec_id         INTEGER NOT NULL,
  item_ref1c      VARCHAR(255) NOT NULL,  -- Ссылка на компонент в 1С
  quantity        DECIMAL(15,3) NOT NULL,
  unit_ref1c      VARCHAR(255),  -- Ссылка на единицу измерения
  link_stage_id   INTEGER,       -- Этап производства
  created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(spec_id) REFERENCES specifications(spec_id),
  FOREIGN KEY(link_stage_id) REFERENCES production_stages(stage_id),
  FOREIGN KEY(unit_ref1c) REFERENCES units(unit_ref1c)
);
```
Компоненты спецификаций (Catalog_Спецификации_Состав).

### 7. operations — Операции (из 1С)
```sql
CREATE TABLE operations (
  operation_id     SERIAL PRIMARY KEY,
  operation_ref1c  VARCHAR(255) UNIQUE NOT NULL,
  operation_name   VARCHAR(500),  -- Наименование операции
  created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```
Справочник операций (получаемый через навигацию из спецификаций).

### 8. spec_operations — Операции в спецификациях
```sql
CREATE TABLE spec_operations (
  spec_op_id      SERIAL PRIMARY KEY,
 spec_id         INTEGER NOT NULL,
  operation_ref1c VARCHAR(255) NOT NULL,
  stage_id        INTEGER,       -- Этап производства
  time_norm_nh    DECIMAL(10,2), -- Норма времени в человеко-часах
  created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(spec_id) REFERENCES specifications(spec_id),
  FOREIGN KEY(stage_id) REFERENCES production_stages(stage_id)
);
```
Операции в спецификациях (Catalog_Спецификации_Операции).

### 9. default_specifications — Спецификации по умолчанию
```sql
CREATE TABLE default_specifications (
  id              SERIAL PRIMARY KEY,
  item_ref1c      VARCHAR(255) NOT NULL,  -- Ссылка на изделие в 1С
  spec_ref1c      VARCHAR(255) NOT NULL, -- Ссылка на спецификацию в 1С
  created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```
Связь изделий с их спецификациями по умолчанию (InformationRegister_СпецификацииПоУмолчанию).

### 10. production_plan_entries — Планы производства
```sql
CREATE TABLE production_plan_entries (
  id              SERIAL PRIMARY KEY,
  item_id         INTEGER NOT NULL,
  stage_id        INTEGER,
  date            DATE NOT NULL,
  planned_qty     DECIMAL(15,3) NOT NULL DEFAULT 0.0,
  completed_qty   DECIMAL(15,3) NOT NULL DEFAULT 0.0,
  status          VARCHAR(20) NOT NULL DEFAULT 'GREEN',
  notes           TEXT,
  updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(item_id) REFERENCES items(item_id),
  FOREIGN KEY(stage_id) REFERENCES production_stages(stage_id)
);
```
Планы производства по изделиям и этапам.

### 11. stock_history — История остатков
```sql
CREATE TABLE stock_history (
  id              SERIAL PRIMARY KEY,
  item_code       VARCHAR(255) NOT NULL,
  stock_qty       DECIMAL(15,3) NOT NULL,
  recorded_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(item_code) REFERENCES items(item_code)
);
```
История изменения остатков по номенклатуре (хранится 30 дней).

## Индексы для производительности

```sql
-- Индексы для быстрых запросов
CREATE UNIQUE INDEX idx_bom_parent_child ON bom(parent_item_id, child_item_id);
CREATE INDEX idx_plan_stage_date ON production_plan_entries(stage_id, date);
CREATE INDEX idx_stock_history_item_date ON stock_history(item_code, recorded_at);
CREATE INDEX idx_items_code ON items(item_code);
CREATE INDEX idx_items_stage ON items(stage_id);
CREATE INDEX idx_specifications_code ON specifications(spec_code);
```

## Служебные таблицы

- `root_products` — Корневые изделия (определяются автоматно)
- `user_orders` — Пользовательские заказы
- `import_batches` — История импорта данных

### 12. production_resources — Производственные участки
```sql
CREATE TABLE production_resources (
  resource_id     SERIAL PRIMARY KEY,
  resource_name   VARCHAR(255) NOT NULL,
  shift_offset    INTEGER DEFAULT 0,      -- Сдвиг планирования
  planning_range  INTEGER DEFAULT 30,     -- Диапазон планирования в днях
  capacity        DECIMAL(10,3) DEFAULT 0.0, -- Мощность
  work_schedule   VARCHAR(50) DEFAULT '5/2', -- График работы
  daily_work_hours DECIMAL(4,2) DEFAULT 8.0, -- Рабочее время в часах в сутки
  created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### 13. resource_stages — Привязка этапов к участкам
```sql
CREATE TABLE resource_stages (
  id              SERIAL PRIMARY KEY,
  resource_id     INTEGER NOT NULL,
  stage_id        INTEGER NOT NULL,
  created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(resource_id) REFERENCES production_resources(resource_id),
  FOREIGN KEY(stage_id) REFERENCES production_stages(stage_id)
);
```

## Индексы для производительности

```sql
-- Индексы для быстрых запросов
CREATE UNIQUE INDEX idx_bom_parent_child ON bom(parent_item_id, child_item_id);
CREATE INDEX idx_plan_stage_date ON production_plan_entries(stage_id, date);
CREATE INDEX idx_stock_history_item_date ON stock_history(item_code, recorded_at);
CREATE INDEX idx_items_code ON items(item_code);
CREATE INDEX idx_items_stage ON items(stage_id);
CREATE INDEX idx_specifications_code ON specifications(spec_code);
CREATE INDEX idx_resource_stages_resource ON resource_stages(resource_id);
CREATE INDEX idx_resource_stages_stage ON resource_stages(stage_id);
```

---

# Сущности 1С и маппинг данных

Этот раздел описывает сущности, получаемые из OData API 1С, и их сопоставление с таблицами в базе данных PRODPLAN.

### Основные сущности

#### 1. Catalog_Номенклатура

Сущность содержит информацию о номенклатуре товаров.

##### Поля:

| Поле | Тип | Описание |
|------|-----|----------|
| Ref_Key | GUID | Уникальный идентификатор номенклатуры |
| Code | Строка | Код номенклатуры |
| Description | Строка | Наименование номенклатуры |
| Артикул | Строка | Артикул изделия |
| СпособПополнения | Строка | Способ пополнения (Закупка, Производство и т.д.) |
| СрокПополнения | Число | Срок пополнения в днях |
| ЕдиницаИзмерения_Key | GUID | Ссылка на единицу измерения |
| КатегорияНоменклатуры_Key | GUID | Ссылка на категорию номенклатуры |
| ТипНоменклатуры | Строка | Тип номенклатуры (Запас, Услуга и т.д.) |

#### 1.1. Catalog_КатегорииНоменклатуры

Сущность содержит информацию о категориях номенклатуры. Вся иерархия категорий номенклатуры хранится в одном справочнике.
Поле IsFolder разделяет два типа элементов:
- IsFolder: true - "Группы категорий номенклатуры" (папки)
- IsFolder: false - "Категории номенклатуры" (конечные категории)

##### Поля:

| Поле | Тип | Описание |
|------|-----|----------|
| Ref_Key | GUID | Уникальный идентификатор категории |
| Code | Строка | Код категории |
| Description | Строка | Наименование категории |
| Parent_Key | GUID | Ссылка на родительскую категорию |
| IsFolder | Булево | Признак группы категорий (true - группа, false - категория) |
| Predefined | Булево | Признак предопределенной категории |
| PredefinedDataName | Строка | Имя предопределенной категории |
| DataVersion | Строка | Версия данных |
| DeletionMark | Булево | Признак удаления |

#### 3. AccumulationRegister_ЗапасыНаСкладах

Сущность содержит информацию об остатках на складах.

ВНИМАНИЕ: Остатки читаются через ресурс регистра "Остатки" (Balance), а не через набор записей регистра.
Пример запроса к остатку:
- GET {base}/AccumulationRegister_ЗапасыНаСкладах/Balance?$select=Номенклатура_Key,Склад_Key,КоличествоОстаток&$top=5

Для получения полей номенклатуры по ссылкам (код/наименование/артикул) используйте $expand или отдельный батч‑запрос к Catalog_Номенклатура.

##### Поля (ресурс Остатки/Balance):

| Поле | Тип | Описание |
|------|-----|----------|
| Номенклатура_Key | GUID | Ссылка на номенклатуру |
| Склад_Key | GUID | Ссылка на склад |
| КоличествоОстаток | Число | Остаток количества |
| Номенклатура/Code | Строка | Код номенклатуры (через расширение) |
| Номенклатура/Description | Строка | Наименование номенклатуры (через расширение) |
| Номенклатура/Артикул | Строка | Артикул номенклатуры (через расширение) |

Примечание: если обращаться к базовому набору регистра без ресурса остатков, сервер вернёт движения с полями RecordSet/Recorder, а не срез остатков.

#### 4. Catalog_Спецификации

Сущность содержит информацию о спецификациях изделий, включая состав и операции.

Примечание: детальные строки состава и операций доступны отдельными наборами:
- Catalog_Спецификации_Состав — строки состава (компоненты),
- Catalog_Спецификации_Операции — строки операций.
В самой Catalog_Спецификации поля "Состав" и "Операции" представлены как навигации.

##### Поля:

| Поле | Тип | Описание |
|------|-----|----------|
| Ref_Key | GUID | Уникальный идентификатор спецификации |
| Code | Строка | Код спецификации |
| Description | Строка | Наименование спецификации |
| Состав | Массив | Состав спецификации (номенклатура, количество, этапы) |
| Операции | Массив | Операции по производству |

##### Поля состава:

| Поле | Тип | Описание |
|------|-----|----------|
| Номенклатура_Key | GUID | Ссылка на номенклатуру компонента |
| Количество | Число | Количество компонента |
| Этап_Key | GUID | Ссылка на этап производства |
| ТипСтрокиСостава | Строка | Тип строки (Материал, Сборка) |

##### Поля операций:

| Поле | Тип | Описание |
|------|-----|----------|
| Операция_Key | GUID | Ссылка на операцию |
| НормаВремени | Число | Норма времени на операцию |
| Этап_Key | GUID | Ссылка на этап производства |

#### 5. Document_ЗаказНаПроизводство

Сущность содержит информацию о заказах на производство.

##### Поля:

| Поле | Тип | Описание |
|------|-----|----------|
| Ref_Key | GUID | Уникальный идентификатор заказа |
| Number | Строка | Номер заказа |
| Date | Дата | Дата заказа |
| Posted | Булево | Проведен ли документ |
| Продукция | Массив | Список продукции для производства |
| Запасы | Массив | Список запасов (компонентов) |
| Операции | Массив | Операции по производству |

##### Поля продукции:

| Поле | Тип | Описание |
|------|-----|----------|
| Номенклатура_Key | GUID | Ссылка на номенклатуру продукции |
| Количество | Число | Количество продукции |
| Спецификация_Key | GUID | Ссылка на спецификацию |
| Этап_Key | GUID | Ссылка на этап производства (опционально) |

##### Поля запасов (компонентов):

| Поле | Тип | Описание |
|------|-----|----------|
| Номенклатура_Key | GUID | Ссылка на номенклатуру компонента |
| Количество | Число | Количество компонента |
| Спецификация_Key | GUID | Ссылка на спецификацию |
| Этап_Key | GUID | Ссылка на этап производства |

##### Поля операций:

| Поле | Тип | Описание |
|------|-----|----------|
| Операция_Key | GUID | Ссылка на операцию |
| КоличествоПлан | Число | Планируемое количество |
| НормаВремени | Число | Норма времени на операцию |
| Нормочасы | Число | Нормочасы |
| Этап_Key | GUID | Ссылка на этап производства |

#### 6. Document_ЗаказПоставщику

Сущность содержит информацию о заказах поставщикам.

##### Поля:

| Поле | Тип | Описание |
|------|-----|----------|
| Ref_Key | GUID | Уникальный идентификатор заказа |
| Number | Строка | Номер заказа |
| Date | Дата | Дата заказа |
| Posted | Булево | Проведен ли документ |
| Контрагент_Key | GUID | Ссылка на контрагента (поставщика) |
| СуммаДокумента | Число | Сумма документа |
| Запасы | Массив | Список запасов (номенклатуры) в заказе |

##### Поля запасов (номенклатуры в заказе):

| Поле | Тип | Описание |
|------|-----|----------|
| Номенклатура_Key | GUID | Ссылка на номенклатуру |
| Количество | Число | Количество номенклатуры |
| Цена | Число | Цена номенклатуры |
| Сумма | Число | Сумма позиции |
| ДатаПоступления | Дата | Планируемая дата поступления |

#### 7. InformationRegister_СпецификацииПоУмолчанию

Сущность содержит информацию о спецификациях по умолчанию для номенклатуры.

##### Поля:

| Поле | Тип | Описание |
|------|-----|----------|
| Номенклатура_Key | GUID | Ссылка на номенклатуру |
| Характеристика_Key | GUID | Ссылка на характеристику |
| Спецификация_Key | GUID | Ссылка на спецификацию по умолчанию |

### Сопоставление с собственной базой данных

#### Таблица items (номенклатура)

| Поле 1С | Поле нашей БД | Описание |
|---------|---------------|----------|
| Ref_Key | item_ref1c | GUID 1С (уникальный идентификатор номенклатуры) |
| Code | item_code | Код номенклатуры |
| Description | item_name | Наименование номенклатуры |
| Артикул | item_article | Артикул изделия |
| СпособПополнения | replenishment_method | Способ пополнения |
| СрокПополнения | replenishment_time | Срок пополнения в днях |

#### Таблица stock (остатки)

| Поле 1С | Поле нашей БД | Описание |
|---------------|----------|
| Номенклатура_Key | item_id | Ссылка на номенклатуру |
| Склад_Key | warehouse_id | Ссылка на склад |
| КоличествоОстаток | quantity | Остаток количества |

#### Таблица specifications (спецификации)

| Поле 1С | Поле нашей БД | Описание |
|---------|---------------|----------|
| Ref_Key | spec_id | Уникальный идентификатор спецификации |
| Code | spec_code | Код спецификации |
| Description | spec_name | Наименование спецификации |

#### Таблица spec_components (компоненты спецификаций)

| Поле 1С | Поле нашей БД | Описание |
|---------|---------------|----------|
| Номенклатура_Key (в составе) | component_item_id | Ссылка на номенклатуру компонента |
| Номенклатура_Key (владелец) | parent_item_id | Ссылка на номенклатуру изделия |
| Количество | quantity | Количество компонента |
| Этап_Key | stage_id | Ссылка на этап производства |
| ТипСтрокиСостава | component_type | Тип компонента (Материал, Сборка) |

#### Таблица operations (операции)

| Поле 1С | Поле нашей БД | Описание |
|---------|---------------|----------|
| Операция_Key | operation_id | Уникальный идентификатор операции |
| НормаВремени | time_norm | Норма времени на операцию |
| Этап_Key | stage_id | Ссылка на этап производства |

#### Таблица production_orders (заказы на производство)

| Поле 1С | Поле нашей БД | Описание |
|---------|---------------|----------|
| Ref_Key | order_id | Уникальный идентификатор заказа |
| Number | order_number | Номер заказа |
| Date | order_date | Дата заказа |
| Posted | is_posted | Проведен ли документ |

#### Таблица production_products (продукция в заказах)

| Поле 1С | Поле нашей БД | Описание |
|---------|---------------|----------|
| Номенклатура_Key | item_id | Ссылка на номенклатуру продукции |
| Количество | quantity | Количество продукции |
| Спецификация_Key | spec_id | Ссылка на спецификацию |
| Этап_Key | stage_id | Ссылка на этап производства |

#### Таблица production_components (компоненты в заказах)

| Поле 1С | Поле нашей БД | Описание |
|---------|---------------|----------|
| Номенклатура_Key | item_id | Ссылка на номенклатуру компонента |
| Количество | quantity | Количество компонента |
| Спецификация_Key | spec_id | Ссылка на спецификацию |
| Этап_Key | stage_id | Ссылка на этап производства |

#### Таблица production_operations (операции в заказах)

| Поле 1С | Поле нашей БД | Описание |
|---------|---------------|----------|
| Операция_Key | operation_id | Ссылка на операцию |
| КоличествоПлан | planned_quantity | Планируемое количество |
| НормаВремени | time_norm | Норма времени на операцию |
| Нормочасы | standard_hours | Нормочасы |
| Этап_Key | stage_id | Ссылка на этап производства |

#### Таблица supplier_orders (заказы поставщикам)

| Поле 1С | Поле нашей БД | Описание |
|---------|---------------|----------|
| Ref_Key | order_id | Уникальный идентификатор заказа |
| Number | order_number | Номер заказа |
| Date | order_date | Дата заказа |
| Posted | is_posted | Проведен ли документ |
| Контрагент_Key | supplier_id | Ссылка на контрагента (поставщика) |
| СуммаДокумента | document_amount | Сумма документа |

#### Таблица supplier_order_items (позиции в заказах поставщикам)

| Поле 1С | Поле нашей БД | Описание |
|---------|---------------|----------|
| Номенклатура_Key | item_id | Ссылка на номенклатуру |
| Количество | quantity | Количество номенклатуры |
| Цена | price | Цена номенклатуры |
| Сумма | amount | Сумма позиции |
| ДатаПоступления | delivery_date | Планируемая дата поступления |

#### Таблица default_specifications (спецификации по умолчанию)

| Поле 1С | Поле нашей БД | Описание |
|---------------|----------|
| Номенклатура_Key | item_id | Ссылка на номенклатуру |
| Характеристика_Key | characteristic_id | Ссылка на характеристику |
| Спецификация_Key | spec_id | Ссылка на спецификацию по умолчанию |

### Примечания

1. Для получения полной информации о номенклатуре может потребоваться использование $expand в запросах OData.
2. При синхронизации данных необходимо учитывать возможные различия в форматах данных между 1С и нашей базой.
3. Необходимо реализовать механизм обработки изменений в данных 1С для поддержания актуальности нашей базы.
4. Спецификации содержат сложную структуру с вложенными массивами, требующими особой обработки при импорте.
5. Заказы на производство содержат сложную структуру с несколькими вложенными массивами (продукция, запасы, операции), требующими особой обработки при импорте.
6. Заказы поставщикам содержат информацию о закупаемой номенклатуре, количествах, ценах и сроках поставки.
7. Регистр сведений "Спецификации по умолчанию" используется для определения актуальной спецификации изделия при поиске спецификации.
