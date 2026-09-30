from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.user import ADMIN
from app.services import auth


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_auth(request: Request, db: Session = Depends(get_db)) -> auth.AuthContext:
    """The logged-in user from the session cookie, or 401.

    `main.py` attaches it to every router except `/auth`.
    """
    found = auth.read_session(db, request.cookies.get(auth.COOKIE))
    if found is None:
        raise HTTPException(status_code=401, detail="You are not logged in.")
    return found


def require_admin(ctx: auth.AuthContext = Depends(get_auth)) -> auth.AuthContext:
    if ctx.user.role != ADMIN:
        raise HTTPException(status_code=403, detail="Only the organization admin can do this.")
    return ctx
