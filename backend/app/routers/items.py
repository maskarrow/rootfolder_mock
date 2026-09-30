"""Items: the newest, full-text search and nearest neighbours.

Each route proves one thing about the production database: that the migrations
ran (`pinned` comes from the second one), that the `romanian` text search
configuration exists, and that pgvector answers cosine distance queries.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, literal_column, select
from sqlalchemy.orm import Session

from app.deps import get_auth, get_db
from app.models.item import Item
from app.services.auth import AuthContext
from app.tenancy import in_org

router = APIRouter(prefix="/items", tags=["items"])

# Must be the configuration the generated `search` column uses (migration 0001), or
# stems would not match. Cast explicitly: a bare string could resolve to another
# overload of the function.
_ROMANIAN = literal_column("'romanian'::regconfig")

# Everything but the embedding, which is of no use to the page.
_COLUMNS = (Item.id, Item.title, Item.body, Item.pinned, Item.created_at)


class ItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    body: str
    pinned: bool
    created_at: datetime


class SearchHit(ItemOut):
    rank: float


class SimilarItem(ItemOut):
    # Cosine distance: 0 is the same direction, 2 the opposite.
    distance: float


@router.get("", response_model=list[ItemOut])
def newest(ctx: AuthContext = Depends(get_auth), db: Session = Depends(get_db)):
    """The org's 20 newest items, pinned ones first."""
    return db.execute(
        select(*_COLUMNS)
        .where(Item.org_id == ctx.user.org_id)
        .order_by(Item.pinned.desc(), Item.created_at.desc(), Item.title)
        .limit(20)
    ).all()


@router.get("/search", response_model=list[SearchHit])
def search(
    q: str = Query(min_length=1, max_length=200),
    ctx: AuthContext = Depends(get_auth),
    db: Session = Depends(get_db),
):
    """Web-style query syntax (`"exact phrase"`, `or`, `-word`); words are stemmed
    in Romanian, so `licitație` also finds `licitațiile`."""
    query = func.websearch_to_tsquery(_ROMANIAN, q)
    rank = func.ts_rank(Item.search, query).label("rank")
    return db.execute(
        select(*_COLUMNS, rank)
        .where(Item.org_id == ctx.user.org_id, Item.search.bool_op("@@")(query))
        .order_by(rank.desc(), Item.title)
        .limit(20)
    ).all()


@router.get("/{item_id}/similar", response_model=list[SimilarItem])
def similar(
    item_id: uuid.UUID, ctx: AuthContext = Depends(get_auth), db: Session = Depends(get_db)
):
    """The 5 nearest items of the same org by cosine distance, the item itself
    excluded."""
    item = in_org(db, Item, item_id, ctx.user.org_id)
    distance = Item.embedding.cosine_distance(item.embedding).label("distance")
    return db.execute(
        select(*_COLUMNS, distance)
        .where(Item.org_id == ctx.user.org_id, Item.id != item.id)
        .order_by(distance)
        .limit(5)
    ).all()
