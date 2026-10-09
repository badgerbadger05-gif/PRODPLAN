"""Bounded accepted receipt-exclusion repair; dry-run unless --apply is given."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.database import SessionLocal
from app.services.item_ledger.physical_refresh_candidacy import has_live_physical_refresh_candidate
from app.services.item_ledger.physical_refresh_orchestrator import _acquire_lifecycle_lock, _release_lifecycle_lock
from app.services.item_ledger.physical_refresh_supplier_evidence import repair_non_supplier_receipt_provenance
from app.services.one_c_export_common import create_odata_client
from app.services.odata_client import OData1CClient
from app.services.odata_config import load_odata_config
from app.services.planning_pool_resolver import resolve_planning_pool_by_warehouse
from app.services.sync_orchestrator import _acquire_cluster_lock, _release_cluster_lock, ClusterLockError


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-generation-id", type=int, required=True)
    parser.add_argument("--sle-ids", required=True, help="Exact comma-separated accepted physical SLE ids")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    ids = tuple(int(value.strip()) for value in args.sle_ids.split(","))
    cluster_lock = lifecycle_lock = None
    with SessionLocal() as db:
        try:
            if args.apply:
                cluster_lock = _acquire_cluster_lock(db)
                if not cluster_lock or isinstance(cluster_lock, ClusterLockError):
                    raise RuntimeError("Sync cluster lock is busy or unavailable")
                lifecycle_lock = _acquire_lifecycle_lock(db)
                if not lifecycle_lock or has_live_physical_refresh_candidate(db):
                    raise RuntimeError("Ledger publication is active; finish it before metadata repair")
            client = create_odata_client(load_odata_config(), OData1CClient)
            with contextlib.redirect_stdout(io.StringIO()):
                result = repair_non_supplier_receipt_provenance(db,
                    expected_generation_id=args.expected_generation_id, sle_ids=ids,
                    planning_pool_by_warehouse=resolve_planning_pool_by_warehouse(db),
                    odata_client=client, dry_run=not args.apply)
            if args.apply:
                db.commit()
            else:
                db.rollback()
            print(json.dumps(result, ensure_ascii=False))
        except Exception:
            db.rollback()
            raise
        finally:
            if lifecycle_lock:
                _release_lifecycle_lock(lifecycle_lock)
            if cluster_lock and not isinstance(cluster_lock, ClusterLockError):
                _release_cluster_lock(cluster_lock)


if __name__ == "__main__":
    main()
