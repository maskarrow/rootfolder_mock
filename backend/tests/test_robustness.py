"""Hostile and malformed input: nothing a client sends may produce a 500, leak
another org's data, or store unbounded data.

The audit checklist behind these tests:
- authority comes from the session row, never from the request body;
- every id in a path is checked against the caller's org (ownership);
- every text field has a type and a maximum length (resource exhaustion);
- input the database cannot store (a NUL byte) is a 422, not a crash;
- query text is data, never SQL.

Every endpoint is also called by every kind of caller (`test_access_matrix`).
"""

import uuid

import pytest
from sqlalchemy import func, select

from app.models.audit import AuditEntry
from app.models.file import File
from app.models.user import MEMBER, User
from conftest import PASSWORD

NUL = "\x00"
INJECTION_LIKE = [
    "' OR '1'='1",
    "'; DROP TABLE users; --",
    '" OR ""="',
    "licitatie' UNION SELECT password_hash FROM users --",
    "%' --",
    "\\x27",
    "a & | ! ( ) :* <-> b",  # tsquery operators
    "))))))((((((",
    "😀 ᴜɴɪᴄᴏᴅᴇ ‮ RTL",
]


# --- No 500, whatever the body ------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"{",
        b"[]",
        b"null",
        b'{"email": 1, "password": 2}',
        b'{"email": null, "password": null}',
        b'{"email": ["a"], "password": {"b": 1}}',
        b'{"email": "a@b.test"}',
        b'{"email": "a@b.test", "password": "x", "remember": "maybe"}',
        b'{"email": "a@b.test", "password": "x", "remember": 2}',
    ],
)
def test_malformed_login_bodies_are_422(anon_client, body):
    response = anon_client.post(
        "/auth/login", content=body, headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 422


def test_login_with_a_nul_byte_is_refused_not_a_crash(anon_client, db):
    response = anon_client.post(
        "/auth/login", json={"email": f"a{NUL}@b.test", "password": "whatever-password"}
    )

    assert response.status_code in (401, 422)


def test_oversized_login_fields_are_refused_and_not_stored(anon_client, db):
    """A failed login is written to the audit log with its email: without a cap,
    every attempt could store megabytes."""
    huge = "a" * 100_000 + "@b.test"

    response = anon_client.post("/auth/login", json={"email": huge, "password": "x" * 100_000})

    assert response.status_code == 422
    longest = db.scalar(select(func.max(func.length(AuditEntry.email))))
    assert longest is None or longest < 1000


@pytest.mark.parametrize("query", INJECTION_LIKE)
def test_search_treats_any_text_as_text(client, org, other_org, make_item, query):
    make_item(org, "Licitația noastră")
    make_item(other_org, "Licitația lor")

    response = client.get("/items/search", params={"q": query})

    assert response.status_code == 200
    assert all(hit["title"] != "Licitația lor" for hit in response.json())


def test_search_with_a_nul_byte_is_refused_not_a_crash(client):
    response = client.get("/items/search", params={"q": f"licita{NUL}tie"})

    assert response.status_code == 422


@pytest.mark.parametrize("query", ["", "x" * 201])
def test_search_length_is_bounded(client, query):
    assert client.get("/items/search", params={"q": query}).status_code == 422


@pytest.mark.parametrize(
    "bad_id", ["not-a-uuid", "1", "' OR 1=1 --", "00000000-0000-0000-0000", "%00", "..%2F..%2F"]
)
def test_malformed_ids_in_paths_are_rejected(client, bad_id):
    for path in (f"/items/{bad_id}/similar", f"/files/{bad_id}/download"):
        response = client.get(path)
        assert response.status_code in (404, 422), (path, response.status_code)


def test_wrong_methods_are_405(client):
    assert client.delete("/items").status_code == 405
    assert client.put("/files").status_code == 405
    assert client.get("/stream").status_code == 405
    assert client.get("/auth/login").status_code == 405


# --- Uploads --------------------------------------------------------------------


def _upload(client, name, content=b"%PDF-1.4 x", mime="application/pdf"):
    return client.post("/files", files={"file": (name, content, mime)})


@pytest.mark.parametrize(
    "name",
    [
        "../../../etc/passwd.pdf",
        "..\\..\\windows\\win.ini.pdf",
        "name\r\nSet-Cookie: x=y.pdf",
        '"; filename="evil.html.pdf',
        "a" * 1000 + ".pdf",
        ".pdf",
    ],
)
def test_hostile_file_names_are_stored_safely(client, db, name):
    response = _upload(client, name)

    assert response.status_code in (201, 415, 422)
    if response.status_code != 201:
        return
    row = db.get(File, response.json()["id"])
    # On disk the name is always ours: a UUID, never the client's text.
    assert (
        "/" not in row.stored_path and "\\" not in row.stored_path and ".." not in row.stored_path
    )
    assert len(row.filename) <= 255
    download = client.get(f"/files/{row.id}/download")
    assert download.status_code == 200
    disposition = download.headers["content-disposition"]
    assert "\r" not in disposition and "\n" not in disposition


@pytest.mark.parametrize("name", [f"evil{NUL}.pdf", "evil\r\n.pdf", "evil\x7f.pdf"])
def test_control_characters_in_upload_names_are_refused_and_leave_nothing(client, db, name):
    """Built by hand: a client library escapes these, a hostile client does not."""
    from app import storage

    before = set(storage.root().iterdir()) if storage.root().exists() else set()
    boundary = "rootfolderboundary"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
        "%PDF-1.4 x\r\n"
        f"--{boundary}--\r\n"
    ).encode()

    response = client.post(
        "/files",
        content=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )

    assert response.status_code in (400, 415, 422)
    assert db.scalar(select(func.count()).select_from(File)) == 0
    after = set(storage.root().iterdir()) if storage.root().exists() else set()
    assert after == before


