"""Saves an org's provider key, encrypted with `API_KEYS_ENCRYPTION_KEY`.

The key is typed in the terminal and not echoed; never passed as an argument,
where it would stay in the shell history. Saving again replaces the old key.

From the repo root:
    just set-org-key Delegate
or from backend/:
    uv run python set_org_key.py --org Delegate --provider anthropic
"""

import argparse
import getpass
import sys

from sqlalchemy import select

from app import secrets
from app.db import SessionLocal
from app.models.api_key import ANTHROPIC, ApiKey
from app.models.org import Org


def main() -> int:
    parser = argparse.ArgumentParser(description="Save an org's provider key")
    parser.add_argument("--org", required=True, help="org name")
    parser.add_argument("--provider", required=True, choices=[ANTHROPIC])
    args = parser.parse_args()

    if not secrets.configured():
        print(
            "API_KEYS_ENCRYPTION_KEY is empty: set it in the environment first.",
            file=sys.stderr,
        )
        return 1

    db = SessionLocal()
    try:
        org = db.scalar(select(Org).where(Org.name == args.org.strip()))
        if org is None:
            print(f"No org named {args.org!r}.", file=sys.stderr)
            return 1

        key = getpass.getpass(f"{args.provider} API key: ").strip()
        if not key:
            print("The key is empty; nothing saved.", file=sys.stderr)
            return 1
        try:
            ciphertext = secrets.encrypt(key)
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            return 1

        row = db.scalar(
            select(ApiKey).where(ApiKey.org_id == org.id, ApiKey.provider == args.provider)
        )
        if row is None:
            row = ApiKey(org_id=org.id, provider=args.provider)
            db.add(row)
        row.ciphertext = ciphertext
        row.last_checked_at = None
        row.last_error = None
        db.commit()
        print(f"Key saved for {org.name} ({args.provider}). Test it with 'Check provider key'.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
