from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

from rebuild_ledger_history import (  # noqa: E402
    DatabaseRuntime,
    GenerationState,
    ReplayError,
    ReplayManifest,
    _backend_dir,
    replay_history,
)


def _raw_manifest():
    return {
        "opening_at": "2026-05-31T23:59:59.999999+03:00",
        "replay_from": "2026-06-02T00:00:00+03:00",
        "bootstrap_cutoff": "2026-06-02T17:12:50+03:00",
        "bootstrap_key": "bootstrap",
        "required_assembly_item_codes": ["SKU-1", "SKU-11"],
        "plans": [
            {
                "plan_id": 1,
                "cutoff": "2026-06-02T17:12:50+03:00",
                "physical_key": None,
                "obligation_key": "obligation-1",
            },
            {
                "plan_id": 11,
                "cutoff": "2026-07-28T14:30:24+03:00",
                "physical_key": "physical-11",
                "obligation_key": "obligation-11",
            },
        ],
    }


def test_manifest_requires_offsets_and_first_plan_at_bootstrap():
    raw = _raw_manifest()
    raw["opening_at"] = "2026-05-31T23:59:59"
    with pytest.raises(ReplayError, match="explicit UTC offset"):
        ReplayManifest.from_mapping(raw)

    raw = _raw_manifest()
    raw["plans"][0]["physical_key"] = "must-not-refresh"
    with pytest.raises(ReplayError, match="first plan"):
        ReplayManifest.from_mapping(raw)


def test_manifest_rejects_unordered_cutoffs_and_duplicate_keys():
    raw = _raw_manifest()
    raw["plans"][1]["cutoff"] = raw["plans"][0]["cutoff"]
    with pytest.raises(ReplayError, match="strictly increasing"):
        ReplayManifest.from_mapping(raw)

    raw = _raw_manifest()
    raw["plans"][1]["physical_key"] = "obligation-1"
    with pytest.raises(ReplayError, match="generation keys"):
        ReplayManifest.from_mapping(raw)


class FakeRuntime:
    def __init__(self):
        self.states = {}
        self.current = None
        self.next_id = 1
        self.snapshots = set()
        self.calls = []
        self.import_results = [False, True]
        self.valid_balance = False
        self.preflight_error = None

    def preflight_assembly_rates(self, item_codes):
        self.calls.append(("preflight", tuple(item_codes)))
        if self.preflight_error is not None:
            raise ReplayError(self.preflight_error)

    def preflight_planning_pools(self):
        self.calls.append(("preflight_planning_pools",))

    def _add(self, key, cutoff, parent, status="accepted", historical=None, replay=None):
        state = GenerationState(
            self.next_id, key, status, cutoff, parent, historical, replay
        )
        self.next_id += 1
        self.states[key] = state
        return state

    def generation(self, key):
        return self.states.get(key)

    def current_generation_id(self):
        return self.current

    def bootstrap(self, manifest):
        self.calls.append(("bootstrap", manifest.bootstrap_key))
        state = self._add(
            manifest.bootstrap_key,
            manifest.bootstrap_cutoff,
            None,
            "building",
            manifest.opening_at,
            manifest.replay_from,
        )
        return state

    def import_once(self, generation_id):
        self.calls.append(("import", generation_id))
        return self.import_results.pop(0)

    def balance_is_valid(self, generation_id):
        return self.valid_balance

    def verify_balance(self, generation_id):
        self.calls.append(("verify", generation_id))
        self.valid_balance = True
        return True

    def accept_bootstrap(self, generation_id, replay_from):
        self.calls.append(("accept", generation_id))
        old = self.states["bootstrap"]
        self.states["bootstrap"] = GenerationState(
            old.generation_id,
            old.key,
            "accepted",
            old.cutoff,
            old.parent_generation_id,
            old.historical_from,
            old.replay_from,
        )
        self.current = generation_id

    def fixed_snapshot_exists(self, generation_id, plan_id):
        return (generation_id, plan_id) in self.snapshots

    def physical_refresh(self, cutoff, key):
        self.calls.append(("physical", key))
        state = self._add(key, cutoff, self.current)
        self.current = state.generation_id

    def create_snapshot(self, plan_id, key):
        self.calls.append(("snapshot", plan_id, key))
        cutoff = next(
            plan.cutoff for plan in self.manifest.plans if plan.plan_id == plan_id
        )
        state = self._add(key, cutoff, self.current)
        self.snapshots.add((state.generation_id, plan_id))
        self.current = state.generation_id

    def commit(self):
        self.calls.append(("commit",))

    def fix_plan(self, plan_id):
        self.calls.append(("fix", plan_id))

    def rollback(self):
        self.calls.append(("rollback",))


