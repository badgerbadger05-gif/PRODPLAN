"""Test-first contract for the isolated R2 PostgreSQL contour."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).with_name("fixtures") / "r2_synthetic_seed.json"


def test_r2_contour_is_pg_only_and_never_starts_workers_or_external_hosts():
    compose = (ROOT / "docker-compose.r2.yml").read_text(encoding="utf-8")
    assert "postgres:" in compose
    assert "sync-worker" not in compose
    assert "reconcile-worker" not in compose
    assert "extra_hosts" not in compose
    assert "127.0.0.1" in compose
    assert "prodplan_r2" in compose
    assert "prodplan" not in compose.replace("prodplan_r2", "")


def test_r2_start_verify_entrypoint_is_documented_and_non_destructive():
    script = (ROOT / "scripts" / "r2-postgres.ps1").read_text(encoding="utf-8")
    assert "start-verify" in script
    assert "r2-postgres" in script
    assert "down -v" not in script
    assert "docker compose" in script
    docs = (ROOT / "docs" / "r2-local-contour.md").read_text(encoding="utf-8")
    assert "start-verify" in docs
    assert "PRODPLAN_R2_TEST_DSN" in docs


def test_synthetic_fixture_covers_r2_contract_without_credentials():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["seed"] == "r2-fixed-20260910-v1"
    assert len(data["plans"]) >= 2
    assert any(item["kind"] == "common" for item in data["items"])
    assert {pool["scope"] for pool in data["pools"]} == {"plan-a", "shared"}
    movement_kinds = {movement["kind"] for movement in data["movements"]}
    assert {"buy", "consume", "cancel", "rework", "material_custody", "close"} <= movement_kinds
    assert {movement.get("mode") for movement in data["movements"]} >= {"addressed", "aggregated", "fifo"}
    assert any("posting_at" in movement and "known_at" in movement for movement in data["movements"])
    assert data["mrp_change"]["does_not_copy_execution"] is True
    fixture_text = FIXTURE.read_text(encoding="utf-8").lower()
    assert "password" not in fixture_text
    assert "secret" not in fixture_text
    assert "token" not in fixture_text


def test_r2_guard_rejects_external_and_non_r2_databases():
    from app.r2_local_contract import validate_r2_dsn

    with pytest.raises(ValueError, match="local"):
        validate_r2_dsn("postgresql://r2_user:r2_local_only@db.example.invalid:5432/prodplan_r2")
    with pytest.raises(ValueError, match="identity"):
        validate_r2_dsn("postgresql://r2_user:r2_local_only@127.0.0.1:5432/prodplan")
    with pytest.raises(ValueError, match="explicit"):
        validate_r2_dsn("postgresql://r2_user:r2_local_only@127.0.0.1:5432/postgres")


def test_r2_guard_accepts_only_explicit_local_identity():
    from app.r2_local_contract import validate_r2_dsn

    parsed = validate_r2_dsn("postgresql://r2_user:r2_local_only@127.0.0.1:55441/prodplan_r2")
    assert parsed.host == "127.0.0.1"
    assert parsed.database == "prodplan_r2"

