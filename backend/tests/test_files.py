"""Upload with a cap, relative stored paths, download, and the job."""

import re

from sqlalchemy import func, select

from app import storage
from app.config import settings
from app.models.file import File

PDF = b"%PDF-1.4\n" + b"0" * 2048 + b"\n%%EOF\n"


def _upload(client, content=PDF, name="Caiet de sarcini ă.pdf", mime="application/pdf"):
    return client.post("/files", files={"file": (name, content, mime)})


def _stored_files():
    root = storage.root()
    return sorted(path.name for path in root.iterdir()) if root.exists() else []


def test_upload_stores_a_relative_name_and_runs_the_job(client, db):
    response = _upload(client)

    assert response.status_code == 201
    body = response.json()
    assert body["filename"] == "Caiet de sarcini ă.pdf"
    assert body["size_bytes"] == len(PDF)

    row = db.get(File, body["id"])
    # Only `<uuid>.pdf`: a full path would break when STORAGE_DIR moves.
    assert re.fullmatch(r"[0-9a-f-]{36}\.pdf", row.stored_path)
    assert storage.path_of(row.stored_path).read_bytes() == PDF
    # JOB_SECONDS=0 in tests, and the test client runs background tasks before
    # returning.
    assert row.status == "done"
    assert row.finished_at is not None


def test_download_returns_the_same_bytes(client):
    file_id = _upload(client).json()["id"]

    response = client.get(f"/files/{file_id}/download")

    assert response.status_code == 200
    assert response.content == PDF
    assert response.headers["content-type"] == "application/pdf"
    assert "attachment" in response.headers["content-disposition"]


def test_listing_shows_status(client):
    _upload(client)

    [listed] = client.get("/files").json()

    assert listed["status"] == "done"


def test_over_the_cap_is_refused_and_nothing_is_kept(client, db, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_mb", 1)
    before = _stored_files()

    response = _upload(client, content=b"%PDF-1.4\n" + b"0" * (1024 * 1024 + 1))

    assert response.status_code == 413
    assert response.json()["detail"] == "The file exceeds the 1 MB limit."
    assert _stored_files() == before
    assert db.scalar(select(func.count()).select_from(File)) == 0


def test_exactly_at_the_cap_passes(client, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_mb", 1)

    assert _upload(client, content=b"0" * (1024 * 1024)).status_code == 201


def test_only_pdf_is_accepted(client):
    response = _upload(client, content=b"hello", name="notes.txt", mime="text/plain")

    assert response.status_code == 415


def test_a_row_without_its_file_says_so(client, db):
    file_id = _upload(client).json()["id"]
    storage.path_of(db.get(File, file_id).stored_path).unlink()

    response = client.get(f"/files/{file_id}/download")

    assert response.status_code == 404
    assert "missing from STORAGE_DIR" in response.json()["detail"]