def test_replay_sequences_bootstrap_then_plans_and_is_idempotent():
    manifest = ReplayManifest.from_mapping(_raw_manifest())
    runtime = FakeRuntime()
    runtime.manifest = manifest

    result = replay_history(runtime, manifest, max_import_iterations=3)

    assert result["plans"] == [1, 11]
    assert runtime.calls == [
        ("preflight", ("SKU-1", "SKU-11")),
        ("preflight_planning_pools",),
        ("bootstrap", "bootstrap"),
        ("import", 1),
        ("import", 1),
        ("verify", 1),
        ("accept", 1),
        ("snapshot", 1, "obligation-1"),
        ("commit",),
        ("fix", 1),
        ("physical", "physical-11"),
        ("snapshot", 11, "obligation-11"),
        ("commit",),
        ("fix", 11),
    ]

    runtime.calls.clear()
    second = replay_history(runtime, manifest, max_import_iterations=3)
    assert second == result
    assert runtime.calls == [
        ("preflight", ("SKU-1", "SKU-11")),
        ("preflight_planning_pools",),
    ]


def test_replay_is_bounded_and_does_not_accept_partial_import():
    manifest = ReplayManifest.from_mapping(_raw_manifest())
    runtime = FakeRuntime()
    runtime.manifest = manifest
    runtime.import_results = [False, False]

    with pytest.raises(ReplayError, match="max_import_iterations"):
        replay_history(runtime, manifest, max_import_iterations=2)
    assert not any(call[0] == "accept" for call in runtime.calls)


def test_resume_refuses_existing_key_with_wrong_lineage():
    manifest = ReplayManifest.from_mapping(_raw_manifest())
    runtime = FakeRuntime()
    runtime.manifest = manifest
    bootstrap = runtime._add(
        "bootstrap",
        manifest.bootstrap_cutoff,
        None,
        "accepted",
        manifest.opening_at,
        manifest.replay_from,
    )
    runtime.valid_balance = True
    runtime.current = bootstrap.generation_id
    wrong = runtime._add(
        "obligation-1", manifest.plans[0].cutoff, 999, "accepted"
    )
    runtime.snapshots.add((wrong.generation_id, 1))

    with pytest.raises(ReplayError, match="different lineage"):
        replay_history(runtime, manifest, max_import_iterations=1)
    assert runtime.calls == [
        ("preflight", ("SKU-1", "SKU-11")),
        ("preflight_planning_pools",),
    ]


def test_resume_refuses_accepted_obligation_without_fixed_snapshot():
    manifest = ReplayManifest.from_mapping(_raw_manifest())
    runtime = FakeRuntime()
    runtime.manifest = manifest
    bootstrap = runtime._add(
        "bootstrap",
        manifest.bootstrap_cutoff,
        None,
        "accepted",
        manifest.opening_at,
        manifest.replay_from,
    )
    runtime.valid_balance = True
    runtime.current = bootstrap.generation_id
    runtime._add(
        "obligation-1",
        manifest.plans[0].cutoff,
        bootstrap.generation_id,
        "accepted",
    )

    with pytest.raises(ReplayError, match="no unique fixed snapshot"):
        replay_history(runtime, manifest, max_import_iterations=1)


