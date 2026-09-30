"""Org isolation: listings show only the org's rows, and another org's id gets the
same 404 as a missing one."""

import uuid

import pytest

from app import storage
from app.models.file import DONE, File


@pytest.fixture
def other_file(db, other_org):
    name = f"{uuid.uuid4()}.pdf"
    storage.root().mkdir(parents=True, exist_ok=True)
    storage.path_of(name).write_bytes(b"%PDF-1.4 other org")
    row = File(
        org_id=other_org.id,
        filename="other.pdf",
        stored_path=name,
        size_bytes=18,
        status=DONE,
    )
    db.add(row)
    db.commit()
    return row


def test_items_listing_is_per_org(client, org, other_org, make_item):
    make_item(org, "Ours")
    make_item(other_org, "Theirs")

    assert [item["title"] for item in client.get("/items").json()] == ["Ours"]


def test_search_is_per_org(client, org, other_org, make_item):
    make_item(org, "Licitația noastră")
    make_item(other_org, "Licitația lor")

    hits = client.get("/items/search", params={"q": "licitație"}).json()

    assert [hit["title"] for hit in hits] == ["Licitația noastră"]


def test_similar_to_another_orgs_item_is_404(client, other_org, make_item):
    theirs = make_item(other_org, "Theirs")

    response = client.get(f"/items/{theirs.id}/similar")

    assert response.status_code == 404
    assert response.json()["detail"] == "Item not found"
    assert client.get(f"/items/{uuid.uuid4()}/similar").json() == response.json()


def test_similar_never_returns_another_orgs_items(client, org, other_org, make_item):
    ours = make_item(org, "Ours")
    make_item(other_org, "Theirs, identical vector")

    assert client.get(f"/items/{ours.id}/similar").json() == []


def test_files_listing_is_per_org(client, other_file):
    assert client.get("/files").json() == []


def test_downloading_another_orgs_file_is_404(client, other_file):
    response = client.get(f"/files/{other_file.id}/download")

    assert response.status_code == 404
    assert response.json()["detail"] == "File not found"
