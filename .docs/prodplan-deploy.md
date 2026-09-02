# Выведенный из эксплуатации контур 8010/9010

Контур `prodplan` на backend `:8010` и frontend `:9010` окончательно выведен
из эксплуатации 2026-09-02. Его нельзя запускать, восстанавливать или
использовать как резервный интерфейс.

`docker-compose.test.yml` оставлен только как пустая совместимая заглушка:
даже историческая команда `docker compose -f docker-compose.test.yml up -d`
не содержит сервисов и не может поднять старый контур.

Единственный действующий PRODPLAN:

- checkout `/home/barsukov/prodplan-shadow`;
- compose project `prodplan-shadow`;
- frontend `http://mtzdock.lan:9020`;
- backend health `http://mtzdock.lan:8020/health`;
- PostgreSQL `127.0.0.1:55434`;
- runbook [prodplan-shadow-deploy.md](prodplan-shadow-deploy.md).

При диагностике порты 8010 и 9010 должны быть закрыты. Обнаружение ответа на
любом из них — инцидент: старые контейнеры нужно остановить и удалить, не
переключая пользователей и не восстанавливая старую базу.
