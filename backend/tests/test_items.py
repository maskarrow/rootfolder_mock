"""Items: newest first, Romanian full-text search, similarity order."""

import math


def _unit(*values):
    padded = list(values) + [0.0] * (8 - len(values))
    norm = math.sqrt(sum(v * v for v in padded))
    return [v / norm for v in padded]


def test_newest_puts_pinned_first_and_stops_at_20(client, org, make_item):
    for n in range(22):
        make_item(org, f"Item {n:02d}")
    make_item(org, "Pinned", pinned=True)

    items = client.get("/items").json()

    assert len(items) == 20
    assert items[0]["title"] == "Pinned"
    assert items[0]["pinned"] is True
    assert "embedding" not in items[0]


def test_search_stems_romanian_words(client, org, make_item):
    make_item(org, "Licitațiile publice", "Garanția de participare se depune la termen.")
    make_item(org, "Achiziții directe", "Fără procedură competitivă.")

    # Singular and without the article, while the texts have plural and articles.
    hits = client.get("/items/search", params={"q": "licitație garanții"}).json()

    assert [hit["title"] for hit in hits] == ["Licitațiile publice"]


def test_search_ranks_by_relevance(client, org, make_item):
    make_item(org, "Ofertă", "Un singur cuvânt despre drum.")
    make_item(org, "Drumuri", "Drumul județean și drumurile comunale: reabilitarea drumului.")

    hits = client.get("/items/search", params={"q": "drum"}).json()

    assert [hit["title"] for hit in hits] == ["Drumuri", "Ofertă"]
    assert hits[0]["rank"] > hits[1]["rank"]


def test_search_supports_web_syntax(client, org, make_item):
    make_item(org, "Autobuze electrice", "Achiziția de autobuze.")
    make_item(org, "Iluminat public", "Modernizarea iluminatului și achiziția de becuri.")

    hits = client.get("/items/search", params={"q": "achiziția -iluminat"}).json()

    assert [hit["title"] for hit in hits] == ["Autobuze electrice"]


def test_search_needs_a_query(client):
    assert client.get("/items/search").status_code == 422


def test_similar_orders_by_cosine_distance(client, org, make_item):
    anchor = make_item(org, "Anchor", embedding=_unit(1, 0))
    make_item(org, "Far", embedding=_unit(0, 1))
    make_item(org, "Near", embedding=_unit(1, 0.1))
    make_item(org, "Middle", embedding=_unit(1, 1))

    similar = client.get(f"/items/{anchor.id}/similar").json()

    assert [item["title"] for item in similar] == ["Near", "Middle", "Far"]
    assert similar[0]["distance"] < similar[1]["distance"] < similar[2]["distance"]


def test_similar_returns_at_most_five(client, org, make_item):
    anchor = make_item(org, "Anchor")
    for n in range(7):
        make_item(org, f"Other {n}", embedding=_unit(1, n + 1))

    assert len(client.get(f"/items/{anchor.id}/similar").json()) == 5
