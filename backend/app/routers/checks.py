"""Checks an org admin runs from the dashboard against the outside world.

Both answer 200 with `{ok, detail}`: a failed check is a result to show, not an
error of the request.

- **Provider:** decrypts the org's Anthropic key and lists the models
  (`GET /v1/models`), which bills no tokens. Proves outbound HTTPS from the
  server, the master key, and the encrypted row. The key never appears in the
  answer or the logs, even inside an error message.
- **Email:** sends a test email to the current user through Resend. The Resend
  test sender only delivers to the Resend account's own address.
"""

from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import emails, secrets
from app.config import settings
from app.deps import get_db, require_admin
from app.models.api_key import ANTHROPIC, ApiKey
from app.services.auth import AuthContext

router = APIRouter(prefix="/checks", tags=["checks"])

_ANTHROPIC_MODELS = "https://api.anthropic.com/v1/models"
TIMEOUT = 10.0


class CheckOut(BaseModel):
    ok: bool
    detail: str


def _list_models(key: str) -> httpx.Response:
    # Kept thin so tests can replace it.
    with httpx.Client(timeout=TIMEOUT) as client:
        return client.get(
            _ANTHROPIC_MODELS, headers={"x-api-key": key, "anthropic-version": "2023-06-01"}
        )


def _refusal(response: httpx.Response) -> str:
    """Anthropic's own reason (`error.message`), or the status text."""
    try:
        error = response.json().get("error") or {}
        return error.get("message") or response.reason_phrase
    except ValueError, AttributeError:
        return response.reason_phrase


def _ask_anthropic(key: str) -> CheckOut:
    try:
        response = _list_models(key)
    except httpx.HTTPError as exc:
        return CheckOut(ok=False, detail=f"Could not reach api.anthropic.com: {exc!r}")
    if response.status_code != 200:
        return CheckOut(
            ok=False,
            detail=f"api.anthropic.com answered {response.status_code}: {_refusal(response)}",
        )
    count = len(response.json().get("data", []))
    return CheckOut(ok=True, detail=f"The key works: api.anthropic.com listed {count} models.")


@router.post("/provider", response_model=CheckOut)
def check_provider(ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    row = db.scalar(
        select(ApiKey).where(ApiKey.org_id == ctx.user.org_id, ApiKey.provider == ANTHROPIC)
    )
    if row is None:
        return CheckOut(ok=False, detail="No key saved. Save one with `just set-org-key <org>`.")

    try:
        key = secrets.decrypt(row.ciphertext)
    except RuntimeError as exc:
        result = CheckOut(ok=False, detail=str(exc))
    else:
        result = _ask_anthropic(key)
        # Error texts come from libraries and the provider; none should quote the key,
        # but this is the one place it could leak, so it is scrubbed regardless.
        result.detail = result.detail.replace(key, "***")

    row.last_checked_at = datetime.now(UTC)
    row.last_error = None if result.ok else result.detail
    db.commit()
    return result


@router.post("/email", response_model=CheckOut)
def check_email(ctx: AuthContext = Depends(require_admin)):
    to = ctx.user.email
    text = (
        f"This is a test email from delegate-mock ({settings.app_env}, version "
        f"{settings.app_version}, {settings.app_url}).\n\n"
        "If you are reading it, the Resend key and the sender work.\n"
    )
    try:
        sent = emails.send(to, "delegate-mock: test email", text)
    except emails.EmailError as exc:
        return CheckOut(ok=False, detail=str(exc))
    if not sent:
        return CheckOut(
            ok=False, detail="RESEND_API_KEY is not set: the email was logged instead of sent."
        )
    return CheckOut(ok=True, detail=f"Resend accepted the email to {to}.")
