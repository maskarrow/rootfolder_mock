"""Startup: files left `processing` become `interrupted`, and the two refusals."""

import pytest
from fastapi.testclient import TestClient

from app import main, secrets
from app.config import settings
from app.main import app
from app.models.api_key import ANTHROPIC, ApiKey
from app.models.file import DONE, PROCESSING, File


def _file(db, org, status):
    row = File(org_id=org.id, filename="f.pdf", stored_path="f.pdf", size_bytes=1, status=status)
    db.add(row)
    db.commit()
    return row


def test_files_left_processing_are_marked_interrupted(db, org):
    stuck = _file(db, org, PROCESSING)
    finished = _file(db, org, DONE)

    with TestClient(app):
        pass

    db.refresh(stuck)
    db.refresh(finished)
    assert stuck.status == "interrupted"
    assert stuck.finished_at is not None
    assert finished.status == "done"


def test_public_address_without_resend_key_refuses_to_start(monkeypatch):
    monkeypatch.setattr(settings, "app_url", "https://mock.example.com")
    monkeypatch.setattr(settings, "resend_api_key", "")

    with pytest.raises(RuntimeError, match="RESEND_API_KEY"):
        with TestClient(app):
            pass


def test_public_address_with_resend_key_starts(monkeypatch):
    monkeypatch.setattr(settings, "app_url", "https://mock.example.com")
    monkeypatch.setattr(settings, "resend_api_key", "re_test_not_real")

    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200


def test_saved_keys_without_master_key_refuse_to_start(db, org, monkeypatch):
    db.add(ApiKey(org_id=org.id, provider=ANTHROPIC, ciphertext=secrets.encrypt("sk-test")))
    db.commit()
    monkeypatch.setattr(settings, "api_keys_encryption_key", "")

    with pytest.raises(RuntimeError, match="API_KEYS_ENCRYPTION_KEY"):
        with TestClient(app):
            pass


def test_no_saved_keys_start_without_master_key(monkeypatch):
    monkeypatch.setattr(settings, "api_keys_encryption_key", "")

    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200


def test_api_docs_only_on_a_local_address(monkeypatch):
    assert main._docs_config() == {}

    monkeypatch.setattr(settings, "app_url", "https://mock.example.com")

    assert main._docs_config() == {"docs_url": None, "redoc_url": None, "openapi_url": None}
