# PRODPLAN 9020: эксплуатация и развёртывание

С 2026-09-02 это единственный действующий серверный контур PRODPLAN. Названия
`shadow` в каталоге, compose project и runtime-каталогах сохранены как
идентичность существующей базы и инфраструктуры; они не означают параллельный
или тестовый режим.

## Единственный источник развёртывания

| Ресурс | Значение |
|---|---|
| Каталог | `/home/barsukov/prodplan-shadow` |
| Compose project | `prodplan-shadow` |
| Compose file | `docker-compose.shadow.yml` |
| Frontend | `http://mtzdock.lan:9020` |
| Backend | `http://mtzdock.lan:8020` |
| PostgreSQL | `127.0.0.1:55434` |
| Config / output | `config-shadow/` / `output-shadow/` |
| PostgreSQL volume | `prodplan-shadow_prodplan_shadow_postgres_data` |

Порты 8010 и 9010 выведены из эксплуатации и должны быть закрыты. Старый
checkout `/home/barsukov/prodplan` не является источником запуска. Его
`docker-compose.test.yml` — пустая заглушка.

## Подготовка

В `.env.shadow` должны быть уникальные секреты, права `600` и точный полный
commit frontend:

```text
PRODPLAN_FRONTEND_BUILD_CONTEXT=./frontend-erp-shell
PRODPLAN_FRONTEND_EXPECTED_COMMIT=<ПОЛНЫЙ_40_СИМВОЛЬНЫЙ_COMMIT>
```

`config-shadow/odata_config.json` содержит рабочую конфигурацию 1С и также
должен иметь права `600`. Секреты нельзя коммитить или печатать в лог.

## Развёртывание

```bash
cd /home/barsukov/prodplan-shadow
git status --short
git pull --ff-only
scripts/shadow-stack.sh start
```

`start` проверяет, что 8010/9010 закрыты, собирает образы, запускает БД,
применяет Alembic, затем запускает backend/frontend и выполняет проверку.
Нельзя применять `git reset --hard`, `docker compose down -v` или подключать
другой PostgreSQL volume.

Проверка:

```bash
scripts/shadow-stack.sh verify
curl -fsS http://127.0.0.1:8020/health
curl -I http://127.0.0.1:9020
if curl -fsS http://127.0.0.1:8010/health; then exit 1; fi
if curl -fsS http://127.0.0.1:9010; then exit 1; fi
```

## Воркеры, backup и остановка

```bash
scripts/shadow-stack.sh start-workers
scripts/shadow-stack.sh backup
scripts/shadow-stack.sh stop
```

`sync-worker` находится в compose-профиле `automation`. `backup` создаёт
согласованный custom-format dump действующей базы. `stop` сохраняет volume и
данные. Restore допустим только из проверенного файла в `backups-shadow/` и с
явным `CONFIRM_RESTORE=prodplan-shadow` согласно подсказке скрипта.

## Запрещено

- запускать или восстанавливать контур 8010/9010;
- использовать 9010 как fallback при ошибке 9020;
- подключать backend к иной или старой PostgreSQL;
- копировать живой PostgreSQL volume через `cp`/`rsync`;
- выполнять `docker compose down -v`;
- создавать второй PRODPLAN-писатель документов 1С.
