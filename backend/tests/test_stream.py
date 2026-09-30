"""The stream: event order and the headers that keep proxies from buffering it."""

import json

import pytest


@pytest.fixture(autouse=True)
def _fast_deltas(monkeypatch):
    monkeypatch.setattr("app.routers.stream.DELTA_INTERVAL_S", 0)


def _events(response) -> list[tuple[str, dict]]:
    out = []
    for block in response.text.split("\n\n"):
        name, data = None, None
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data = json.loads(line.removeprefix("data:"))
        if name:
            out.append((name, data))
    return out


def test_status_then_forty_deltas_then_done(client):
    response = client.post("/stream")

    events = _events(response)
    assert events[0] == ("status", {"phase": "silence", "seconds": 0})
    deltas = [data for name, data in events if name == "delta"]
    assert [delta["n"] for delta in deltas] == list(range(1, 41))
    assert events[-1][0] == "done"
    assert events[-1][1]["deltas"] == 40


def test_stream_headers_disable_buffering(client):
    response = client.post("/stream")

    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["x-accel-buffering"] == "no"


def test_stream_needs_a_session(anon_client):
    assert anon_client.post("/stream").status_code == 401
