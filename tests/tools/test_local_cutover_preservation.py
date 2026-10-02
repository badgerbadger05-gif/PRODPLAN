import json
import os

import pytest

from tools import local_cutover_migration as runner


DATABASE = "prodplan_weekend_20261002"
URL = f"postgresql://weekend_rehearsal:secret@127.0.0.1:55441/{DATABASE}"


def _restore_receipt():
    return {
        "receipt_version": 1, "status": "restored", "verified_backup": True,
        "restore_verified": True, "source_revision": runner.SOURCE_REVISION,
        "backup_sha256": "a" * 64, "source_dump_bytes": 123,
        "target": {"host": "127.0.0.1", "port": 55441, "database": DATABASE, "user": "weekend_rehearsal"},
    }


@pytest.mark.parametrize("url", [
    URL.replace("127.0.0.1", "mtzdock.lan"),
    URL.replace(":55441", ":5432"),
    URL.replace(":55441", ""),
    URL + "?host=mtzdock.lan",
    URL + "?options=-c%20search_path=public",
    URL.replace(DATABASE, "prodplan_prod_20260930"),
    URL.replace("weekend_rehearsal", "postgres"),
    "sqlite:///prodplan_weekend_20261002.db",
])
def test_remote_live_and_routing_override_dsns_blocked_before_engine_creation(url, monkeypatch, tmp_path):
    opened = []
    monkeypatch.setattr(runner, "create_engine", lambda *a, **k: opened.append(a))
    with pytest.raises(runner.RehearsalBlocked):
        runner.run_rehearsal(url, expected_database=DATABASE, restore_receipt=_restore_receipt(), writers_stopped=True, output=tmp_path / "run.json")
    assert opened == []


@pytest.mark.parametrize("patch", [
    {"verified_backup": False}, {"restore_verified": False},
    {"source_revision": "wrong"}, {"backup_sha256": "bad"},
    {"source_dump_bytes": 0}, {"source_dump_bytes": True},
    {"target": {"host": "127.0.0.1", "port": 55442, "database": DATABASE, "user": "weekend_rehearsal"}},
])
def test_unverified_or_wrong_restore_receipt_blocks_before_connection(patch):
    receipt = dict(_restore_receipt(), **patch)
    with pytest.raises(runner.RehearsalBlocked):
        runner.validate_target(URL, DATABASE, receipt, writers_stopped=True)


def test_writers_stop_acknowledgement_required():
    with pytest.raises(runner.RehearsalBlocked, match="writers_stopped_required"):
        runner.validate_target(URL, DATABASE, _restore_receipt(), writers_stopped=False)


def _fake_pipeline(monkeypatch, tmp_path):
    calls = []

    class Engine:
        def dispose(self):
            calls.append("dispose")

    def connect(url, **options):
        assert os.environ["DATABASE_URL"] == URL
        assert os.environ["PGOPTIONS"] == runner.PGOPTIONS
        assert options == {"connect_args": {"options": runner.PGOPTIONS}}
        calls.append("connect")
        return Engine()

    monkeypatch.setattr(runner, "create_engine", connect)
    monkeypatch.setattr(runner, "_check_connection_identity", lambda *a: {"database": DATABASE})

    def capture(engine):
        calls.append("capture")
        return {"revision": runner.SOURCE_REVISION, "tables": {}}

    def upgrade(config, revision):
        assert (tmp_path / "run-before.json").is_file()
        calls.append("upgrade:" + revision)

    def preflight(engine):
        calls.append("preflight")
        return {"status": "ready"}

    def apply(engine, *, writers_stopped):
        assert writers_stopped is True
        calls.append("apply")
        return {"status": "ready"}

    def postflight(engine, *, generation_id):
        assert generation_id == 123
        calls.append("postflight")
        return {"status": "ready"}

    def preservation(engine, before):
        assert before == json.loads((tmp_path / "run-before.json").read_text(encoding="utf-8"))
        calls.append("preservation")
        return {"status": "passed"}

    monkeypatch.setattr(runner, "capture_before", capture)
    monkeypatch.setattr(runner.command, "upgrade", upgrade)
    monkeypatch.setattr(runner, "build_manifest", preflight)
    monkeypatch.setattr(runner, "apply_current_obligation_migration", apply)
    monkeypatch.setattr(runner, "_accepted_truth_generation", lambda e: 123)
    monkeypatch.setattr(runner, "postflight_manifest", postflight)
    monkeypatch.setattr(runner, "verify_preservation", preservation)
    return calls


