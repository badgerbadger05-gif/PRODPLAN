from app.routers import production_control as routes


def test_execution_sync_reads_orders_before_transfers(db_session, monkeypatch):
    calls: list[str] = []
    request = object()

    monkeypatch.setattr(
        routes,
        "configured_production_order_sync_request",
        lambda *, dry_run: calls.append(f"request:{dry_run}") or request,
    )
    monkeypatch.setattr(
        routes,
        "sync_production_orders_from_odata",
        lambda db, payload: calls.append("orders") or {"orders_updated": 1},
    )
    monkeypatch.setattr(
        routes,
        "sync_posted_transfers",
        lambda db, *, dry_run: calls.append("transfers") or {"advanced": 2},
    )

    result = routes.post_sync_execution_from_1c(dry_run=False, db=db_session)

    assert calls == ["request:False", "orders", "transfers"]
    assert result == {
        "orders": {"orders_updated": 1},
        "transfers": {"advanced": 2},
    }
