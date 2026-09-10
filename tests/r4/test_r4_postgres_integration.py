"""Real PostgreSQL R4 transaction/locking checks.

The test is opt-in through the already supported local R2 DSN and never uses a
default or production connection.
"""

from __future__ import annotations

import os
import threading
import time

import pytest


def _dsn() -> str:
    value = os.getenv("PRODPLAN_R2_TEST_DSN")
    if not value:
        pytest.skip("PRODPLAN_R2_TEST_DSN is not configured")
    from app.r2_local_contract import validate_r2_dsn

    validate_r2_dsn(value)
    return value


@pytest.mark.integration
def test_two_postgresql_sessions_serialize_current_replenishment_without_double_apply():
    dsn = _dsn()
    pytest.importorskip("psycopg2")
    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker

    from app.services.item_ledger.current_replenishment import apply_current_replenishment
    from tests.services.test_current_replenishment_transaction import _reserves, _world

    engine = sa.create_engine(dsn, poolclass=sa.pool.NullPool)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    seed = Session()
    try:
        generation_id, _item_id, reservations, facts = _world(seed, prefix="pg-lock")
        reserves = _reserves(reservations)
        apply_current_replenishment(
            seed,
            generation_id=generation_id,
            source_key="physical:r4-pg",
            source_revision=1,
            facts=facts,
            reserves=reserves,
            complete_scope=True,
        )
        seed.commit()
    finally:
        seed.close()

    first = Session()
    second = Session()
    entered = threading.Event()
    release = threading.Event()
    result: dict[str, object] = {}

    def writer_one() -> None:
        try:
            result["one"] = apply_current_replenishment(
                first,
                generation_id=generation_id,
                source_key="physical:r4-pg",
                source_revision=2,
                facts=facts,
                reserves=reserves,
                complete_scope=True,
            )
            entered.set()
            assert release.wait(timeout=10)
            first.commit()
        except BaseException as exc:  # pragma: no cover - surfaced below
            result["one_error"] = exc

    def writer_two() -> None:
        try:
            assert entered.wait(timeout=10)
            result["two"] = apply_current_replenishment(
                second,
                generation_id=generation_id,
                source_key="physical:r4-pg",
                source_revision=2,
                facts=facts,
                reserves=reserves,
                complete_scope=True,
            )
            second.commit()
        except BaseException as exc:  # pragma: no cover - surfaced below
            result["two_error"] = exc

    one = threading.Thread(target=writer_one)
    two = threading.Thread(target=writer_two)
    one.start()
    assert entered.wait(timeout=10)
    two.start()
    time.sleep(0.25)
    release.set()
    one.join(timeout=10)
    two.join(timeout=10)
    try:
        assert "one_error" not in result
        assert "two_error" not in result
        assert result["one"].changed_pairs == 0
        assert result["two"].idempotent is True
    finally:
        first.close()
        second.close()
        engine.dispose()
