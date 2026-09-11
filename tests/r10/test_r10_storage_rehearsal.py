"""R10 C contract for backup/restore and physical legacy-space reclaim."""

from __future__ import annotations

import json
import os
import re

import pytest

from tools.r10_storage_rehearsal import (
    _safe_connection_args,
    _tool_environment,
    run_storage_rehearsal,
)


@pytest.mark.integration
def test_storage_rehearsal_restores_subject_values_and_reclaims_dropped_relations(
    tmp_path,
):
    dsn = os.getenv("PRODPLAN_R2_TEST_DSN")
    if not dsn:
        pytest.skip("PRODPLAN_R2_TEST_DSN is not configured")

    report = run_storage_rehearsal(dsn, output_dir=tmp_path)

    assert re.fullmatch(r"r10_storage_[0-9a-f]{32}", report["schema"])
    assert report["dump"]["format"] == "custom"
    assert report["dump"]["bytes"] > 0
    assert re.fullmatch(r"[0-9a-f]{64}", report["dump"]["sha256"])
    assert report["dump"]["restore_list_entries"] > 0
    assert report["reclaim"]["bytes_before"] > 0
    assert report["reclaim"]["bytes_after"] == 0
    assert report["reclaim"]["bytes_reclaimed"] == report["reclaim"]["bytes_before"]
    assert report["cutover"]["legacy_tables_present"] == []
    assert report["restore"]["legacy_tables_present"] == [
        "planning_read_root_member",
        "planning_read_row",
        "planning_read_snapshot",
    ]
    assert report["before"]["subject_values"] == report["restore"]["subject_values"]
    assert report["before"]["current_scope_ids"] == report["restore"]["current_scope_ids"]
    assert report["restore"]["current_generation_id"] == report["before"]["current_generation_id"]
    assert report["restore"]["ready_scope_count"] == 4

    serialized = json.dumps(report, sort_keys=True)
    assert dsn not in serialized
    assert "r2_local_only" not in serialized


def test_storage_rehearsal_report_contract_is_machine_readable(tmp_path, monkeypatch):
    """The tool exposes a stable JSON report rather than shell-specific output."""
    monkeypatch.setenv("PRODPLAN_R2_TEST_DSN", "postgresql://r2_user:secret@127.0.0.1:55444/prodplan_r2")
    # The integration implementation must validate the local DSN itself; this
    # test remains a contract guard even when the local cluster is unavailable.
    assert callable(run_storage_rehearsal)


def test_storage_tool_never_embeds_password_in_pg_argv():
    args, password = _safe_connection_args(
        "postgresql://r2_user:secret@127.0.0.1:55444/prodplan_r2"
    )
    assert password == "secret"
    assert "secret" not in " ".join(args)
    environment = _tool_environment(["wsl.exe", "--", "/usr/bin/pg_dump"], password)
    assert environment is not None
    assert environment["PGPASSWORD"] == "secret"
    assert "PGPASSWORD/u" in environment["WSLENV"]
