# Диагностика

## Сначала определить контур

Команды нельзя запускать до определения целевого контура.

| Контур | Compose | Backend | Frontend |
|---|---|---|---|
| production | `docker-compose.test.yml`, project `prodplan` | `:8010` | `:9010` |
| shadow | `scripts/shadow-stack.sh` | `:8020` | `:9020` |
| локальная разработка | выбранный разработчиком compose/Vite | проверить фактические порты | проверить фактические порты |

Production-команды заданы только в
[`prodplan-deploy.md`](prodplan-deploy.md). Shadow-команды — только в
[`prodplan-shadow-deploy.md`](prodplan-shadow-deploy.md).

Не использовать без проверки старые команды `docker-compose up` и порт
`:8000`: они не описывают production.

## Безопасная первичная проверка production

```bash
ssh barsukov@mtzdock.lan
cd /home/barsukov/prodplan
git status --short
docker compose -f docker-compose.test.yml ps
curl -fsS http://localhost:8010/health
curl -I http://localhost:9010
```

Команды только читают состояние. Перед обновлением, миграцией или рестартом
следовать полному runbook `prodplan-deploy.md`.

## Логи production

```bash
cd /home/barsukov/prodplan
docker compose -f docker-compose.test.yml logs --tail=200 backend
docker compose -f docker-compose.test.yml logs --tail=200 frontend
docker compose -f docker-compose.test.yml logs --tail=200 sync-worker
docker compose -f docker-compose.test.yml logs --tail=200 reconcile-worker
```

## Миграции production

Сначала проверить текущую версию:

```bash
docker compose -f docker-compose.test.yml exec backend alembic current
```

`alembic upgrade head` изменяет БД и выполняется только в утверждённой
последовательности deploy-runbook.

## Planning truth

При неверных цифрах сначала проверяются:

1. принятое `ledger_generation`;
2. cutoff и freshness;
3. полнота физического Ledger;
4. статус опубликованной read-model;
5. чтение остатков из принятого поколения Item Ledger.

Нельзя диагностически переключаться на legacy-остатки и сравнивать их как
второй источник истины. Balance используется только для сверки полноты
Ledger.