@pytest.mark.parametrize("name", ["virus.exe", "page.html", "x.pdf.exe", "noextension", "x.PDF.js"])
def test_only_pdf_names_are_accepted(client, name):
    assert _upload(client, name).status_code == 415


def test_upload_without_a_file_is_422(client):
    assert client.post("/files", data={"x": "y"}).status_code == 422


def test_downloads_are_never_rendered_by_the_browser(client):
    file_id = _upload(client, "x.pdf", content=b"<html><script>alert(1)</script></html>").json()[
        "id"
    ]

    response = client.get(f"/files/{file_id}/download")

    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith("attachment")
    assert response.headers["x-content-type-options"] == "nosniff"


# --- Sessions -------------------------------------------------------------------


@pytest.mark.parametrize(
    "cookie", ["", "x", "a" * 5000, f"abc{NUL}def", "' OR 1=1 --", "%00", "😀"]
)
def test_forged_session_cookies_are_401(anon_client, cookie):
    # Raw bytes: a client library refuses to send some of these, an attacker does not.
    raw = b"mock_session=" + cookie.encode("utf-8")

    response = anon_client.get("/auth/me", headers=[(b"cookie", raw)])

    assert response.status_code == 401


def test_the_request_body_cannot_choose_the_role_or_org(anon_client, admin, other_org, db):
    """Authority source: login ignores any role or org the client sends."""
    response = anon_client.post(
        "/auth/login",
        json={
            "email": admin.email,
            "password": PASSWORD,
            "role": "superadmin",
            "org_id": str(other_org.id),
            "is_active": False,
        },
    )

    assert response.status_code == 200
    assert response.json()["role"] == "admin"
    assert response.json()["org_id"] == str(admin.org_id)


# --- Who may call what ----------------------------------------------------------

ADMIN_ONLY = {("POST", "/checks/provider"), ("POST", "/checks/email")}


@pytest.fixture
def callers(anon_client, client_for, make_user, org, other_org, admin):
    member = make_user(org, role=MEMBER)
    stranger = make_user(other_org)
    return {
        "anonymous": anon_client,
        "member": client_for(member),
        "admin": client_for(admin),
        "other org admin": client_for(stranger),
    }


def test_access_matrix(callers, org, make_item, client_for, admin):
    item = make_item(org, "Ours")
    file_id = _upload(callers["admin"], "ours.pdf").json()["id"]

    endpoints = [
        ("GET", "/auth/me"),
        ("GET", "/items"),
        ("GET", "/items/search?q=x"),
        ("GET", f"/items/{item.id}/similar"),
        ("GET", "/files"),
        ("GET", f"/files/{file_id}/download"),
        ("POST", "/checks/provider"),
        ("POST", "/checks/email"),
    ]
    for who, caller in callers.items():
        for method, path in endpoints:
            status = caller.request(method, path).status_code
            if who == "anonymous":
                expected = {401}
            elif (method, path) in ADMIN_ONLY and who == "member":
                expected = {403}
            elif (
                who == "other org admin"
                and str(item.id) in path
                or (who == "other org admin" and file_id in path)
            ):
                expected = {404}
            else:
                expected = {200}
            assert status in expected, f"{who} {method} {path}: {status}, expected {expected}"


def test_another_orgs_listing_is_empty_not_shared(callers, org, make_item):
    make_item(org, "Ours")
    _upload(callers["admin"], "ours.pdf")

    stranger = callers["other org admin"]
    assert stranger.get("/items").json() == []
    assert stranger.get("/files").json() == []


def test_deactivated_member_is_locked_out_at_once(client_for, make_user, org, db):
    member = make_user(org, role=MEMBER)
    session = client_for(member)
    assert session.get("/auth/me").status_code == 200

    db.get(User, member.id).is_active = False
    db.commit()

    assert session.get("/auth/me").status_code == 401


def test_unknown_ids_reveal_nothing(client):
    random_id = uuid.uuid4()

    response = client.get(f"/items/{random_id}/similar")

    assert response.status_code == 404
    assert str(random_id) not in response.text


def test_a_number_json_cannot_express_is_a_422_not_a_500(anon_client):
    response = anon_client.post(
        "/auth/login",
        content=b'{"email": "a@b.c", "password": "x", "remember": 1e999}',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422


def test_validation_errors_never_echo_what_was_sent(anon_client):
    secret = "my-real-password-" + "x" * 1100

    response = anon_client.post("/auth/login", json={"email": "a@b.c", "password": secret})

    assert response.status_code == 422
    assert "my-real-password" not in response.text
    assert response.json()["detail"][0]["loc"] == ["body", "password"]
