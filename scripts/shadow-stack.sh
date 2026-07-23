#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

PROJECT_NAME="prodplan-shadow"
COMPOSE_FILE="docker-compose.shadow.yml"
ENV_FILE=".env.shadow"
DEFAULT_EXPECTED_DIR="/home/barsukov/prodplan-shadow"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
EXPECTED_DIR="${PRODPLAN_SHADOW_EXPECTED_DIR:-${DEFAULT_EXPECTED_DIR}}"

die() {
  printf 'ОШИБКА: %s\n' "$*" >&2
  exit 1
}

info() {
  printf 'shadow: %s\n' "$*"
}

require_exact_checkout() {
  [[ "${REPO_DIR}" == "${EXPECTED_DIR}" ]] || die \
    "ожидался каталог ${EXPECTED_DIR}, фактически ${REPO_DIR}. Не запускаю чужой compose."
  [[ -f "${REPO_DIR}/${COMPOSE_FILE}" ]] || die "не найден ${COMPOSE_FILE}"
  cd -- "${REPO_DIR}"
}

require_env() {
  [[ -f "${ENV_FILE}" ]] || die \
    "нет ${ENV_FILE}; выполните '$0 bootstrap', затем задайте отдельный пароль"
  local mode
  mode="$(stat -c '%a' "${ENV_FILE}")"
  [[ "${mode}" == "600" ]] || die "${ENV_FILE} должен иметь права 600, сейчас ${mode}"

  local password
  password="$(sed -n 's/^PRODPLAN_SHADOW_POSTGRES_PASSWORD=//p' "${ENV_FILE}" | tail -n 1)"
  [[ "${#password}" -ge 24 ]] || die "пароль shadow PostgreSQL должен быть не короче 24 символов"
  [[ "${password}" != *REPLACE* && "${password}" != *change_me* ]] || die \
    "замените шаблонный пароль в ${ENV_FILE}"
  [[ "${password}" =~ ^[A-Za-z0-9_-]+$ ]] || die \
    "для DATABASE_URL используйте URL-безопасный пароль: A-Z, a-z, 0-9, _ и -"

  local frontend_context
  frontend_context="$(sed -n 's/^PRODPLAN_FRONTEND_BUILD_CONTEXT=//p' "${ENV_FILE}" | tail -n 1)"
  frontend_context="${frontend_context:-./frontend-erp-shell}"
  [[ -f "${frontend_context}/Dockerfile" && -f "${frontend_context}/package.json" ]] || die \
    "PRODPLAN_FRONTEND_BUILD_CONTEXT не указывает на подготовленный frontend: ${frontend_context}"

  local expected_frontend_commit actual_frontend_commit
  expected_frontend_commit="$(
    sed -n 's/^PRODPLAN_FRONTEND_EXPECTED_COMMIT=//p' "${ENV_FILE}" | tail -n 1
  )"
  [[ "${expected_frontend_commit}" =~ ^[0-9a-f]{40}$ ]] || die \
    "задайте полный 40-символьный PRODPLAN_FRONTEND_EXPECTED_COMMIT"
  actual_frontend_commit="$(git -C "${frontend_context}" rev-parse HEAD 2>/dev/null)" || die \
    "frontend context не принадлежит проверенному Git checkout: ${frontend_context}"
  [[ "${actual_frontend_commit}" == "${expected_frontend_commit}" ]] || die \
    "frontend commit ${actual_frontend_commit} не совпадает с ожидаемым ${expected_frontend_commit}"
  [[ -z "$(git -C "${frontend_context}" status --short --untracked-files=no)" ]] || die \
    "frontend checkout содержит незакоммиченные изменения"
}

compose() {
  docker compose \
    --project-name "${PROJECT_NAME}" \
    --env-file "${ENV_FILE}" \
    --file "${COMPOSE_FILE}" \
    "$@"
}

wait_for_db() {
  local attempts=45
  until compose exec -T db sh -c \
    'exec pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >/dev/null 2>&1; do
    attempts=$((attempts - 1))
    [[ "${attempts}" -gt 0 ]] || die "shadow PostgreSQL не стал готов за отведённое время"
    sleep 2
  done
}

bootstrap() {
  mkdir -p -- config-shadow output-shadow backups-shadow
  chmod 700 -- config-shadow output-shadow backups-shadow
  if [[ ! -e "${ENV_FILE}" ]]; then
    cp -- .env.shadow.example "${ENV_FILE}"
    chmod 600 -- "${ENV_FILE}"
    info "создан ${ENV_FILE}; задайте в нём уникальный пароль и повторите команду"
    return 2
  fi
  chmod 600 -- "${ENV_FILE}"
  require_env
  compose config --quiet
  info "каталоги и конфигурация подготовлены"
}

