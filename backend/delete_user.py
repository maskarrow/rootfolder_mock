"""Deletes an account. Its sessions go with it (it is logged out at once); the files
it uploaded stay with its org, and the audit log keeps its entries, with the email,
plus one more saying it was deleted.

Asks to type the email again, and refuses to delete an org's last admin: nobody
could then run the admin checks for that org.

On the server, from /opt/rootfolder:
    docker compose run --rm api python delete_user.py someone@company.com
locally, from the repo root:
    just delete-user someone@company.com
"""

import argparse
import sys

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models.user import ADMIN, User
from app.services import audit


def main() -> int:
    parser = argparse.ArgumentParser(description="Delete an account")
    parser.add_argument("email")
    args = parser.parse_args()
    email = args.email.strip().lower()

    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(func.lower(User.email) == email))
        if user is None:
            print(f"No account with the email {email}.", file=sys.stderr)
            return 1

        if user.role == ADMIN:
            admins = db.scalar(
                select(func.count()).where(
                    User.org_id == user.org_id, User.role == ADMIN, User.is_active
                )
            )
            if admins <= 1:
                print(
                    f"{email} is the last admin of its org; create another admin first.",
                    file=sys.stderr,
                )
                return 1

        if input(f"Type {email} again to delete it: ").strip().lower() != email:
            print("Not confirmed; nothing deleted.", file=sys.stderr)
            return 1

        # Written first, while the user row still exists to take the org from.
        audit.write(db, audit.ACCOUNT_DELETED, user=user)
        db.delete(user)
        db.commit()
        print(f"Deleted {email}.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
