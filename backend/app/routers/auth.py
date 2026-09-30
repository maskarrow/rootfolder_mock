"""Login, logout and "who am I".

Every failed login (unknown email, wrong password, deactivated account) gets the
same 401 in about the same time, so the form does not reveal which emails have
accounts.

Rate limits are counted from the audit log: 10 failures per email or 50 per IP in
15 minutes, after which attempts get 429 without checking the password. Blocked
attempts are not logged, or an attack would fill the table and extend its own
block.

The IP is `request.client.host`. Behind the production proxy uvicorn needs
`--proxy-headers` and `--forwarded-allow-ips`, or every request appears to come
from the proxy and the IP limit blocks everyone at once. `/auth/me` returns that
IP and the scheme precisely so a deploy can check it.
"""

import logging
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.deps import get_auth, get_db
from app.models.org import Org
from app.models.user import User
from app.services import audit, auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

FAILURE_WINDOW = timedelta(minutes=15)
MAX_FAILURES_PER_EMAIL = 10
MAX_FAILURES_PER_IP = 50

_WRONG = "Wrong email or password."
_TOO_MANY = "Too many failed attempts. Try again in 15 minutes."


class LoginIn(BaseModel):
    email: str
    password: str
    remember: bool = False


class MeOut(BaseModel):
    id: uuid.UUID
    email: str
    name: str
    role: str
    org_id: uuid.UUID
    org_name: str
    # What the backend sees of the caller: through the proxy, the browser's address
    # and `https`; otherwise the proxy's address, or `http`.
    seen_ip: str | None
    scheme: str


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _user_agent(request: Request) -> str | None:
    return (request.headers.get("user-agent") or "")[:500] or None


def _me(db: Session, user: User, request: Request) -> MeOut:
    org = db.get(Org, user.org_id)
    return MeOut(
        id=user.id,
        email=user.email,
        name=user.name,
        role=user.role,
        org_id=user.org_id,
        org_name=org.name if org else "",
        seen_ip=_ip(request),
        scheme=request.url.scheme,
    )


@router.post("/login", response_model=MeOut)
def login(payload: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    email = payload.email.strip().lower()
    ip = _ip(request)

    since = auth.now() - FAILURE_WINDOW
    if audit.recent_failures(db, since=since, email=email) >= MAX_FAILURES_PER_EMAIL or (
        ip is not None and audit.recent_failures(db, since=since, ip=ip) >= MAX_FAILURES_PER_IP
    ):
        logger.warning("Login blocked by the rate limit: email=%s ip=%s", email, ip)
        raise HTTPException(status_code=429, detail=_TOO_MANY)

    user = db.scalars(select(User).where(func.lower(User.email) == email)).first()
    password_ok = auth.verify_password(user.password_hash if user else None, payload.password)
    if user is None or not password_ok or not user.is_active:
        audit.write(
            db,
            audit.LOGIN_FAILED,
            user=user,
            email=email,
            ip=ip,
            user_agent=_user_agent(request),
        )
        raise HTTPException(status_code=401, detail=_WRONG)

    token = auth.open_session(db, user)
    audit.write(db, audit.LOGIN, user=user, ip=ip, user_agent=_user_agent(request))
    response.set_cookie(
        auth.COOKIE,
        token,
        # No `max_age` = a session cookie, forgotten when the browser closes.
        max_age=settings.session_max_days * 24 * 3600 if payload.remember else None,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )
    return _me(db, user, request)


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    """Answers 204 even without a valid session: the user wanted out, and is out."""
    token = request.cookies.get(auth.COOKIE)
    ctx = auth.read_session(db, token)
    if ctx is not None:
        auth.close_session(db, token)
        audit.write(
            db, audit.LOGOUT, user=ctx.user, ip=_ip(request), user_agent=_user_agent(request)
        )
    response.delete_cookie(
        auth.COOKIE,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )


@router.get("/me", response_model=MeOut)
def me(request: Request, ctx: auth.AuthContext = Depends(get_auth), db: Session = Depends(get_db)):
    return _me(db, ctx.user, request)
