from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_retired_compose_has_no_services() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.test.yml").read_text(encoding="utf-8"))

    assert compose["name"] == "prodplan-retired-9010"
    assert compose["services"] == {}


def test_live_stack_has_no_retired_fallback_configuration() -> None:
    live_compose = (ROOT / "docker-compose.shadow.yml").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.shadow.example").read_text(encoding="utf-8")
    frontend_dockerfile = (
        ROOT / "frontend-erp-shell" / "Dockerfile"
    ).read_text(encoding="utf-8")

    retired_keys = (
        "PRODPLAN_STABLE_URL",
        "PRODPLAN_STABLE_API_URL",
        "STABLE_PRODPLAN_API_URL",
        "VITE_STABLE_PRODPLAN_URL",
    )
    for key in retired_keys:
        assert key not in live_compose
        assert key not in env_example
        assert key not in frontend_dockerfile


def test_retired_frontend_origin_is_not_allowed_by_default() -> None:
    main_module = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")

    assert "_DEFAULT_FRONTEND_PORTS = (9000, 9020, 9300)" in main_module
    assert "_DEFAULT_FRONTEND_PORTS = (9000, 9010, 9020, 9300)" not in main_module
