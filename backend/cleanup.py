"""The periodic cleanup, run by hand: the same pass the backend makes every
`CLEANUP_INTERVAL_HOURS`, with the report printed.

From the repo root:
    just cleanup
"""

import sys

from app.db import SessionLocal
from app.services import cleanup


def main() -> int:
    db = SessionLocal()
    try:
        report = cleanup.run(db)
    finally:
        db.close()
    print(f"Sessions deleted: {report['sessions']}")
    print(f"Audit entries deleted: {report['audit']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
