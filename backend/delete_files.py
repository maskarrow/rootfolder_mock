"""Deletes every file one account uploaded: the rows and the PDFs on disk. Made for
the load test's throwaway account (loadtest/README.md); run it before deleting the
account, since a deleted account's files stay with its org without an owner.

Shows how many files and how much space, then asks to type the email again. The
rows go first and the PDFs after, so a failure halfway leaves at worst an unused
PDF on disk, never a row whose PDF is missing. The audit log gets one entry.

On the server, from /opt/rootfolder:
    docker compose run --rm api python delete_files.py --uploaded-by loadtest@rootfolder.com
locally, from the repo root:
    just delete-files loadtest@rootfolder.com
"""

import argparse
import sys

from sqlalchemy import delete, func, select

from app import storage
from app.db import SessionLocal
from app.models.file import File
from app.models.user import User
from app.services import audit


def main() -> int:
    parser = argparse.ArgumentParser(description="Delete every file an account uploaded")
    parser.add_argument("--uploaded-by", required=True, metavar="EMAIL")
    args = parser.parse_args()
    email = args.uploaded_by.strip().lower()

    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(func.lower(User.email) == email))
        if user is None:
            print(f"No account with the email {email}.", file=sys.stderr)
            return 1

        files = db.scalars(select(File).where(File.uploaded_by == user.id)).all()
        if not files:
            print(f"{email} has no files.")
            return 0
        total_mb = sum(f.size_bytes for f in files) / (1024 * 1024)
        print(f"{email} uploaded {len(files)} files, {total_mb:.1f} MB.")

        if input(f"Type {email} again to delete them: ").strip().lower() != email:
            print("Not confirmed; nothing deleted.", file=sys.stderr)
            return 1

        paths = [storage.path_of(f.stored_path) for f in files]
        db.execute(delete(File).where(File.id.in_([f.id for f in files])))
        db.commit()
        for path in paths:
            path.unlink(missing_ok=True)
        audit.write(db, audit.FILES_DELETED, user=user)
        print(f"Deleted {len(files)} files of {email}.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
