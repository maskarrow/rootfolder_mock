"""Creates an account, and its org if the org does not exist yet.

The password is typed twice in the terminal and not echoed; never passed as an
argument, where it would stay in the shell history. In a deploy this runs inside
the api container, from backend/.

From the repo root:
    just create-user someone@company.com "First Last" "Delegate" --admin
or from backend/:
    uv run python create_user.py someone@company.com "First Last" --org Delegate --admin
"""

import argparse
import getpass
import sys

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models.org import Org
from app.models.user import ADMIN, MEMBER, User
from app.services import audit, auth


def ask_password() -> str | None:
    """A new password typed twice, or `None` (the reason goes to stderr)."""
    password = getpass.getpass("Password: ")
    problem = auth.password_problem(password)
    if problem is not None:
        print(problem, file=sys.stderr)
        return None
    if getpass.getpass("Password, again: ") != password:
        print("The passwords do not match.", file=sys.stderr)
        return None
    return password


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a user")
    parser.add_argument("email")
    parser.add_argument("name")
    parser.add_argument("--org", required=True, help="org name; created if missing")
    parser.add_argument("--admin", action="store_true", help="org admin instead of member")
    args = parser.parse_args()

    email = args.email.strip().lower()
    name = args.name.strip()
    org_name = args.org.strip()
    if "@" not in email or not name or not org_name:
        print("The email must contain @, and the name and org cannot be empty.", file=sys.stderr)
        return 2

    db = SessionLocal()
    try:
        if db.scalar(select(User).where(func.lower(User.email) == email)) is not None:
            print(f"An account with the email {email} already exists.", file=sys.stderr)
            return 1

        password = ask_password()
        if password is None:
            return 1

        org = db.scalar(select(Org).where(Org.name == org_name))
        if org is None:
            org = Org(name=org_name)
            db.add(org)
            db.flush()
            print(f"Org created: {org_name}")

        role = ADMIN if args.admin else MEMBER
        user = User(
            org_id=org.id,
            email=email,
            name=name,
            role=role,
            password_hash=auth.hash_password(password),
        )
        db.add(user)
        db.commit()
        audit.write(db, audit.ACCOUNT_CREATED, user=user)
        print(f"User created: {name} <{email}>, {role} of {org_name}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
