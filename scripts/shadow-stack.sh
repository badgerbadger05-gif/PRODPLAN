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

  local admin_token
  admin_token="$(sed -n 's/^ITEM_LEDGER_ADMIN_TOKEN=//p' "${ENV_FILE}" | tail -n 1)"
  [[ "${#admin_token}" -ge 32 ]] || die \
    "ITEM_LEDGER_ADMIN_TOKEN должен быть не короче 32 символов"
  [[ "${admin_token}" != *REPLACE* && "${admin_token}" != *change_me* ]] || die \
    "замените шаблонный ITEM_LEDGER_ADMIN_TOKEN"

}

env_value() {
  local key="$1"
  sed -n "s/^${key}=//p" "${ENV_FILE}" | tail -n 1
}

require_runtime_images() {
  require_runtime_image \
    PRODPLAN_BACKEND_GREEN_IMAGE \
    PRODPLAN_BACKEND_GREEN_EXPECTED_IMAGE_ID \
    PRODPLAN_BACKEND_GREEN_REVISION
  require_runtime_image \
    PRODPLAN_SYNC_WORKER_IMAGE \
    PRODPLAN_SYNC_WORKER_EXPECTED_IMAGE_ID \
    PRODPLAN_SYNC_WORKER_REVISION
  require_runtime_image \
    PRODPLAN_FRONTEND_IMAGE \
    PRODPLAN_FRONTEND_EXPECTED_IMAGE_ID
}

require_runtime_image() {
  local image_var="$1" image_id_var="$2" revision_var="${3:-}"
  local image expected_id actual_id expected_revision actual_revision
  image="$(env_value "${image_var}")"
  expected_id="$(env_value "${image_id_var}")"
  [[ "${image}" =~ ^[A-Za-z0-9._/-]+:[A-Za-z0-9._-]+$ ]] || die \
    "${image_var} must name a stable local image tag"
  [[ "${image}" != *:latest && "${image}" != *REPLACE* ]] || die \
    "${image_var} must not use latest or a placeholder"
  [[ "${expected_id}" =~ ^sha256:[0-9a-f]{64}$ ]] || die \
    "${image_id_var} must contain the reviewed sha256 image ID"
  actual_id="$(docker image inspect "${image}" --format '{{.Id}}' 2>/dev/null)" || die \
    "reviewed image is not present locally: ${image}"
  [[ "${actual_id}" == "${expected_id}" ]] || die \
    "image ${image} ID ${actual_id} != expected ${expected_id}"
  if [[ -n "${revision_var}" ]]; then
    expected_revision="$(env_value "${revision_var}")"
    [[ "${expected_revision}" =~ ^[0-9a-f]{40}$ ]] || die \
      "${revision_var} must contain the reviewed 40-character commit"
    actual_revision="$(docker image inspect "${image}" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')"
    [[ "${actual_revision}" == "${expected_revision}" ]] || die \
      "image ${image} revision ${actual_revision} != expected ${expected_revision}"
  fi
}

require_frontend_proxy_config() {
  local nginx_config
  nginx_config="$(env_value PRODPLAN_FRONTEND_NGINX_CONFIG)"
  nginx_config="${nginx_config:-./config-shadow/nginx/default.conf}"
  [[ -f "${nginx_config}" ]] || die \
    "reviewed persistent frontend nginx config is missing: ${nginx_config}"
  grep -Fqx '  set $backend_upstream http://backend-green:8000;' "${nginx_config}" || die \
    "frontend nginx config must point only to backend-green"
}

require_schema_at_head() {
  local expected_heads current_heads
  expected_heads="$(
    compose run --rm --no-deps backend-green python -c \
      'from alembic.config import Config; from alembic.script import ScriptDirectory; print(",".join(sorted(ScriptDirectory.from_config(Config("alembic.ini")).get_heads())))'
  )" || die "не удалось прочитать Alembic heads из reviewed backend image"
  current_heads="$(
    compose exec -T db sh -c \
      'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "SELECT version_num FROM alembic_version ORDER BY version_num"'
  )" || die "не удалось прочитать текущую ревизию Alembic из shadow-базы"
  current_heads="${current_heads//$'\n'/,}"
  [[ -n "${expected_heads}" && "${current_heads}" == "${expected_heads}" ]] || die \
    "shadow schema ${current_heads:-<none>} != reviewed backend head ${expected_heads:-<none>}; run explicit migrate"
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

