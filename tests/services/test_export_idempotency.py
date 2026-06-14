"""B2 / M-4: post_export_entries must persist each successful 1C export
immediately, so a crash after a POST but before the end-of-batch commit cannot
lose the stored Ref_Key and cause a duplicate document on re-run.
"""

import pytest

from app.models import SyncLink
from app.services.one_c_export_common import (
    post_export_entries,
    upsert_sync_link,
    find_sync_link,
)

TARGET = "Document_Test"
DOCTYPE = "test_doc"


class FakeEntry:
    def __init__(self, sid):
        self.source_id = sid
        self.target_ref_key = None
        self.status = None
        self.error = None


class FakeClient:
    """Stand-in for OData1CClient. Records POSTed ids; can fail on chosen ids."""

    def __init__(self, fail_on=None):
        self.fail_on = set(fail_on or ())
        self.posted = []

    def post(self, entity, payload):
        sid = payload.get("_sid")
        if sid in self.fail_on:
            raise RuntimeError("1C rejected document")
        self.posted.append(sid)
        return {"Ref_Key": f"ref-{sid}"}

    def patch(self, path, payload):
        return {}

    def post_operation(self, path):
        return None


def _make_upsert(db):
    def _upsert(*, entry, payload_hash, target_ref_key, status, last_error):
        upsert_sync_link(
            db,
            SyncLink,
            source_doctype=DOCTYPE,
            source_id=entry.source_id,
            target_entity=TARGET,
            target_number=f"N{entry.source_id}",
            payload_hash=payload_hash,
            target_ref_key=target_ref_key,
            status=status,
            last_error=last_error,
        )

    return _upsert


def _entries(ids):
    for i in ids:
        yield (FakeEntry(i), {"payload": {"_sid": i, "data": "x"}})


def _link(db, sid):
    return find_sync_link(db, SyncLink, source_doctype=DOCTYPE, source_id=sid, target_entity=TARGET)


def test_successful_export_is_committed_before_next_entry(db_session):
    """The regression: a crash mid-batch must not lose an already-POSTed doc."""
    db = db_session
    client = FakeClient()

    def crashing_entries():
        yield (FakeEntry(1), {"payload": {"_sid": 1, "data": "x"}})
        raise RuntimeError("process crash before end-of-batch commit")

    with pytest.raises(RuntimeError):
        post_export_entries(
            db,
            entries=crashing_entries(),
            client=client,
            target_entity=TARGET,
            missing_ref_error="no ref",
            upsert_link=_make_upsert(db),
            on_success=lambda e, rk: None,
        )

    # Emulate the lost in-memory state of a real crash: anything not committed
    # is gone. With per-entry commit, entry 1's success link survives.
    db.rollback()
    link = _link(db, 1)
    assert link is not None, "successful export was not durably committed -> re-run would duplicate"
    assert link.status == "success"
    assert link.target_ref_key == "ref-1"
    assert client.posted == [1]


def test_per_entry_commit_isolates_success_from_later_error(db_session):
    db = db_session
    client = FakeClient(fail_on={2})

    created, errored = post_export_entries(
        db,
        entries=_entries([1, 2]),
        client=client,
        target_entity=TARGET,
        missing_ref_error="no ref",
        upsert_link=_make_upsert(db),
        on_success=lambda e, rk: None,
    )

    assert (created, errored) == (1, 1)
    db.rollback()
    assert _link(db, 1).status == "success"
    assert _link(db, 1).target_ref_key == "ref-1"
    assert _link(db, 2).status == "error"
    assert client.posted == [1]


def test_post_succeeds_but_on_success_fails_keeps_ref_key(db_session):
    """If the doc was created in 1C but a later step fails, keep the Ref_Key so
    a re-run PATCHes the existing document instead of creating a duplicate."""
    db = db_session
    client = FakeClient()

    def bad_on_success(entry, ref_key):
        raise RuntimeError("operational posting failed")

    created, errored = post_export_entries(
        db,
        entries=_entries([1]),
        client=client,
        target_entity=TARGET,
        missing_ref_error="no ref",
        upsert_link=_make_upsert(db),
        on_success=bad_on_success,
    )

    assert (created, errored) == (0, 1)
    db.rollback()
    link = _link(db, 1)
    assert link is not None
    assert link.status == "error"
    assert link.target_ref_key == "ref-1"
    assert client.posted == [1]
