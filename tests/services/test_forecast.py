from datetime import date

from app.services.forecast import forecast_payload, forecast_status


def test_forecast_status_has_one_critical_threshold_owner():
    assert forecast_status(None) == "unavailable"
    assert forecast_status(-1) == "early"
    assert forecast_status(0) == "on_time"
    assert forecast_status(5) == "delayed"
    assert forecast_status(6) == "critical"


def test_forecast_payload_preserves_dates_reason_and_status():
    assert forecast_payload(date(2026, 8, 7), date(2026, 8, 1)) == {
        "forecast_date": "2026-08-07",
        "forecast_shift_days": 6,
        "forecast_reason": "смещение по мощностям",
        "forecast_status": "critical",
    }
