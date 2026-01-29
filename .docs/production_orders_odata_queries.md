# Запросы к 1С (OData) для учёта активных заказов

Цель: выяснить, как получать:
1) список **активных** заказов на производство (не «Завершен»)
2) их **остатки к выпуску** по позициям (`remaining_qty = ordered_qty - produced_qty`)

Тестовый кейс: заказ **000070** (частично выполнен).

## Предпосылки (метаданные)
- Заказ: `Document_ЗаказНаПроизводство`
- Состояние заказа: `СостояниеЗаказа_Key`, navigation `СостояниеЗаказа`
- Факт выпуска: `Document_СборкаЗапасов` содержит `ЗаказНаПроизводство_Key`

## Как пользоваться
1) Подставь URL: `{{BASE}}` = `https://<host>/<base>/odata/standard.odata/`
2) Открывай ссылки в браузере
3) Копируй JSON-ответ целиком

Рекомендация: почти везде добавлен `?$format=json`.

---

# A. Найти заказ 000070 и его состояние

## A1) Заголовок заказа по номеру
`{{BASE}}Document_ЗаказНаПроизводство?$format=json&$select=Ref_Key,Number,Date,Posted,DeletionMark&$filter=Number eq '000070'`

## A2) Состояние заказа (через expand)
`{{BASE}}Document_ЗаказНаПроизводство?$format=json&$select=Ref_Key,Number,Date,Posted,СостояниеЗаказа_Key&$expand=СостояниеЗаказа&$filter=Number eq '000070'`

## A3) GUID состояния «Завершен»
`{{BASE}}Document_ЗаказНаПроизводство?$format=json&$select=Ref_Key,Number,СостояниеЗаказа_Key&$expand=СостояниеЗаказа&$filter=Number eq '000070'`

После определения `{{DONE_STATE_KEY}}` (GUID состояния «Завершен»), фильтр активных:

`{{BASE}}Document_ЗаказНаПроизводство?$format=json&$select=Ref_Key,Number,Date,Posted,СостояниеЗаказа_Key&$filter=DeletionMark eq false and (СостояниеЗаказа_Key ne guid'{{DONE_STATE_KEY}}')`

---

# B. Заказано (ordered_qty) по позициям заказа

## B1) Табличная часть «Продукция» у заказа 000070
Сначала возьми `{{ORDER_REF_KEY}}` из ответа A1.

`{{BASE}}Document_ЗаказНаПроизводство_Продукция?$format=json&$select=Ref_Key,LineNumber,Номенклатура_Key,Количество&$filter=Ref_Key eq guid'{{ORDER_REF_KEY}}'`

---

# C. Выпущено (produced_qty) через «Сборка запасов»

## C1) Документы «Сборка запасов», привязанные к заказу 000070
Сначала возьми `{{ORDER_REF_KEY}}`.

`{{BASE}}Document_СборкаЗапасов?$format=json&$select=Ref_Key,Number,Date,Posted,DeletionMark,ЗаказНаПроизводство_Key&$filter=ЗаказНаПроизводство_Key eq guid'{{ORDER_REF_KEY}}'`

## C2) «Продукция» по найденным сборкам
Вариант 1 (через отдельный EntitySet табличной части):

`{{BASE}}Document_СборкаЗапасов_Продукция?$format=json&$select=Ref_Key,LineNumber,Номенклатура_Key,Характеристика_Key,Количество,Спецификация_Key&$filter=Ref_Key eq guid'{{ASSEMBLY_REF_KEY}}'`

Где `{{ASSEMBLY_REF_KEY}}` — `Ref_Key` из ответа C1 (запусти для каждой сборки).

Вариант 2 (через expand, одной пачкой):

`{{BASE}}Document_СборкаЗапасов?$format=json&$select=Ref_Key,Number,Date,Posted,ЗаказНаПроизводство_Key&$expand=Продукция&$filter=ЗаказНаПроизводство_Key eq guid'{{ORDER_REF_KEY}}'`

### Проверка частичного выполнения
1) Из B1 получаем `ordered_qty` по каждой номенклатуре
2) Из C2 суммируем `Количество` по каждой номенклатуре как `produced_qty`
3) Считаем `remaining_qty = max(ordered_qty - produced_qty, 0)`

Отдельно решаем правило: учитывать ли только `Posted == true` у `Document_СборкаЗапасов`.

---

# D. Альтернатива: регистр «Выпуск продукции»

Идея: если регистр `AccumulationRegister_ВыпускПродукции` даёт устойчивый «факт выпуска», его можно использовать как источник `produced_qty`.

Проблема: в видимом фрагменте метаданных у записи регистра не видно поля `ЗаказНаПроизводство_Key` (прямой привязки к заказу).

## D1) Выгрузить последние записи регистра
`{{BASE}}AccumulationRegister_ВыпускПродукции_RecordType?$format=json&$top=50&$orderby=Period desc&$select=Period,Recorder,Recorder_Type,Номенклатура_Key,Характеристика_Key,Партия_Key,Спецификация_Key,Количество,КоличествоПлан`

Что ищем в ответе:
- поле, по которому можно однозначно привязать выпуск к заказу на производство (например `ЗаказНаПроизводство_Key`);
- если нет — кандидаты (`Партия_Key`, `Recorder` + `Recorder_Type`, связка со `Document_СборкаЗапасов`).