"""Vision AI provider. The AI only *looks and reads*; the code in verification.py makes the
Pass/Fail decision against PO data and thresholds, so the rules stay auditable."""
import base64
import io
import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal, Protocol

from PIL import Image

from app.core.config import settings

Verdict = Literal["pass", "fail", "unsure"]


@dataclass
class VisionTask:
    check_title: str
    instruction: str                 # what to verify
    frame: bytes                     # live frame from the vendor's video
    reference: bytes | None = None   # approved PPS / sealer image
    context: dict | None = None      # PO values the AI may need (expected size, color name ...)


@dataclass
class AIResult:
    verdict: Verdict
    confidence: float
    observed_text: str
    reason: str
    defects: list[str]

    @classmethod
    def unsure(cls, reason: str) -> "AIResult":
        return cls("unsure", 0.0, "", reason, [])


class VisionProvider(Protocol):
    def analyze(self, task: VisionTask) -> AIResult: ...


class MockProvider:
    """No AI configured: every visual check goes to Manual Review. Never fakes a Pass."""

    def analyze(self, task: VisionTask) -> AIResult:
        return AIResult.unsure("AI provider not configured (AI_PROVIDER=mock) - QC must verify manually.")


_SYSTEM = """You are a garment quality-control inspector reviewing frames from a vendor's live video call.
You compare bulk-production garments with the approved PPS / sealer sample and read labels exactly.
Rules:
- Judge only what is clearly visible. If the image is blurry, cropped, badly lit, or the item is not shown, answer "unsure".
- Never guess label text. Copy text exactly as printed into observed_text; leave it empty if unreadable.
- "fail" only for a clear, visible mismatch or defect; list each defect briefly.
- confidence is your probability (0-1) that the verdict is correct."""

_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail", "unsure"]},
        "confidence": {"type": "number"},
        "observed_text": {"type": "string"},
        "defects": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "confidence", "observed_text", "defects", "reason"],
    "additionalProperties": False,
}


def _image_block(data: bytes) -> dict:
    img = Image.open(io.BytesIO(data)).convert("RGB")
    img.thumbnail((1568, 1568))  # larger images cost more tokens without helping
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg",
                   "data": base64.standard_b64encode(buf.getvalue()).decode()},
    }


class ClaudeProvider:
    def __init__(self) -> None:
        import anthropic
        self._anthropic = anthropic
        self.client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY

    def analyze(self, task: VisionTask) -> AIResult:
        content: list[dict] = []
        if task.reference:
            content += [{"type": "text", "text": "APPROVED REFERENCE (PPS / sealer):"}, _image_block(task.reference)]
        content += [{"type": "text", "text": "LIVE FRAME from the vendor's video call:"}, _image_block(task.frame)]
        prompt = f"Checklist point: {task.check_title}\nTask: {task.instruction}"
        if task.context:
            prompt += f"\nPO data: {json.dumps(task.context)}"
        content.append({"type": "text", "text": prompt})

        anthropic = self._anthropic
        try:
            resp = self.client.beta.messages.create(
                model=settings.anthropic_model,
                max_tokens=16000,
                system=_SYSTEM,
                messages=[{"role": "user", "content": content}],
                output_config={"effort": "medium", "format": {"type": "json_schema", "schema": _SCHEMA}},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.RateLimitError:
            return AIResult.unsure("AI rate limited - retry or verify manually.")
        except anthropic.APIStatusError as e:
            return AIResult.unsure(f"AI error {e.status_code} - verify manually.")
        except anthropic.APIConnectionError:
            return AIResult.unsure("AI unreachable - verify manually.")

        if resp.stop_reason != "end_turn":
            return AIResult.unsure(f"AI stopped early ({resp.stop_reason}) - verify manually.")
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if not text:
            return AIResult.unsure("AI returned no result - verify manually.")
        data = json.loads(text)
        return AIResult(
            verdict=data["verdict"],
            confidence=max(0.0, min(1.0, float(data["confidence"]))),
            observed_text=data["observed_text"],
            reason=data["reason"],
            defects=data["defects"],
        )


@lru_cache
def get_provider() -> VisionProvider:
    if settings.ai_provider == "claude":
        return ClaudeProvider()
    return MockProvider()
