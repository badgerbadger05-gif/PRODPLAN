# Диагностика действующего PRODPLAN

Единственный серверный контур работает из `/home/barsukov/prodplan-shadow`:
backend `:8020`, frontend `:9020`, compose project `prodplan-shadow`. Контур
8010/9010 выведен из эксплуатации и не является fallback.

## Первичная проверка

```bash
ssh barsukov@mtzdock.lan
cd /home/barsukov/prodplan-shadow
git status --short
scripts/shadow-stack.sh verify
curl -fsS http://127.0.0.1:8020/health
curl -I http://127.0.0.1:9020
```

`verify` завершится ошибкой, если 8010 или 9010 отвечают. В таком случае
остановить и удалить старые контейнеры; старую базу не восстанавливать.

## Контейнеры и логи

```bash
docker compose --project-name prodplan-shadow --env-file .env.shadow \
  -f docker-compose.shadow.yml ps
docker compose --project-name prodplan-shadow --env-file .env.shadow \
  -f docker-compose.shadow.yml logs --tail=200 backend frontend sync-worker
docker compose --project-name prodplan-shadow --env-file .env.shadow \
  -f docker-compose.shadow.yml exec -T backend alembic current
```

## Planning truth

При неверных цифрах сначала проверяются принятое поколение Ledger, cutoff и
freshness, полнота физического Ledger, опубликованная read-model и чтение
остатков из принятого поколения. Нельзя переключаться на legacy-остатки или
использовать старый контур как второй источник истины.

Полное развёртывание описано только в
[prodplan-shadow-deploy.md](prodplan-shadow-deploy.md).
