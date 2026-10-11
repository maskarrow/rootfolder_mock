"""PDF upload, listing and download.

What a deploy must get right here: the proxy's body size limit and streaming
(`MAX_UPLOAD_MB` is 500), the storage volume mount (`STORAGE_DIR`), and relative
paths, so a file uploaded before a move still downloads after it.
"""

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from app import storage
from app.config import settings
from app.deps import get_auth, get_db
from app.models.file import PROCESSING, File
from app.services import jobs
from app.services.auth import AuthContext
from app.tenancy import in_org

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/files", tags=["files"])

_MB = 1024 * 1024
# Room for the multipart headers around the file, in a declared Content-Length.
_FORM_OVERHEAD_BYTES = 10 * _MB
_SEND_AS = "Send the PDF as multipart/form-data, in the field `file`."


class FileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    size_bytes: int
    status: str
    created_at: datetime
    finished_at: datetime | None


@router.post("", response_model=FileOut, status_code=201)
async def upload(
    request: Request,
    background_tasks: BackgroundTasks,
    ctx: AuthContext = Depends(get_auth),
    db: Session = Depends(get_db),
):
    """The PDF arrives as the multipart field `file`.

    The body is read here, after the session check, on purpose: with a `file:
    UploadFile` parameter FastAPI reads and spools the whole body (up to 510 MB)
    before it runs any dependency, so a client without a session could fill the disk
    and only then get its 401.
    """
    if storage.free_mb() < settings.upload_min_free_mb + settings.max_upload_mb:
        logger.error("Upload refused: the storage has %s MB free", storage.free_mb())
        raise HTTPException(
            status_code=507,
            detail="The storage is full; uploads are paused. Tell the administrator.",
        )
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > settings.max_upload_mb * _MB + _FORM_OVERHEAD_BYTES:
        raise HTTPException(
            status_code=413, detail=f"The file exceeds the {settings.max_upload_mb} MB limit."
        )
    # Multipart only: Starlette reads any other form encoding into memory whole.
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise HTTPException(status_code=422, detail=_SEND_AS)

    async with request.form(max_files=1, max_fields=10) as form:
        file = form.get("file")
        if not isinstance(file, UploadFile):
            raise HTTPException(status_code=422, detail=_SEND_AS)
        row = await run_in_threadpool(_store, file, ctx, db)
    background_tasks.add_task(jobs.run, row.id)
    return row


def _store(file: UploadFile, ctx: AuthContext, db: Session) -> File:
    """Copies the upload into the storage and records it. Blocking: runs in a
    thread, off the event loop."""
    try:
        stored_path, size = storage.save_upload(file, max_mb=settings.max_upload_mb)
    except storage.UploadValidationError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except storage.UploadTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc

    row = File(
        org_id=ctx.user.org_id,
        uploaded_by=ctx.user.id,
        filename=file.filename[:255],
        stored_path=stored_path,
        size_bytes=size,
        status=PROCESSING,
    )
    db.add(row)
    try:
        db.commit()
    except Exception:
        # The file is already on disk; without its row nothing would ever find or
        # delete it.
        storage.path_of(stored_path).unlink(missing_ok=True)
        raise
    logger.info("File %s stored: %s bytes", row.id, size)
    return row


@router.get("", response_model=list[FileOut])
def list_files(ctx: AuthContext = Depends(get_auth), db: Session = Depends(get_db)):
    return db.scalars(
        select(File)
        .where(File.org_id == ctx.user.org_id)
        .order_by(File.created_at.desc())
        .limit(50)
    ).all()


@router.get("/{file_id}/download")
def download(
    file_id: uuid.UUID, ctx: AuthContext = Depends(get_auth), db: Session = Depends(get_db)
):
    row = in_org(db, File, file_id, ctx.user.org_id)
    path = storage.path_of(row.stored_path)
    if not path.is_file():
        # Its own message: in a deploy this means the storage volume is not the one
        # the file was written to.
        logger.error("File %s has a row but is missing from storage at %s", row.id, path)
        raise HTTPException(
            status_code=404, detail="The file is recorded but missing from STORAGE_DIR."
        )
    filename = row.filename
    # Released before the file is sent: a 450 MB download to a slow client takes
    # minutes, and a few of them at once would otherwise hold the whole pool.
    db.close()
    return FileResponse(path, media_type="application/pdf", filename=filename)
