"""Sets a new password for an existing account and logs it out everywhere: a new
password is what you set when someone else may know the old one.

The password is typed twice in the terminal and not echoed.

On the server, from /opt/rootfolder:
    docker compose run --rm api python set_password.py someone@company.com
locally, from the repo root:
    just set-password someone@company.com
"""

import argparse
import sys

from sqlalchemy import delete, func, select

from app.db import SessionLocal
from app.models.user import User
from app.models.user_session import UserSession
from app.services import audit, auth
from create_user import ask_password


def main() -> int:
    parser = argparse.ArgumentParser(description="Set an account's password")
    parser.add_argument("email")
    args = parser.parse_args()
    email = args.email.strip().lower()

    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(func.lower(User.email) == email))
        if user is None:
            print(f"No account with the email {email}.", file=sys.stderr)
            return 1

        password = ask_password()
        if password is None:
            return 1

        user.password_hash = auth.hash_password(password)
        closed = db.execute(delete(UserSession).where(UserSession.user_id == user.id)).rowcount
        db.commit()
        audit.write(db, audit.PASSWORD_SET, user=user)
        print(f"Password changed for {email}; {closed} open sessions closed.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
