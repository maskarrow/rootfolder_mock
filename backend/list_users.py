"""Lists every account: email, name, role, org, and whether it can log in.

On the server, from /opt/rootfolder:
    docker compose run --rm api python list_users.py
locally, from the repo root:
    just list-users
"""

import sys

from sqlalchemy import select

from app.db import SessionLocal
from app.models.org import Org
from app.models.user import User


def main() -> int:
    db = SessionLocal()
    try:
        rows = db.execute(
            select(User, Org.name).join(Org, Org.id == User.org_id).order_by(Org.name, User.email)
        ).all()
    finally:
        db.close()

    if not rows:
        print("No accounts yet.")
        return 0
    print(f"{'EMAIL':<36} {'NAME':<24} {'ROLE':<7} {'ORG':<16} {'ACTIVE':<7} LAST LOGIN (UTC)")
    for user, org_name in rows:
        active = "yes" if user.is_active else "no"
        last = user.last_login_at.strftime("%Y-%m-%d %H:%M") if user.last_login_at else "never"
        print(f"{user.email:<36} {user.name:<24} {user.role:<7} {org_name:<16} {active:<7} {last}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
