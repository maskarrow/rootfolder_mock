"""`CheckOrigin`: writes from another origin get 403; reads, non-browser clients and
CORS preflights pass."""

ALLOWED = "http://localhost:3010"
EVIL = "https://evil.example"


def test_a_post_from_another_origin_is_refused(anon_client):
    response = anon_client.post(
        "/auth/login", json={"email": "a@b.test", "password": "x"}, headers={"Origin": EVIL}
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Cross-site request refused."
    # Refused before the app: the security headers still apply.
    assert response.headers["x-frame-options"] == "DENY"


def test_a_post_from_the_ui_origin_reaches_the_app(anon_client):
    response = anon_client.post(
        "/auth/login", json={"email": "a@b.test", "password": "x"}, headers={"Origin": ALLOWED}
    )

    assert response.status_code == 401


def test_a_post_without_origin_reaches_the_app(anon_client):
    assert anon_client.post("/auth/logout").status_code == 204


def test_reads_are_not_checked(anon_client):
    assert anon_client.get("/health/live", headers={"Origin": EVIL}).status_code == 200


def test_the_preflight_is_answered_by_cors(anon_client):
    response = anon_client.options(
        "/auth/login",
        headers={"Origin": ALLOWED, "Access-Control-Request-Method": "POST"},
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED
    assert response.headers["access-control-allow-credentials"] == "true"
