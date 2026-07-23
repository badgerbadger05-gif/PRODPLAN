# Статус правок по код-ревью

**Дата:** 2026-06-14. **Ветка:** `main` (слита из `review-fixes`). **Задеплоено в прод:** `main @ 3bcb50af` (mtzdock.lan, `/home/barsukov/prodplan`).

Полный разбор находок — [`code_review_merged.md`](code_review_merged.md) (свод 3 ревью). Здесь — что реально сделано и что осталось.

---

## ✅ Сделано и задеплоено в прод (тесты: 330 passed, 1 skipped)

| Код | Что | Файлы | Тесты |
|-----|-----|-------|-------|
| A1 (частично) | Сужен CORS; `rollback()` в `get_db` при ошибке | `main.py`, `database.py` | suite |
| A2 | `GET /odata/config` маскирует пароль/токен; sentinel `***` при сохранении = «оставить текущий секрет» (не затирает пароль 1С); `.dict()`→`.model_dump()` | `routers/odata.py`, `services/odata_config.py`, `item_service.py` | `test_odata_config_secrets.py` |
| A3 | Починен краш `/debug/production-order-states` (`models.ODataConfig` → `load_odata_config`); `iter_by_guid` перенесён в класс | `routers/sync.py`, `odata_client.py` | — |
| A4 | Фронт: loading-leak на странице закупок, экспорт скрытых выбранных строк, `AbortSignal` в `api()`, ErrorBoundary + catch-all маршрут | `PurchaseControlPage.tsx`, `lib/api.ts`, `ui/App.tsx`, `ui/ErrorBoundary.tsx` | сборка/lint |
| **B2** | Идемпотентность экспорта в 1С: per-entry commit (нет дублей документов при крахе); legacy `export_issue_to_1c` не постит повторно при наличии `exported_ref1c`; частичный экспорт закупок исключает уже выгруженные строки (нет дубля заказа поставщику) | `one_c_export_common.py`, `production_control_material_issues.py`, `one_c_purchase_order_export.py` | `test_export_idempotency.py`, `test_export_issue_idempotency.py`, `test_purchase_export_dedup.py` |
| **B4** | Идемпотентность пересчёта прогона (повтор с тем же `run_id` чистит старые результаты); `rollback()` вместо `commit()` при FAILURE (не фиксируется частичный проваленный прогон) | `planning_service.py` | `test_planning_run_idempotency.py` |
| **B5** | Блокировка пула остатков участка: транзакционный advisory-lock в `create_material_issues` (нет двойного claim'а при параллельной работе — кейс PP001308915) | `production_control_material_issues.py` | `test_material_issue_locking.py` |
| B3 (чистка) | Удалён мёртвый цикл в `_limit_by_components` | `order_quantity_calculator.py` | — |
| B6 | `datetime.utcnow()` → `datetime.now(timezone.utc)` (13 файлов) | по проекту | suite |

**Проверено на боевой после деплоя:** все 5 сервисов healthy; `GET /odata/config` маскирует и сохраняет секрет; синхронизация с 1С работает (FACT SYNC, sync-tick 200); запись в тома работает; `alembic current = 20260611_01 (head)`.

---

## ⏳ Осталось (не блокеры, отдельными сессиями)

### A1-non-root — вернуть запуск контейнера без root
**Почему отложено:** `USER appuser` (uid 10001) не мог писать в смонтированные `./output`/`./config` (владелец `barsukov`) — откатили перед деплоем.
**Как сделать безопасно:** не хардкодить uid в Dockerfile, а задать `user: "<uid>:<gid>"` (uid `barsukov`, узнать `id barsukov`) для сервисов backend/sync-worker/reconcile-worker в `docker-compose.yml`. Редеплой → проверить, что выгрузки в `output/` пишутся. Риск низкий, обратимо.

### B1 — убрать несущий `create_all`, завести `alembic upgrade` в деплой
**Почему не сделано сразу:** `create_all` сейчас **несущий** — на чистой БД `alembic upgrade head` падает (`relation "items" does not exist`): базовые таблицы не создаёт ни одна миграция, alembic внедрён поверх существующей БД. Прод стоит на `stamp` head, схему строит `create_all`. Подробно: [[schema-create-all-load-bearing]] / `code_review_merged.md`.
**Протокол (с риском для управления схемой, делать осознанно):**
1. `pg_dump` схемы прода как бэкап/эталон.
2. Сгенерировать baseline-миграцию (вся текущая схема, `down_revision=None`), перецепить `20250925_01` на неё.
3. **Сверить** model-схему с фактической схемой прода (поймать дрейф от `create_all`).
4. `alembic upgrade head` на чистой PG (одноразовый контейнер) → схема воспроизводится 1-в-1.
5. Завести `alembic upgrade head` в entrypoint (только backend, не воркеры) и убрать `create_all` из `main.py`.
6. Деплой; на проде (уже на head) upgrade — no-op.

### B3-ядро — гейтинг по компонентам в MRP
Двойной счёт в гейтинге воспроизведён, **но это семантика/политика, а не баг**: гейтинг оптимистичный (родитель планируется при наличии валового остатка компонента, который встанет в план отдельным заказом). Заказчик подтвердил, что **MRP на реальных цифрах считает корректно** → оставлено как есть (by design). Разбор и воспроизведение — в `code_review_merged.md` (раздел M-5 / «Статус B3»).

### Остаточные M-4 (низкий приоритет, требуют прод-валидации)
Pre-generated `Ref_Key` до POST (устранить окно дубля на один документ); `ON CONFLICT` в `upsert_sync_link`; ретраи PATCH/Post в `odata_client`.

---

## Откат прода (если понадобится)
Схема не менялась (миграций не добавляли), поэтому откат — только код:
```
ssh barsukov@mtzdock.lan
cd /home/barsukov/prodplan
git checkout feature/purchase-journal
docker compose up -d --build
```