def test_runner_uses_canonical_order_captures_before_any_mutation_and_keeps_evidence(monkeypatch, tmp_path):
    calls = _fake_pipeline(monkeypatch, tmp_path)
    monkeypatch.setenv("PGOPTIONS", "original-options")
    monkeypatch.setenv("DATABASE_URL", "original-url")
    output = tmp_path / "run.json"
    result = runner.run_rehearsal(URL, expected_database=DATABASE, restore_receipt=_restore_receipt(), writers_stopped=True, output=output)
    assert result["status"] == "passed"
    assert calls == ["connect", "capture", "upgrade:20260911_01", "preflight", "apply", "postflight", "upgrade:head", "preservation", "dispose"]
    evidence = output.read_bytes()
    assert b"secret" not in evidence and URL.encode() not in evidence
    assert os.environ["PGOPTIONS"] == "original-options"
    assert os.environ["DATABASE_URL"] == "original-url"
    with pytest.raises(runner.RehearsalBlocked, match="new_receipt"):
        runner.run_rehearsal(URL, expected_database=DATABASE, restore_receipt=_restore_receipt(), writers_stopped=True, output=output)
    assert output.read_bytes() == evidence
    assert calls.count("connect") == 1


def test_dry_run_never_invokes_migrations_or_publishers(monkeypatch, tmp_path):
    calls = _fake_pipeline(monkeypatch, tmp_path)
    result = runner.run_rehearsal(URL, expected_database=DATABASE, restore_receipt=_restore_receipt(), writers_stopped=True, output=tmp_path / "run.json", dry_run=True)
    assert result["status"] == "dry_run_passed"
    assert calls == ["connect", "capture", "dispose"]


@pytest.mark.parametrize("failure_stage,expected_last", [
    ("capture_before", "capture"),
    ("build_manifest", "upgrade:20260911_01"),
    ("postflight_manifest", "apply"),
    ("verify_preservation", "upgrade:head"),
])
def test_pipeline_stops_on_failed_gate_and_never_exposes_exception_dsn(monkeypatch, tmp_path, failure_stage, expected_last):
    calls = _fake_pipeline(monkeypatch, tmp_path)

    def fail(*a, **k):
        raise RuntimeError("connection password secret in " + URL)

    monkeypatch.setattr(runner, failure_stage, fail)
    result = runner.run_rehearsal(URL, expected_database=DATABASE, restore_receipt=_restore_receipt(), writers_stopped=True, output=tmp_path / "run.json")
    assert result["status"] == "failed"
    assert result["error_type"] == "RuntimeError"
    assert calls[-1] == "dispose"
    if failure_stage == "capture_before":
        assert calls == ["connect", "dispose"]
    else:
        assert calls[-2] == expected_last
    assert "secret" not in (tmp_path / "run.json").read_text(encoding="utf-8")


def test_alembic_percent_encoded_credentials_are_not_interpolated_or_double_encoded():
    percent_url = URL.replace("secret", "p%40ss%25word")
    config = runner._config(percent_url)
    assert config.get_main_option("sqlalchemy.url") == percent_url
    # The actual env.py repeats this setter from DATABASE_URL.
    config.set_main_option("sqlalchemy.url", percent_url)
    assert config.get_main_option("sqlalchemy.url") == percent_url