@pytest.mark.parametrize(
    "message",
    [
        "assembly-rate preflight failed; items not found: SKU-11",
        "assembly-rate preflight failed; ambiguous assembly rates: SKU-11",
    ],
)
def test_preflight_failure_stops_before_any_replay_mutation(message):
    manifest = ReplayManifest.from_mapping(_raw_manifest())
    runtime = FakeRuntime()
    runtime.manifest = manifest
    runtime.preflight_error = message

    with pytest.raises(ReplayError, match="assembly-rate preflight failed"):
        replay_history(runtime, manifest, max_import_iterations=1)
    assert runtime.calls == [("preflight", ("SKU-1", "SKU-11"))]


def _database_runtime(db_session):
    return DatabaseRuntime(
        db_session,
        window_hours=24,
        page_size=1000,
        max_pages_per_window=10_000,
    )


def test_database_preflight_rejects_missing_item_and_missing_rate(db_session):
    from app import models

    db_session.add(models.Item(item_code="NO-RATE", item_name="No rate"))
    db_session.commit()

    with pytest.raises(ReplayError, match="items not found: UNKNOWN"):
        _database_runtime(db_session).preflight_assembly_rates(["UNKNOWN"])
    with pytest.raises(ReplayError, match="missing positive rate.*NO-RATE"):
        _database_runtime(db_session).preflight_assembly_rates(["NO-RATE"])


def test_database_preflight_rejects_ambiguous_positive_rates(db_session):
    from app import models

    item = models.Item(item_code="AMBIGUOUS", item_name="Ambiguous")
    first = models.ProductionResource(resource_name="First")
    second = models.ProductionResource(resource_name="Second")
    db_session.add_all([item, first, second])
    db_session.flush()
    db_session.add_all(
        [
            models.AssemblyRate(
                item_id=item.item_id,
                resource_id=first.resource_id,
                qty_per_capacity=1,
            ),
            models.AssemblyRate(
                item_id=item.item_id,
                resource_id=second.resource_id,
                qty_per_capacity=2,
            ),
        ]
    )
    db_session.commit()

    with pytest.raises(ReplayError, match="ambiguous assembly rates: AMBIGUOUS"):
        _database_runtime(db_session).preflight_assembly_rates(["AMBIGUOUS"])


def test_preflight_only_path_performs_no_replay(monkeypatch, tmp_path, capsys):
    import json
    import rebuild_ledger_history as tool

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_raw_manifest()), encoding="utf-8")
    calls = []

    class PreflightRuntime:
        def __init__(self, *args, **kwargs):
            pass

        def preflight_assembly_rates(self, codes):
            calls.append(tuple(codes))

        def preflight_planning_pools(self):
            calls.append(("planning-pools",))

    class Session:
        def rollback(self):
            raise AssertionError("preflight-only must not mutate or roll back")

        def close(self):
            calls.append(("closed",))

    monkeypatch.setattr(tool, "DatabaseRuntime", PreflightRuntime)
    monkeypatch.setitem(
        sys.modules,
        "app.database",
        type("DatabaseModule", (), {"SessionLocal": staticmethod(Session)}),
    )
    assert tool.main([str(manifest_path), "--preflight-only"]) == 0
    assert calls == [
        ("SKU-1", "SKU-11"),
        ("planning-pools",),
        ("closed",),
    ]
    assert '"status": "preflight-ok"' in capsys.readouterr().out


def test_backend_dir_honors_explicit_container_source(monkeypatch, tmp_path):
    backend = tmp_path / "mounted-backend"
    app = backend / "app"
    app.mkdir(parents=True)
    (app / "database.py").write_text("", encoding="utf-8")

    monkeypatch.setenv("PRODPLAN_BACKEND_DIR", str(backend))

    assert _backend_dir() == backend.resolve()
