"""Checks every uploaded photo before it is stored or processed.

- size limit (MAX_UPLOAD_MB), read in a bounded way so a huge upload is never fully loaded
- must really be a JPEG or PNG (checked from the content, not the file name)
- pixel limit (MAX_IMAGE_PIXELS), checked from the header before decoding, so a small file that
  expands to a giant image ("decompression bomb") is refused
"""
import io

from fastapi import HTTPException, UploadFile
from PIL import Image, UnidentifiedImageError

from app.core.config import settings

ALLOWED = {"JPEG": ".jpg", "PNG": ".png"}


def check_image(data: bytes) -> str:
    """Return the file extension for valid image bytes, or raise 413 / 415 / 422."""
    if not data:
        raise HTTPException(422, "Empty file")
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"Photo is larger than {settings.max_upload_mb:g} MB")
    try:
        with Image.open(io.BytesIO(data)) as img:   # reads the header only
            fmt, (w, h) = img.format, img.size
            if fmt not in ALLOWED:
                raise HTTPException(415, "Only JPEG or PNG photos are accepted")
            if w * h > settings.max_image_pixels:
                raise HTTPException(422, f"Image is too large ({w}x{h} pixels)")
            img.verify()                             # detects truncated / corrupt files
    except HTTPException:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise HTTPException(415, "File is not a valid JPEG or PNG image")
    return ALLOWED[fmt]


def read_image(upload: UploadFile) -> tuple[bytes, str]:
    """Read an uploaded photo (at most the size limit + 1 byte) and validate it."""
    limit = int(settings.max_upload_mb * 1024 * 1024)
    data = upload.file.read(limit + 1)
    return data, check_image(data)
