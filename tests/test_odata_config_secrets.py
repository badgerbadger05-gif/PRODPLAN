"""A2 (deploy-safety): masking 1C secrets in the API must not let the SPA
round-trip the "***" placeholder back and overwrite the real stored password.
"""

import json

from app.services import odata_config as oc


def test_resolve_keeps_stored_secret_when_masked(monkeypatch):
    monkeypatch.setattr(oc, "load_odata_config", lambda: {"base_url": "u", "password": "realpass", "token": "realtok"})
    resolved = oc.resolve_config_secrets({"base_url": "u", "password": "***", "token": "***", "username": "x"})
    assert resolved["password"] == "realpass"
    assert resolved["token"] == "realtok"
    assert resolved["username"] == "x"


def test_resolve_accepts_a_genuinely_new_secret(monkeypatch):
    monkeypatch.setattr(oc, "load_odata_config", lambda: {"password": "old"})
    resolved = oc.resolve_config_secrets({"password": "newpass"})
    assert resolved["password"] == "newpass"


def test_mask_then_resolve_roundtrip_preserves_secret(monkeypatch):
    stored = {"base_url": "u", "password": "secret", "token": ""}
    monkeypatch.setattr(oc, "load_odata_config", lambda: stored)
    masked = oc.mask_odata_config(stored)
    assert masked["password"] == "***"
    assert masked["token"] == ""
    # The SPA posts the masked config back unchanged.
    resolved = oc.resolve_config_secrets(masked)
    assert resolved["password"] == "secret"
    assert resolved["token"] == ""


def test_save_does_not_overwrite_real_secret_with_mask(monkeypatch, tmp_path):
    cfg_file = tmp_path / "odata_config.json"
    cfg_file.write_text(json.dumps({"base_url": "http://x", "password": "realsecret", "token": "realtok"}), "utf-8")
    monkeypatch.setattr(oc, "CONFIG_PATH", cfg_file)

    # SPA loaded the masked config and saved it back without retyping the password.
    oc.save_odata_config({"base_url": "http://x", "password": "***", "token": "***", "username": "u"})

    saved = json.loads(cfg_file.read_text("utf-8"))
    assert saved["password"] == "realsecret"
    assert saved["token"] == "realtok"
    assert saved["username"] == "u"
