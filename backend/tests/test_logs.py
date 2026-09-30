"""JSON logs: one line per request, with a request ID shared by every line the
request writes, and nothing secret in them.

The lines are captured with the app's own formatter on a handler added for the
test; only the destination differs from production (a buffer, not stdout).
"""

import io
import json
import logging

import pytest

from app.logs import JsonFormatter
from conftest import PASSWORD


@pytest.fixture
def log_lines():
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield lambda: [json.loads(line) for line in buffer.getvalue().splitlines()]
    finally:
        root.removeHandler(handler)


def _request_lines(lines, path):
    return [line for line in lines if line["logger"] == "app.request" and line["path"] == path]


def test_one_json_line_per_request(anon_client, log_lines):
    response = anon_client.get("/health/live")

    [line] = _request_lines(log_lines(), "/health/live")
    assert line["method"] == "GET"
    assert line["status"] == 200
    assert line["level"] == "INFO"
    assert line["client_ip"] == "testclient"
    assert isinstance(line["duration_ms"], float)
    assert {"ts", "msg", "version", "env"} <= line.keys()
    assert line["request_id"] == response.headers["x-request-id"]


def test_a_proxy_request_id_is_reused(anon_client, log_lines):
    response = anon_client.get("/health/live", headers={"X-Request-ID": "nginx-abc.123"})

    assert response.headers["x-request-id"] == "nginx-abc.123"
    [line] = _request_lines(log_lines(), "/health/live")
    assert line["request_id"] == "nginx-abc.123"


def test_a_malformed_request_id_is_replaced(anon_client):
    response = anon_client.get("/health/live", headers={"X-Request-ID": "bad id\twith spaces"})

    assert response.headers["x-request-id"] != "bad id\twith spaces"
    assert len(response.headers["x-request-id"]) == 32


def test_lines_inside_a_request_carry_its_id(client, log_lines):
    # Without a Resend key the email is logged at WARNING, inside the request.
    response = client.post("/checks/email")

    lines = log_lines()
    [warning] = [line for line in lines if line["logger"] == "app.emails"]
    [request] = _request_lines(lines, "/checks/email")
    assert warning["request_id"] == request["request_id"] == response.headers["x-request-id"]


def test_refused_requests_are_logged_too(anon_client, log_lines):
    anon_client.post("/auth/logout", headers={"Origin": "https://evil.example"})

    [line] = _request_lines(log_lines(), "/auth/logout")
    assert line["status"] == 403


def test_passwords_never_reach_the_logs(anon_client, admin, log_lines):
    anon_client.post("/auth/login", json={"email": admin.email, "password": PASSWORD})
    anon_client.post("/auth/login", json={"email": admin.email, "password": PASSWORD + "x"})

    assert PASSWORD not in json.dumps(log_lines())
