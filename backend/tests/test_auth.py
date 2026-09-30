"""Login, rate limits, session expiry, logout, and the session requirement on
every router but `/auth`."""

import uuid
from datetime import timedelta

from sqlalchemy import func, select

from app.main import app
from app.models.audit import AuditEntry
from app.models.user import MEMBER
from app.models.user_session import UserSession
from app.services import audit, auth
from conftest import PASSWORD


def _login(client, email, password=PASSWORD, remember=False):
    return client.post(
        "/auth/login", json={"email": email, "password": password, "remember": remember}
    )


def _failures(db, count, *, email=None, ip=None, minutes_ago=1):
    """Failed logins written straight to the audit log, as the limits count them."""
    at = auth.now() - timedelta(minutes=minutes_ago)
    for n in range(count):
        db.add(
            AuditEntry(
                event=audit.LOGIN_FAILED,
                email=email or f"someone{n}@x.test",
                ip=ip,
                created_at=at,
            )
        )
    db.commit()


def test_login_sets_a_session_cookie(anon_client, admin, org):
    response = _login(anon_client, admin.email)

    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{auth.COOKIE}=")
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/" in cookie
    # Without "remember me" the browser forgets the cookie when it closes.
    assert "Max-Age" not in cookie
    assert response.json()["org_name"] == org.name


def test_remember_keeps_the_cookie_for_the_session_lifetime(anon_client, admin):
    response = _login(anon_client, admin.email, remember=True)

    assert f"Max-Age={30 * 24 * 3600}" in response.headers["set-cookie"]


def test_me_shows_what_the_backend_sees(anon_client, admin):
    _login(anon_client, admin.email)
    response = anon_client.get("/auth/me")

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == admin.email
    assert body["role"] == "admin"
    assert body["seen_ip"] == "testclient"
    assert body["scheme"] == "http"


def test_every_failure_gets_the_same_401(anon_client, make_user, org, admin, db):
    inactive = make_user(org, active=False)
    answers = [
        _login(anon_client, admin.email, password="wrong-password-123"),
        _login(anon_client, "nobody@x.test"),
        _login(anon_client, inactive.email),
    ]

    assert [r.status_code for r in answers] == [401, 401, 401]
    assert len({r.json()["detail"] for r in answers}) == 1
    failed = db.scalar(select(func.count()).where(AuditEntry.event == audit.LOGIN_FAILED))
    assert failed == 3


def test_ten_failures_per_email_block_even_the_right_password(anon_client, admin, db):
    _failures(db, 10, email=admin.email)

    response = _login(anon_client, admin.email)

    assert response.status_code == 429
    # Blocked attempts are not logged, or an attack would extend its own block.
    assert db.scalar(select(func.count()).select_from(AuditEntry)) == 10


def test_fifty_failures_per_ip_block_everyone_from_it(anon_client, admin, db):
    _failures(db, 50, ip="testclient")

    assert _login(anon_client, admin.email).status_code == 429


def test_old_failures_do_not_count(anon_client, admin, db):
    _failures(db, 10, email=admin.email, minutes_ago=16)

    assert _login(anon_client, admin.email).status_code == 200


def test_the_eleventh_failed_login_is_refused(anon_client, admin):
    codes = [
        _login(anon_client, admin.email, password="wrong-password-123").status_code
        for _ in range(11)
    ]

    assert codes == [401] * 10 + [429]


def test_idle_session_expires(client, admin, db):
    session = db.scalar(select(UserSession).where(UserSession.user_id == admin.id))
    session.last_seen_at = auth.now() - timedelta(hours=8, minutes=1)
    db.commit()

    assert client.get("/auth/me").status_code == 401
    assert db.scalar(select(func.count()).select_from(UserSession)) == 0


def test_session_expires_at_its_absolute_limit_despite_activity(client, admin, db):
    session = db.scalar(select(UserSession).where(UserSession.user_id == admin.id))
    session.last_seen_at = auth.now()
    session.expires_at = auth.now() - timedelta(seconds=1)
    db.commit()

    assert client.get("/auth/me").status_code == 401


def test_deactivated_user_loses_the_session(client, admin, db):
    admin.is_active = False
    db.commit()

    assert client.get("/auth/me").status_code == 401


def test_logout_closes_the_session_and_deletes_the_cookie(client, db):
    response = client.post("/auth/logout")

    assert response.status_code == 204
    assert f'{auth.COOKIE}=""' in response.headers["set-cookie"]
    assert db.scalar(select(func.count()).select_from(UserSession)) == 0
    assert client.get("/auth/me").status_code == 401


def test_logout_without_a_session_still_answers_204(anon_client):
    assert anon_client.post("/auth/logout").status_code == 204


def test_member_cannot_run_admin_checks(client_for, make_user, org):
    member = client_for(make_user(org, role=MEMBER))

    assert member.post("/checks/provider").status_code == 403
    assert member.post("/checks/email").status_code == 403


def test_every_route_outside_auth_and_health_needs_a_session(anon_client):
    """Walks the route map, so a new router added without the session dependency
    fails here."""
    checked = 0
    for path, operations in app.openapi()["paths"].items():
        if path.startswith(("/auth/", "/health/")):
            continue
        concrete = path.replace("{item_id}", str(uuid.uuid4())).replace(
            "{file_id}", str(uuid.uuid4())
        )
        for method in operations:
            response = anon_client.request(method.upper(), concrete)
            assert response.status_code == 401, f"{method.upper()} {path}"
            checked += 1
    assert checked >= 9
