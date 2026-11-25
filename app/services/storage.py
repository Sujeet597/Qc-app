"""Local file storage. Swap for S3 / Azure Blob in production by keeping the same two functions."""
import uuid
from pathlib import Path

from app.core.config import settings


def save_file(data: bytes, subdir: str, ext: str = ".jpg") -> str:
    folder = Path(settings.storage_dir) / subdir
    folder.mkdir(parents=True, exist_ok=True)
    rel = Path(subdir) / f"{uuid.uuid4().hex}{ext}"
    (Path(settings.storage_dir) / rel).write_bytes(data)
    return rel.as_posix()


def read_file(rel_path: str) -> bytes:
    return (Path(settings.storage_dir) / rel_path).read_bytes()
