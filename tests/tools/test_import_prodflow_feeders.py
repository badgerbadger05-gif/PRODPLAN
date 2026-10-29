import pytest

from tools.import_prodflow_feeders import _active, _validated_database_target


def test_database_target_requires_explicit_matching_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="explicitly set"):
        _validated_database_target(expected_host="db", expected_database="prodplan")

    monkeypatch.setenv("DATABASE_URL", "postgresql://user:secret@db:5432/prodplan")
    assert _validated_database_target(
        expected_host="db", expected_database="prodplan"
    ) == ("db", "prodplan")
    with pytest.raises(ValueError, match="target mismatch"):
        _validated_database_target(expected_host="other", expected_database="prodplan")


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, True), (1, True), ("1", True), (False, False), (0, False), ("0", False)],
)
def test_active_flag_is_strict(value, expected):
    assert _active(value) is expected


@pytest.mark.parametrize("value", [None, "true", "yes", 2])
def test_active_flag_rejects_malformed_values(value):
    with pytest.raises(ValueError, match="invalid is_active"):
        _active(value)
