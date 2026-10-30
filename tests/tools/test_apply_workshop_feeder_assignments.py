import json
import sys

from tools import apply_workshop_feeder_assignments as tool


def test_apply_validates_and_posts_all_settings_atomically(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "decision_date": "2026-10-30",
        "assignments": [{
            "workshop_id": 5,
            "workshop_name": "Weld",
            "feeder_warehouse_ref1c": "FEEDER",
            "feeder_warehouse_name": "Feeder warehouse",
        }],
    }), encoding="utf-8")
    calls = []

    def request(_url, *, payload=None):
        calls.append(payload)
        if payload is None:
            return {
                "warehouses": [{"warehouse_ref1c": "FEEDER", "warehouse_name": "Feeder warehouse"}],
                "workshop_warehouses": [{
                    "workshop_id": 5, "workshop_name": "Weld",
                    "warehouse_ref1c": "DEST", "production_warehouse_ref1c": "OUTPUT",
                }],
                "ignored_warehouses": [{"warehouse_ref1c": "IGNORED"}],
            }
        return {"workshop_warehouses": payload["workshop_warehouses"]}

    monkeypatch.setattr(tool, "_json", request)
    monkeypatch.setattr(sys, "argv", [
        "apply_workshop_feeder_assignments.py",
        "--base-url", "http://backend:8000",
        "--manifest", str(manifest),
        "--apply",
    ])

    assert tool.main() == 0
    assert len(calls) == 2
    assert calls[1]["workshop_warehouses"][0] == {
        "workshop_id": 5,
        "workshop_name": "Weld",
        "warehouse_ref1c": "DEST",
        "production_warehouse_ref1c": "OUTPUT",
        "feeder_warehouse_ref1c": "FEEDER",
    }
    assert calls[1]["ignored_warehouses"] == [{"warehouse_ref1c": "IGNORED"}]


def test_dry_run_rejects_catalog_mismatch(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "assignments": [{
            "workshop_id": 5, "workshop_name": "Weld",
            "feeder_warehouse_ref1c": "FEEDER", "feeder_warehouse_name": "Expected",
        }],
    }), encoding="utf-8")
    monkeypatch.setattr(tool, "_json", lambda *_args, **_kwargs: {
        "warehouses": [{"warehouse_ref1c": "FEEDER", "warehouse_name": "Wrong"}],
        "workshop_warehouses": [{"workshop_id": 5, "workshop_name": "Weld"}],
    })
    monkeypatch.setattr(sys, "argv", [
        "apply_workshop_feeder_assignments.py", "--base-url", "http://backend:8000",
        "--manifest", str(manifest),
    ])

    import pytest
    with pytest.raises(ValueError, match="warehouse mismatch"):
        tool.main()
