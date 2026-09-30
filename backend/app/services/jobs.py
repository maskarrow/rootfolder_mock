"""The simulated processing job after an upload, and what a restart does to it.

After an upload the file is `processing`; a `BackgroundTasks` job waits
`JOB_SECONDS` and marks it `done`. The job lives in the server process, so a
restart (a deploy) in the middle kills it. At the next startup
`fail_interrupted` marks every file still `processing` as `interrupted`, exactly
like Delegate's OCR. This is the behaviour the deploy plan accepts, made visible.

`fail_interrupted` assumes a single server process: next to another one it would
mark a file that process is still working on.
"""

import asyncio
import logging
import uuid

from anyio import to_thread
from sqlalchemy import func, update

from app.config import settings
from app.db import SessionLocal
from app.models.file import DONE, INTERRUPTED, PROCESSING, File

logger = logging.getLogger(__name__)


async def run(file_id: uuid.UUID) -> None:
    # Awaited, not slept in a thread: a thread held for the whole job would come from
    # the pool that serves the sync routes, and a burst of uploads would starve them.
    await asyncio.sleep(settings.job_seconds)
    await to_thread.run_sync(_mark_done, file_id)


def _mark_done(file_id: uuid.UUID) -> None:
    """Only from `processing`: a row already marked `interrupted` stays so."""
    db = SessionLocal()
    try:
        done = db.execute(
            update(File)
            .where(File.id == file_id, File.status == PROCESSING)
            .values(status=DONE, finished_at=func.now())
        ).rowcount
        db.commit()
    finally:
        db.close()
    if done:
        logger.info("File %s processed", file_id)


def fail_interrupted() -> int:
    """At startup, before the first request: files left `processing` by the previous
    process become `interrupted`. Returns how many."""
    db = SessionLocal()
    try:
        ids = db.scalars(
            update(File)
            .where(File.status == PROCESSING)
            .values(status=INTERRUPTED, finished_at=func.now())
            .returning(File.id)
        ).all()
        db.commit()
    finally:
        db.close()
    if ids:
        logger.warning(
            "%s files left in processing by a restart, marked interrupted: %s",
            len(ids),
            ", ".join(str(file_id) for file_id in ids),
        )
    return len(ids)
