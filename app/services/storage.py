"""Local file storage. Swap for S3 / Azure Blob in production by keeping the same functions.

Paths are always built and checked here, so a PO number like "../../etc" or a file name like
"x.php" can never place or read a file outside STORAGE_DIR.
"""
import re
import uuid
from pathlib import Path

from app.core.config import settings

_EXT = re.compile(r"^\.[a-z0-9]{1,5}$")


def _safe_part(part: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", part)[:64].strip(".")
    return cleaned or "_"


def _root() -> Path:
    return Path(settings.storage_dir).resolve()


def _inside_root(rel_path: str) -> Path:
    path = (_root() / rel_path).resolve()
    if not path.is_relative_to(_root()):
        raise ValueError("Path is outside the storage folder")
    return path


def save_file(data: bytes, subdir: str, ext: str = ".jpg") -> str:
    if not _EXT.match(ext):
        raise ValueError(f"Bad file extension {ext!r}")
    folder = Path(*(_safe_part(p) for p in Path(subdir).parts))
    rel = folder / f"{uuid.uuid4().hex}{ext}"
    target = _inside_root(rel.as_posix())
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return rel.as_posix()


def read_file(rel_path: str) -> bytes:
    return _inside_root(rel_path).read_bytes()
