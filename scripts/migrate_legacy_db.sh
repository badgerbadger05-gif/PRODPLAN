#!/usr/bin/env bash
# Migrate data from the legacy prodplan-db-1 (project root C:\prodplan, old layout)
# to the new prodplan-erp-db-1 (current project root C:\prodplan-erp).
#
# Run this once when you're ready to retire the legacy container.
#
# This script is intentionally destructive on the TARGET database:
# every table in the new prodplan-erp-db-1 will be wiped and replaced
# with the contents of prodplan-db-1.
#
# Prerequisites:
#   - docker ps shows both `prodplan-db-1` and `prodplan-erp-db-1` running
#   - both databases share the same alembic schema version
#
# After running successfully you can stop and remove the legacy stack:
#   docker stop prodplan-db-1 && docker rm prodplan-db-1
#   docker network rm prodplan_default
# (and update the backend to drop the manual `docker network connect prodplan_default ...`).

set -euo pipefail

SRC=prodplan-db-1
DST=prodplan-erp-db-1
TMP=/tmp/prodplan_legacy_dump.sql

echo "→ Dumping data from ${SRC}…"
docker exec "${SRC}" pg_dump -U prodplan -d prodplan \
    --data-only \
    --disable-triggers \
    --exclude-table=alembic_version \
    > "${TMP}"
echo "  done ($(wc -l < "${TMP}") lines)"

echo "→ Wiping target tables in ${DST}…"
docker exec "${DST}" psql -U prodplan -d prodplan -v ON_ERROR_STOP=1 <<'SQL'
DO $$
DECLARE r RECORD;
BEGIN
  FOR r IN
    SELECT tablename FROM pg_tables
     WHERE schemaname = 'public'
       AND tablename <> 'alembic_version'
  LOOP
    EXECUTE format('TRUNCATE TABLE public.%I RESTART IDENTITY CASCADE', r.tablename);
  END LOOP;
END $$;
SQL

echo "→ Restoring dump into ${DST}…"
cat "${TMP}" | docker exec -i "${DST}" psql -U prodplan -d prodplan -v ON_ERROR_STOP=1 -q

echo "→ Verifying counts…"
docker exec "${DST}" psql -U prodplan -d prodplan -c "
SELECT 'items' tbl, count(*) FROM items
UNION ALL SELECT 'specifications', count(*) FROM specifications
UNION ALL SELECT 'production_plan_header', count(*) FROM production_plan_header
UNION ALL SELECT 'production_plan_line', count(*) FROM production_plan_line
UNION ALL SELECT 'planning_run', count(*) FROM planning_run
ORDER BY tbl;
"
echo "✓ Migration complete. Next:"
echo "  1) Disconnect backend from legacy network:"
echo "     docker network disconnect prodplan_default prodplan-erp-backend-1"
echo "  2) (optional) Stop legacy stack:"
echo "     docker stop prodplan-db-1 && docker rm prodplan-db-1"
