"""Two orgs, "Delegate" and "Demo Client", with 50 items each.

The texts are short Romanian notes about public tenders, with diacritics, so the
`romanian` search configuration has real stems to work on. Embeddings are random
unit vectors from a generator seeded with the org name: the same on every machine,
so "similar to the first item" gives the same answer locally and on a server.

Idempotent: an org that already has items is left alone.

From the repo root:
    just seed
"""

import math
import random
import sys

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models.item import EMBEDDING_DIM, Item
from app.models.org import Org

ORGS = ("Delegate", "Demo Client")

SUBJECTS = (
    ("Reabilitarea drumului județean DJ 105", "Consiliul Județean Brașov"),
    ("Achiziția de echipamente medicale", "Spitalul Județean de Urgență Cluj"),
    ("Servicii de salubrizare", "Primăria Municipiului Iași"),
    ("Construcția unei grădinițe cu program prelungit", "Primăria Comunei Florești"),
    ("Modernizarea iluminatului public", "Primăria Orașului Sinaia"),
    ("Furnizarea de mobilier școlar", "Inspectoratul Școlar Județean Timiș"),
    ("Servicii de proiectare pentru o sală de sport", "Primăria Municipiului Oradea"),
    ("Extinderea rețelei de canalizare", "Compania de Apă Someș"),
    ("Achiziția de autobuze electrice", "Primăria Municipiului Constanța"),
    ("Consolidarea podului peste râul Mureș", "Consiliul Județean Alba"),
)

ASPECTS = (
    (
        "caietul de sarcini",
        "Caietul de sarcini descrie cerințele tehnice minime, termenele de execuție "
        "și documentele pe care ofertantul trebuie să le prezinte.",
    ),
    (
        "criteriul de atribuire",
        "Criteriul de atribuire este cel mai bun raport calitate-preț, cu punctaj "
        "pentru prețul ofertei și pentru experiența echipei propuse.",
    ),
    (
        "garanția de participare",
        "Garanția de participare se constituie prin instrument bancar sau de asigurare "
        "și rămâne valabilă pe toată durata de valabilitate a ofertei.",
    ),
    (
        "termenul de depunere a ofertelor",
        "Ofertele se depun în SEAP până la termenul-limită din anunțul de participare; "
        "ofertele întârziate sunt respinse.",
    ),
    (
        "clarificările autorității contractante",
        "Autoritatea contractantă a răspuns la solicitările de clarificare privind "
        "experiența similară și subcontractarea lucrărilor.",
    ),
)


def unit_vector(rng: random.Random) -> list[float]:
    values = [rng.gauss(0.0, 1.0) for _ in range(EMBEDDING_DIM)]
    norm = math.sqrt(sum(v * v for v in values))
    return [v / norm for v in values]


def items_for(org: Org) -> list[Item]:
    rng = random.Random(f"delegate-mock:{org.name}")
    items = []
    for subject, authority in SUBJECTS:
        for aspect, text in ASPECTS:
            items.append(
                Item(
                    org_id=org.id,
                    title=f"{subject}: {aspect}",
                    body=f"{text} Autoritatea contractantă: {authority}.",
                    embedding=unit_vector(rng),
                )
            )
    # One pinned item per org, so the column added by migration 0002 shows up.
    items[0].pinned = True
    return items


def main() -> int:
    db = SessionLocal()
    try:
        for name in ORGS:
            org = db.scalar(select(Org).where(Org.name == name))
            if org is None:
                org = Org(name=name)
                db.add(org)
                db.flush()
            existing = db.scalar(select(func.count()).where(Item.org_id == org.id))
            if existing:
                print(f"{name}: already has {existing} items, left alone")
                continue
            db.add_all(items_for(org))
            db.commit()
            print(f"{name}: {len(SUBJECTS) * len(ASPECTS)} items added")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
