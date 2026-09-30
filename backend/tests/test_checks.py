"""The provider and email checks, with Anthropic and Resend faked."""

import logging

import httpx
from sqlalchemy import select

from app import emails, secrets
from app.config import settings
from app.models.api_key import ANTHROPIC, ApiKey

FAKE_KEY = "sk-ant-test-not-a-real-key"


def _save_key(db, org, key=FAKE_KEY):
    row = ApiKey(org_id=org.id, provider=ANTHROPIC, ciphertext=secrets.encrypt(key))
    db.add(row)
    db.commit()
    return row


def test_provider_without_a_saved_key(client, anthropic):
    response = client.post("/checks/provider")

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["detail"].startswith("No key saved")
    assert anthropic.keys == []


def test_provider_with_a_working_key(client, org, db, anthropic):
    row = _save_key(db, org)

    response = client.post("/checks/provider")

    assert response.json()["ok"] is True
    # The decrypted key went to the provider, and nowhere else.
    assert anthropic.keys == [FAKE_KEY]
    assert FAKE_KEY not in response.text
    db.refresh(row)
    assert row.last_checked_at is not None
    assert row.last_error is None


def test_provider_refusal_is_reported_without_the_key(client, org, db, anthropic):
    row = _save_key(db, org)
    # A provider that quotes the key back must not get it onto the screen.
    anthropic.response = httpx.Response(
        401, json={"error": {"type": "authentication_error", "message": f"bad key {FAKE_KEY}"}}
    )

    response = client.post("/checks/provider")

    assert response.json()["ok"] is False
    assert "401" in response.json()["detail"]
    assert FAKE_KEY not in response.text
    db.refresh(row)
    assert row.last_error == response.json()["detail"]


def test_provider_unreachable(client, org, db, anthropic):
    _save_key(db, org)
    anthropic.error = httpx.ConnectError("name resolution failed")

    response = client.post("/checks/provider")

    assert response.json()["ok"] is False
    assert "Could not reach api.anthropic.com" in response.json()["detail"]


def test_provider_key_saved_with_another_master_key(client, org, db, monkeypatch):
    _save_key(db, org)
    monkeypatch.setattr("app.secrets._fernet_cache", None)
    monkeypatch.setattr(settings, "api_keys_encryption_key", "A" * 43 + "=")

    response = client.post("/checks/provider")

    assert response.json()["ok"] is False
    assert "cannot be decrypted" in response.json()["detail"]


def test_email_without_a_key_is_logged_not_sent(client, admin, resend, caplog):
    with caplog.at_level(logging.WARNING, logger="app.emails"):
        response = client.post("/checks/email")

    assert response.json()["ok"] is False
    assert "RESEND_API_KEY" in response.json()["detail"]
    assert resend.sent == []
    assert admin.email in caplog.text


def test_email_with_a_key_goes_to_the_current_user(client, admin, resend, monkeypatch):
    monkeypatch.setattr(settings, "resend_api_key", "re_test_not_real")

    response = client.post("/checks/email")

    assert response.json()["ok"] is True
    [(to, subject, _)] = resend.sent
    assert to == admin.email
    assert "test email" in subject


def test_email_refused_by_resend(client, resend, monkeypatch):
    monkeypatch.setattr(settings, "resend_api_key", "re_test_not_real")
    resend.error = emails.EmailError("Resend answered 403: testing emails only to yourself")

    response = client.post("/checks/email")

    assert response.json() == {
        "ok": False,
        "detail": "Resend answered 403: testing emails only to yourself",
    }


def test_encrypted_rows_are_versioned(db, org):
    row = _save_key(db, org)

    stored = db.scalar(select(ApiKey.ciphertext).where(ApiKey.id == row.id))

    assert stored.startswith(b"v1:")
    assert FAKE_KEY.encode() not in stored
    assert secrets.decrypt(stored) == FAKE_KEY
