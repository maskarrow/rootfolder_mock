"""PDF upload, listing and download.

What a deploy must get right here: the proxy's body size limit and streaming
(`MAX_UPLOAD_MB` is 500), the storage volume mount (`STORAGE_DIR`), and relative
paths, so a file uploaded before a move still downloads after it.
"""

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import storage
from app.config import settings
from app.deps import get_auth, get_db
from app.models.file import PROCESSING, File
from app.services import jobs
from app.services.auth import AuthContext
from app.tenancy import in_org

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/files", tags=["files"])


class FileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    size_bytes: int
    status: str
    created_at: datetime
    finished_at: datetime | None


@router.post("", response_model=FileOut, status_code=201)
def upload(
    file: UploadFile,
    background_tasks: BackgroundTasks,
    ctx: AuthContext = Depends(get_auth),
    db: Session = Depends(get_db),
):
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
    db.commit()
    logger.info("File %s stored: %s bytes", row.id, size)
    background_tasks.add_task(jobs.run, row.id)
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
    return FileResponse(path, media_type="application/pdf", filename=row.filename)
