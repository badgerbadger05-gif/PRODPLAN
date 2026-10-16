from types import SimpleNamespace

import pytest

from app.services.item_ledger.live_plan_scope import (
    sealed_generation_lineage_ids,
    sealed_lineage_read_batch,
)


class LineageSession:
    def __init__(self, length=128):
        self.calls = 0
        self.rows = {
            key: SimpleNamespace(
                id=key, status="accepted",
                source_watermarks={"parent_generation_id": key - 1} if key > 1 else {},
            )
            for key in range(1, length + 1)
        }

    def get(self, _model, key):
        self.calls += 1
        return self.rows.get(key)


def test_read_batch_validates_immutable_lineage_once_and_drops_proof_after_exit():
    db = LineageSession()
    root = db.rows[128]
    expected = tuple(range(128, 0, -1))
    with sealed_lineage_read_batch(db):
        for _ in range(24):
            assert sealed_generation_lineage_ids(db, root) == expected
    assert db.calls == 127
    assert sealed_generation_lineage_ids(db, root) == expected
    assert db.calls == 254


@pytest.mark.parametrize("broken", ["missing", "cycle", "malformed"])
def test_invalid_lineage_never_becomes_a_cached_partial_proof(broken):
    db = LineageSession(3)
    if broken == "missing":
        del db.rows[2]
    elif broken == "cycle":
        db.rows[1].source_watermarks = {"parent_generation_id": 3}
    else:
        db.rows[2].source_watermarks = {"parent_generation_id": "broken"}
    with sealed_lineage_read_batch(db):
        for _ in range(2):
            with pytest.raises(ValueError):
                sealed_generation_lineage_ids(db, db.rows[3])


def test_building_lineage_is_not_cached_and_session_proofs_are_isolated():
    db = LineageSession(3)
    other = LineageSession(3)
    db.rows[3].status = "building"
    with sealed_lineage_read_batch(db):
        assert sealed_generation_lineage_ids(db, db.rows[3]) == (3, 2, 1)
        db.rows[3].source_watermarks = {"parent_generation_id": 1}
        assert sealed_generation_lineage_ids(db, db.rows[3]) == (3, 1)
        assert sealed_generation_lineage_ids(other, other.rows[3]) == (3, 2, 1)
        assert sealed_generation_lineage_ids(other, other.rows[3]) == (3, 2, 1)
    assert other.calls == 4
