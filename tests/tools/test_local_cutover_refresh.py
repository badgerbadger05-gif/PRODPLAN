from datetime import datetime, timedelta, timezone
import urllib.request

import pytest

from tools import local_cutover_refresh as runner


def test_target_guard_rejects_every_external_or_ambiguous_target():
    database = "prodplan_weekend_20261002"
    valid = f"postgresql://clone:local-secret@127.0.0.1:55441/{database}"
    assert runner.validate_target(valid, database=database, port=55441).database == database
    for target in (
        valid.replace("127.0.0.1", "mtzdock.lan"),
        valid.replace("127.0.0.1", "localhost"),
        valid.replace(":55441/", ":5432/"),
        valid.replace(database, "prodplan"),
        valid + "?host=mtzdock.lan",
        valid.replace("postgresql", "sqlite"),
    ):
        with pytest.raises(ValueError, match="local refresh requires"):
            runner.validate_target(target, database=database, port=55441)
    with pytest.raises(ValueError, match="local refresh requires"):
        runner.validate_target(valid, database="prodplan", port=55441)


def test_cutoff_requires_real_past_instant_with_timezone():
    now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    assert runner.parse_cutoff("2026-10-02T14:00:00+03:00", now=now).hour == 11
    with pytest.raises(ValueError, match="timezone"):
        runner.parse_cutoff("2026-10-02T11:00:00", now=now)
    with pytest.raises(ValueError, match="future"):
        runner.parse_cutoff((now + timedelta(seconds=1)).isoformat(), now=now)


@pytest.mark.parametrize("seconds,rows", [(0, 1), (-1, 1), (float("inf"), 1), (float("nan"), 1), (600, 0)])
def test_limits_must_be_declared_positive_and_finite(seconds, rows):
    with pytest.raises(ValueError, match="explicit positive finite"):
        runner.validate_limits(seconds, rows)


def test_get_only_guard_blocks_raw_http_and_client_writes_and_restores(monkeypatch):
    from app.services.odata_client import OData1CClient

    calls = []
    def fake_open(*args, **kwargs):
        calls.append((args, kwargs))
        return "read-response"

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", fake_open)
    original_write = OData1CClient._send_write
    with runner.get_only_odata():
        assert urllib.request.urlopen("https://example.invalid/odata") == "read-response"
        for request in (
            urllib.request.Request("https://example.invalid/odata", method="POST"),
            urllib.request.Request("https://example.invalid/odata", method="PATCH"),
            urllib.request.Request("https://example.invalid/odata", method="DELETE"),
        ):
            with pytest.raises(RuntimeError, match="writes forbidden"):
                urllib.request.urlopen(request)
            with pytest.raises(RuntimeError, match="writes forbidden"):
                urllib.request.build_opener().open(request)
        with pytest.raises(RuntimeError, match="writes forbidden"):
            urllib.request.urlopen("https://example.invalid/odata", data=b"body")
        with pytest.raises(RuntimeError, match="writes forbidden"):
            urllib.request.urlopen(urllib.request.Request(
                "https://example.invalid/odata", method="GET", data=b"body"))
        with pytest.raises(RuntimeError, match="1C writes"):
            OData1CClient("https://example.invalid/odata").post("Document_Test", {})
    assert len(calls) == 1
    assert urllib.request.urlopen is fake_open
    assert OData1CClient._send_write is original_write


def test_budget_receipt_distinguishes_real_cycles_noop_and_baseline_failure():
    limits = runner.validate_limits(900, 10000)
    metrics = {"duration_ms": 780000, "input_delta_rows": 100,
               "replayed_rows": 1000, "database_ledger_rows": 280000,
               "affected_scopes": ["8033:::default:buy"]}
    verdict = runner.evaluate_cycle(metrics, limits=limits, elapsed=800,
                                    accepted_pointer=True, scopes_ready=True)
    assert verdict["passed"] and verdict["counts_as_changing_cycle"]
    assert not verdict["tracked_r11_budget"]["within_budget"]
    noop = runner.evaluate_cycle({**metrics, "input_delta_rows": 0, "replayed_rows": 0},
                                limits=limits, elapsed=800, accepted_pointer=True, scopes_ready=True)
    assert noop["passed"] and noop["kind"] == "no_op"
    assert not noop["counts_as_changing_cycle"]
    for changed in ({"elapsed": 901}, {"accepted_pointer": False}, {"scopes_ready": False}):
        options = {"limits": limits, "elapsed": 800, "accepted_pointer": True, "scopes_ready": True}
        options.update(changed)
        assert not runner.evaluate_cycle(metrics, **options)["passed"]
    assert not runner.evaluate_cycle({**metrics, "input_delta_rows": 0}, limits=limits,
                                     elapsed=800, accepted_pointer=True, scopes_ready=True)["passed"]
