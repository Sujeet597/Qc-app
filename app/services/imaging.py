"""Frame quality gate and color measurement. Pure numpy + Pillow, no AI needed."""
import io
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

MIN_SHARPNESS = 60.0         # Laplacian variance below this = blurry
MIN_BRIGHTNESS = 40.0
MAX_BRIGHTNESS = 230.0
MIN_IMAGE_SIDE = 100


def load_rgb(data: bytes, max_side: int = 1024) -> np.ndarray:
    img = Image.open(io.BytesIO(data)).convert("RGB")
    img.thumbnail((max_side, max_side))
    return np.asarray(img, dtype=np.float64)


@dataclass
class Quality:
    ok: bool
    sharpness: float
    brightness: float
    issues: list[str] = field(default_factory=list)


def sharpness_brightness(rgb: np.ndarray) -> tuple[float, float]:
    """Laplacian variance (focus) and mean brightness of an RGB array."""
    gray = rgb.astype(np.float64) @ np.array([0.299, 0.587, 0.114])
    if gray.shape[0] < 3 or gray.shape[1] < 3:
        bright = float(gray.mean()) if gray.size else 0.0
        return 0.0, bright if np.isfinite(bright) else 0.0
    lap = (-4 * gray[1:-1, 1:-1] + gray[:-2, 1:-1] + gray[2:, 1:-1] + gray[1:-1, :-2] + gray[1:-1, 2:])
    sharp, bright = float(lap.var()), float(gray.mean())
    return (sharp if np.isfinite(sharp) else 0.0), (bright if np.isfinite(bright) else 0.0)


def downscale(rgb: np.ndarray, max_side: int) -> np.ndarray:
    h, w = rgb.shape[:2]
    if max(h, w) <= max_side:
        return rgb
    img = Image.fromarray(rgb.astype(np.uint8))
    img.thumbnail((max_side, max_side))
    return np.asarray(img)


def encode_jpeg(rgb: np.ndarray, quality: int = 92) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgb.astype(np.uint8)).save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def assess_quality(data: bytes) -> Quality:
    # Size rule on the original photo: after shrinking to 512 px a sharp wide label photo (e.g. 1920x300)
    # would look "too small".
    with Image.open(io.BytesIO(data)) as img:
        w, h = img.size
    rgb = load_rgb(data, 512)
    sharpness, brightness = sharpness_brightness(rgb)
    issues = []
    if min(h, w) < MIN_IMAGE_SIDE:
        issues.append(f"image too small ({w}x{h}) - move closer or check the camera.")
    if not np.isfinite(sharpness) or sharpness < MIN_SHARPNESS:
        issues.append("Image is blurry - hold the camera steady and refocus.")
    if not np.isfinite(brightness) or brightness < MIN_BRIGHTNESS:
        issues.append("Image is too dark - switch on the inspection lamp.")
    elif brightness > MAX_BRIGHTNESS:
        issues.append("Image is overexposed - reduce glare / move away from the light.")
    return Quality(ok=not issues, sharpness=sharpness, brightness=brightness, issues=issues)


MAX_CLIPPED = 0.10   # share of pixels allowed to be pure black / blown-out white in a color check frame


def lighting_issues(data: bytes) -> list[str]:
    """Lighting problems that make a color comparison unreliable (BRD 4A). Average brightness can look fine
    while large areas are blown out by glare or lost in shadow - those areas have no usable color."""
    gray = load_rgb(data, 512) @ np.array([0.299, 0.587, 0.114])
    issues = []
    if float((gray >= 250).mean()) > MAX_CLIPPED:
        issues.append("Glare / overexposed areas - tilt the garment or move the lamp.")
    if float((gray <= 5).mean()) > MAX_CLIPPED:
        issues.append("Deep shadows - light the garment evenly.")
    return issues


def _srgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    c = rgb / 255.0
    c = np.where(c > 0.04045, ((c + 0.055) / 1.055) ** 2.4, c / 12.92)
    m = np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]])
    xyz = c @ m.T / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], axis=-1)


def center_lab(data: bytes) -> np.ndarray:
    """Mean Lab color of the central 50% of the frame (where the garment should be)."""
    rgb = load_rgb(data)
    h, w, _ = rgb.shape
    crop = (rgb if h < 4 or w < 4 else rgb[h // 4: 3 * h // 4, w // 4: 3 * w // 4]).reshape(-1, 3)
    if crop.size == 0:
        raise ValueError("Image has no pixels")
    return _srgb_to_lab(crop.mean(axis=0))


def delta_e(frame: bytes, reference: bytes) -> float:
    """CIE76 Delta-E. < 2 hard to see, 2-3.5 visible on close look, > 5 clearly different.
    Only meaningful when both photos are taken under the same controlled light (use a reference card)."""
    de = float(np.linalg.norm(center_lab(frame) - center_lab(reference)))
    if not np.isfinite(de):
        raise ValueError("Non-finite Delta-E")
    return de
