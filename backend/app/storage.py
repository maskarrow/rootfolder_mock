"""Uploaded files on disk, under `STORAGE_DIR`.

The database keeps only the file's name (`<uuid>.pdf`); `path_of` builds the full
path from the current `STORAGE_DIR` every time. So the same rows work on a laptop
(`storage/` in the repo) and on a server (an absolute path on a mounted volume),
and moving the storage never breaks a row.

Starlette has already spooled the multipart body to a temporary file (in the
system temp directory, for anything over 1 MB) before the route runs; this copies
it into the storage in 1 MB chunks and enforces the cap on the way.
"""

import uuid
from pathlib import Path

from fastapi import UploadFile

from app.config import ROOT, settings

ALLOWED_EXTENSION = ".pdf"

_CHUNK_SIZE = 1024 * 1024


class UploadValidationError(Exception):
    pass


class UploadTooLargeError(Exception):
    pass


def root() -> Path:
    """`STORAGE_DIR`, relative to the repo root unless it is absolute."""
    return ROOT / settings.storage_dir


def path_of(stored_path: str) -> Path:
    return root() / stored_path


def save_upload(file: UploadFile, *, max_mb: int) -> tuple[str, int]:
    """Writes the upload to the storage. Returns the stored name and the size."""
    if not file.filename:
        raise UploadValidationError("The file name is missing.")
    ext = Path(file.filename).suffix.lower()
    if ext != ALLOWED_EXTENSION:
        raise UploadValidationError(f"Only {ALLOWED_EXTENSION} files are accepted.")

    root().mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4()}{ALLOWED_EXTENSION}"
    target = path_of(name)
    max_bytes = max_mb * 1024 * 1024

    size = 0
    try:
        with target.open("wb") as out:
            while chunk := file.file.read(_CHUNK_SIZE):
                size += len(chunk)
                if size > max_bytes:
                    raise UploadTooLargeError(f"The file exceeds the {max_mb} MB limit.")
                out.write(chunk)
    except Exception:
        # Never keep a partial file: too large, disk full, or a broken upload.
        target.unlink(missing_ok=True)
        raise

    return name, size
