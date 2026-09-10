#!/usr/bin/env bash
set -euo pipefail

action="${1:-start-verify}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_file="$repo_root/docker-compose.r2.yml"
project="prodplan-r2-local"

compose() {
  docker compose --project-name "$project" --file "$compose_file" "$@"
}

case "$action" in
  start) compose up --detach --wait r2-postgres ;;
  verify)
    compose exec --no-TTY r2-postgres pg_isready -U r2_user -d prodplan_r2
    echo "R2 PostgreSQL is local-only: 127.0.0.1:55441/prodplan_r2 (project $project)."
    ;;
  start-verify)
    compose up --detach --wait r2-postgres
    compose exec --no-TTY r2-postgres pg_isready -U r2_user -d prodplan_r2
    echo "R2 PostgreSQL is local-only: 127.0.0.1:55441/prodplan_r2 (project $project)."
    ;;
  stop) compose stop r2-postgres ;;
  *) echo "usage: $0 {start|verify|start-verify|stop}" >&2; exit 2 ;;
esac
