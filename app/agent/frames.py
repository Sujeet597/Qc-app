"""Keeps the most recent video frames with a focus score, so the agent can pick the sharpest one."""
import threading
import time
from collections import deque
from dataclasses import dataclass

import numpy as np
from PIL import Image

from app.services import barcode, imaging


@dataclass
class FrameInfo:
    ts: float
    rgb: np.ndarray
    sharpness: float
    brightness: float

    @property
    def well_lit(self) -> bool:
        return imaging.MIN_BRIGHTNESS <= self.brightness <= imaging.MAX_BRIGHTNESS


class FrameSampler:
    def __init__(self, max_frames: int = 60, max_side: int = 1280) -> None:
        self._frames: deque[FrameInfo] = deque(maxlen=max_frames)
        self._codes: dict[str, float] = {}
        self._lock = threading.Lock()
        self.max_side = max_side
        self.decode_barcodes = False
        self.last_frame_at = 0.0

    def push(self, rgb: np.ndarray, ts: float | None = None) -> None:
        """Add one RGB frame (H x W x 3, uint8). Called from the video stream (~3 fps)."""
        ts = time.monotonic() if ts is None else ts
        frame = imaging.downscale(rgb, self.max_side)
        sharp, bright = imaging.sharpness_brightness(imaging.downscale(frame, 512))
        codes = barcode.decode_image(Image.fromarray(frame)) if self.decode_barcodes else []
        with self._lock:
            self._frames.append(FrameInfo(ts, frame, sharp, bright))
            self.last_frame_at = ts
            for c in codes:
                self._codes[c] = ts

    def best_since(self, since: float) -> FrameInfo | None:
        """Sharpest well-lit frame captured after `since` (falls back to sharpest of any)."""
        with self._lock:
            frames = [f for f in self._frames if f.ts >= since]
        if not frames:
            return None
        lit = [f for f in frames if f.well_lit] or frames
        return max(lit, key=lambda f: f.sharpness)

    def codes_since(self, since: float) -> list[str]:
        with self._lock:
            return [c for c, ts in self._codes.items() if ts >= since]
