"""Health: `live` never touches the database, `ready` reports it."""

from sqlalchemy.exc import OperationalError

from app.config import settings


class _DeadEngine:
    def connect(self):
        raise OperationalError("select 1", {}, Exception("connection refused"))


def test_live_answers_with_version_and_env(anon_client):
    response = anon_client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": settings.app_version,
        "env": settings.app_env,
    }


def test_ready_checks_the_database(anon_client):
    response = anon_client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["db"] == "ok"
    assert response.json()["version"] == settings.app_version


def test_database_down_fails_ready_but_not_live(anon_client, monkeypatch):
    monkeypatch.setattr("app.main.engine", _DeadEngine())

    ready = anon_client.get("/health/ready")
    live = anon_client.get("/health/live")

    assert ready.status_code == 503
    assert ready.json()["db"] == "down"
    assert live.status_code == 200
