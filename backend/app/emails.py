"""Email through Resend, with a direct POST and no SDK.

The only email is the test one from `POST /checks/email`. Without
`RESEND_API_KEY` nothing is sent and the would-be email is logged at WARNING. On a
public `APP_URL` the backend refuses to start without the key
(`main._check_emails`), as Delegate does, where emails carry password links.
"""

import logging
from urllib.parse import urlparse

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_RESEND = "https://api.resend.com/emails"
TIMEOUT = 10.0


class EmailError(Exception):
    """Resend refused the email or could not be reached. The message is short and
    safe to show: it never contains the key."""


def is_local_address() -> bool:
    return urlparse(settings.app_url).hostname in ("localhost", "127.0.0.1", "::1")


def _send_resend(to: str, subject: str, text: str) -> None:
    # Kept thin so tests can replace it.
    with httpx.Client(timeout=TIMEOUT) as client:
        response = client.post(
            _RESEND,
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            json={"from": settings.email_from, "to": [to], "subject": subject, "text": text},
        )
    if response.is_error:
        # Resend explains itself in `message` (for example that the test sender only
        # delivers to the account's own address); that is what the operator needs.
        try:
            reason = response.json().get("message") or response.reason_phrase
        except ValueError:
            reason = response.reason_phrase
        raise EmailError(f"Resend answered {response.status_code}: {reason}")


def send(to: str, subject: str, text: str) -> bool:
    """`True` if Resend accepted it, `False` if it was only logged (no key).
    Raises `EmailError` when Resend fails."""
    if not settings.resend_api_key:
        logger.warning("Email to %s not sent (RESEND_API_KEY missing). Subject: %s", to, subject)
        return False
    try:
        _send_resend(to, subject, text)
    except httpx.HTTPError as exc:
        raise EmailError(f"Could not reach Resend: {type(exc).__name__}") from exc
    return True
