"""Row lookups scoped to the request's org.

Routes that take an id in the path look it up through `in_org`, never `db.get`.
A row from another org gets the same 404 as a missing id; a 403 would confirm the
id exists. Listings filter on `org_id` in their `WHERE` instead.
"""

import uuid

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.file import File
from app.models.item import Item

_NOT_FOUND = {Item: "Item not found", File: "File not found"}


def in_org[Row: (Item, File)](
    db: Session, model: type[Row], id_: uuid.UUID, org_id: uuid.UUID
) -> Row:
    row = db.get(model, id_)
    if row is None or row.org_id != org_id:
        raise HTTPException(status_code=404, detail=_NOT_FOUND[model])
    return row
