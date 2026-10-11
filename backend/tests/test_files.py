"""Upload with a cap, relative stored paths, download, and the job."""

import asyncio
import re

from sqlalchemy import func, select

from app import storage
from app.config import settings
from app.main import app
from app.models.file import File
from app.services import auth

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


def test_uploads_pause_when_the_storage_is_almost_full(client, db, monkeypatch):
    almost_full = settings.upload_min_free_mb + settings.max_upload_mb - 1
    monkeypatch.setattr(storage, "free_mb", lambda: almost_full)
    before = _stored_files()

    response = _upload(client)

    assert response.status_code == 507
    assert db.scalar(select(func.count()).select_from(File)) == 0
    assert _stored_files() == before


def _post_raw(chunks: list[bytes], headers: list[tuple[bytes, bytes]]) -> tuple[int, int]:
    """POST /files straight through the ASGI app, without the test client, which
    would read the whole body itself. Returns the status and how many of the chunks
    the app read."""
    read = 0
    status = 0

    async def receive():
        nonlocal read
        if read < len(chunks):
            read += 1
            return {
                "type": "http.request",
                "body": chunks[read - 1],
                "more_body": read < len(chunks),
            }
        return {"type": "http.disconnect"}

    async def send(message):
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]

    origin = settings.cors_origins.split(",")[0].strip().encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/files",
        "raw_path": b"/files",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver"), (b"origin", origin), *headers],
        "client": ("203.0.113.5", 40000),
        "server": ("testserver", 80),
    }
    asyncio.run(app(scope, receive, send))
    return status, read


def test_an_anonymous_upload_is_refused_before_its_body_is_read():
    """FastAPI parses a form body before it runs any dependency: with `file:
    UploadFile` in the signature, anyone could make the server store 500 MB per
    request before the session check answered 401."""
    chunks = [
        b'--b\r\nContent-Disposition: form-data; name="file"; filename="x.pdf"\r\n\r\n',
        *[b"0" * 65536] * 64,
        b"\r\n--b--\r\n",
    ]

    status, read = _post_raw(chunks, [(b"content-type", b"multipart/form-data; boundary=b")])

    assert status == 401
    assert read == 0


def test_a_declared_size_above_the_cap_is_refused_before_reading(client):
    session = f"{auth.COOKIE}={client.cookies.get(auth.COOKIE)}".encode()
    too_large = str((settings.max_upload_mb + 20) * 1024 * 1024).encode()

    status, read = _post_raw(
        [b"0" * 65536] * 4,
        [
            (b"cookie", session),
            (b"content-type", b"multipart/form-data; boundary=b"),
            (b"content-length", too_large),
        ],
    )

    assert status == 413
    assert read == 0


def test_one_file_per_upload(client, db):
    part = (
        b'--b\r\nContent-Disposition: form-data; name="file"; filename="x.pdf"\r\n\r\n'
        + PDF
        + b"\r\n"
    )

    response = client.post(
        "/files",
        content=part + part + b"--b--\r\n",
        headers={"Content-Type": "multipart/form-data; boundary=b"},
    )

    assert response.status_code == 400
    assert db.scalar(select(func.count()).select_from(File)) == 0


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
