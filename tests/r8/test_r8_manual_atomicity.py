from pathlib import Path


def test_r8_current_manual_adapter_does_not_commit_before_outer_boundary():
    source = (
        Path(__file__).parents[2]
        / "backend"
        / "app"
        / "services"
        / "item_ledger"
        / "drum_manual_move.py"
    ).read_text(encoding="utf-8")
    body = source.split("def move_current_drum_slot(", 1)[1]
    body = body.split("\ndef ", 1)[0]
    assert "db.commit()" not in body
    assert "commit=False" in body