build_images() {
  require_env
  compose build backend frontend
}

migrate() {
  require_env
  compose up -d db
  wait_for_db
  # A one-shot container applies migrations before the long-running backend is
  # allowed to start.
  compose run --rm --no-deps backend alembic upgrade head
  info "миграции shadow-базы применены"
}

start_stack() {
  require_env
  compose config --quiet
  compose build backend frontend
  compose up -d db
  wait_for_db
  compose run --rm --no-deps backend alembic upgrade head
  compose up -d --no-deps backend
  compose up -d frontend
  verify_stack
}

verify_stack() {
  require_env
  compose ps
  local backend_port frontend_port
  backend_port="$(sed -n 's/^PRODPLAN_SHADOW_BACKEND_PORT=//p' "${ENV_FILE}" | tail -n 1)"
  frontend_port="$(sed -n 's/^PRODPLAN_SHADOW_FRONTEND_PORT=//p' "${ENV_FILE}" | tail -n 1)"
  backend_port="${backend_port:-8020}"
  frontend_port="${frontend_port:-9020}"
  curl --fail --silent --show-error --max-time 10 \
    "http://127.0.0.1:${backend_port}/health" >/dev/null
  curl --fail --silent --show-error --max-time 10 \
    "http://127.0.0.1:${frontend_port}/" >/dev/null
  compose exec -T backend alembic current
  info "backend и frontend отвечают; версия миграции показана выше"
}

backup_db() {
  require_env
  mkdir -p -- backups-shadow
  chmod 700 -- backups-shadow
  compose up -d db
  wait_for_db
  local stamp target tmp_target
  stamp="$(date +'%Y%m%d-%H%M%S')"
  target="backups-shadow/prodplan-shadow-${stamp}.dump"
  tmp_target="${target}.partial"
  compose exec -T db sh -c \
    'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --no-owner --no-acl' \
    > "${tmp_target}"
  [[ -s "${tmp_target}" ]] || die "pg_dump создал пустой файл"
  mv -- "${tmp_target}" "${target}"
  chmod 600 -- "${target}"
  info "согласованный dump сохранён: ${target}"
}

restore_db() {
  require_env
  local source="${1:-}"
  [[ -n "${source}" && -f "${source}" ]] || die "укажите существующий .dump"
  local source_real backup_root
  source_real="$(realpath -- "${source}")"
  backup_root="$(realpath -- backups-shadow)"
  [[ "${source_real}" == "${backup_root}/"* ]] || die \
    "restore разрешён только из ${backup_root}, получен ${source_real}"
  [[ "${CONFIRM_RESTORE:-}" == "${PROJECT_NAME}" ]] || die \
    "restore удалит ТОЛЬКО shadow-базу; повторите с CONFIRM_RESTORE=${PROJECT_NAME}"
  compose --profile automation stop frontend backend sync-worker 2>/dev/null || true
  compose up -d db
  wait_for_db
  compose exec -T db sh -c \
    'exec dropdb -U "$POSTGRES_USER" --if-exists --force "$POSTGRES_DB"'
  compose exec -T db sh -c \
    'exec createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'
  compose exec -T db sh -c \
    'exec pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-acl' \
    < "${source_real}"
  info "восстановлена только shadow-база из ${source_real}; приложение оставлено остановленным"
}

start_workers() {
  require_env
  compose --profile automation up -d sync-worker
  info "shadow sync-worker включён; legacy MRP reconcile-worker удалён"
}

stop_stack() {
  require_env
  compose --profile automation stop
  info "shadow-контейнеры остановлены; volume и данные сохранены"
}

usage() {
  cat <<'EOF'
Использование: scripts/shadow-stack.sh COMMAND

  bootstrap        создать защищённые каталоги и шаблон .env.shadow
  build            собрать backend и frontend
  migrate          поднять только БД и применить Alembic
  start            build -> БД -> миграции -> backend -> frontend -> verify
  verify           проверить контейнеры, HTTP и текущую миграцию
  backup           создать согласованный custom-format dump shadow-базы
  restore FILE     восстановить shadow-базу (нужен CONFIRM_RESTORE=prodplan-shadow)
  start-workers    явно включить фоновые воркеры профиля automation
  stop             остановить только shadow-контейнеры, не удаляя volume
EOF
}

require_exact_checkout
case "${1:-}" in
  bootstrap) bootstrap ;;
  build) build_images ;;
  migrate) migrate ;;
  start) start_stack ;;
  verify) verify_stack ;;
  backup) backup_db ;;
  restore) restore_db "${2:-}" ;;
  start-workers) start_workers ;;
  stop) stop_stack ;;
  *) usage; exit 2 ;;
esac