require_retired_ports_closed() {
  local port
  for port in 8010 9010; do
    if curl --silent --show-error --max-time 2 \
      "http://127.0.0.1:${port}/" >/dev/null 2>&1; then
      die "выведенный из эксплуатации порт ${port} отвечает; остановите старый контур"
    fi
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
  require_runtime_images
  info "используются pinned backend/frontend/worker images; implicit build запрещён"
}

migrate() {
  require_env
  require_runtime_images
  compose up -d db
  wait_for_db
  # A one-shot container applies migrations before the long-running backend is
  # allowed to start.
  compose run --rm --no-deps backend-green alembic upgrade head
  require_schema_at_head
  info "миграции shadow-базы применены"
}

start_stack() {
  require_retired_ports_closed
  require_env
  require_runtime_images
  require_frontend_proxy_config
  compose config --quiet
  compose up -d db
  wait_for_db
  require_schema_at_head
  compose up -d --no-deps backend-green
  compose up -d frontend
  verify_stack
}

verify_stack() {
  require_retired_ports_closed
  require_env
  require_runtime_images
  require_frontend_proxy_config
  require_schema_at_head
  compose ps
  local backend_port frontend_port
  backend_port="$(sed -n 's/^PRODPLAN_SHADOW_GREEN_PORT=//p' "${ENV_FILE}" | tail -n 1)"
  frontend_port="$(sed -n 's/^PRODPLAN_SHADOW_FRONTEND_PORT=//p' "${ENV_FILE}" | tail -n 1)"
  backend_port="${backend_port:-18020}"
  frontend_port="${frontend_port:-9020}"
  curl --fail --silent --show-error --max-time 10 \
    "http://127.0.0.1:${backend_port}/health" >/dev/null
  curl --fail --silent --show-error --max-time 10 \
    "http://127.0.0.1:${frontend_port}/" >/dev/null
  compose exec -T backend-green alembic current
  info "backend и frontend отвечают; 8010/9010 закрыты; версия миграции показана выше"
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
  compose --profile automation --profile rollback stop \
    frontend backend backend-green sync-worker 2>/dev/null || true
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
  require_runtime_images
  local green_port
  green_port="$(env_value PRODPLAN_SHADOW_GREEN_PORT)"
  green_port="${green_port:-18020}"
  curl --fail --silent --show-error --max-time 10 \
    "http://127.0.0.1:${green_port}/health" >/dev/null || die \
    "backend-green is not healthy; refusing to start worker"
  local worker_id worker_state worker_paused worker_image desired_worker_image
  local worker_base_url
  local stray_green_worker
  stray_green_worker="$(
    docker ps -aq \
      --filter "label=com.docker.compose.project=${PROJECT_NAME}" \
      --filter "label=com.docker.compose.service=sync-worker-green"
  )"
  [[ -z "${stray_green_worker}" ]] || die \
    "sync-worker-green still exists; remove it before starting canonical sync-worker"
  worker_id="$(compose --profile automation ps --all -q sync-worker 2>/dev/null || true)"
  if [[ -n "${worker_id}" ]]; then
    worker_state="$(docker inspect --format '{{.State.Status}}' "${worker_id}")"
    worker_paused="$(docker inspect --format '{{.State.Paused}}' "${worker_id}")"
    worker_image="$(docker inspect --format '{{.Image}}' "${worker_id}")"
    worker_base_url="$(
      docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "${worker_id}" \
        | sed -n 's/^SYNC_BASE_URL=//p' \
        | tail -n 1
    )"
    desired_worker_image="$(env_value PRODPLAN_SYNC_WORKER_EXPECTED_IMAGE_ID)"
    if [[ "${worker_state}" == running ]]; then
      if [[ \
        "${worker_paused}" == false \
        && "${worker_image}" == "${desired_worker_image}" \
        && "${worker_base_url}" == "http://backend-green:8000" \
      ]]; then
        info "canonical sync-worker уже запущен на reviewed image"
        return 0
      fi
      die "existing sync-worker is running or paused on another image; use the explicit pause/drain/stop handoff first"
    fi
  fi
  compose --profile automation up -d --no-deps --force-recreate sync-worker
  info "shadow sync-worker включён; legacy MRP reconcile-worker удалён"
}

stop_stack() {
  require_env
  compose --profile automation --profile rollback stop
  info "shadow-контейнеры остановлены; volume и данные сохранены"
}

usage() {
  cat <<'EOF'
Использование: scripts/shadow-stack.sh COMMAND

  bootstrap        создать защищённые каталоги и шаблон .env.shadow
  build            проверить наличие pinned backend/frontend/worker images
  migrate          поднять только БД и применить Alembic
  start            pinned green backend -> frontend -> verify
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
