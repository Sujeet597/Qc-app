"""The agent's brain. Independent of LiveKit so it can be tested with fake frames.

For each pending checklist point:
  1. send the instruction to the call ("Show the barcode sticker close up...")
  2. for scale / tape / GSM / supplier checks, wait for the vendor to type the reading
  3. give the vendor a few seconds to position the item, then watch a short window
  4. pick the sharpest, well-lit frame (and every barcode seen in the window)
  5. verify it with the same engine as the REST API and announce the result
  6. if the frame was unusable ("Reshow" / "Unable to read"), ask again (max N attempts)

Messages exchanged over the LiveKit data channel (topic "qc"), JSON:
  agent -> room : agent_ready | instruction | waiting_for_input | reshow | result | skipped | no_video | done | error
  room  -> agent: readings {check_no, measured?, supplier_code?, batch_no?} | codes {check_no, codes[]}
                  goto {check_no} | skip | capture | stop
"""
import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from sqlalchemy.orm import Session

from app.agent.frames import FrameSampler
from app.checklist import BY_NO, CHECKLIST, Method
from app.core.database import SessionLocal
from app.models import CheckStatus, Inspection
from app.services import imaging
from app.services.decision import summarize
from app.services.evidence import EvidenceError, record_evidence
from app.services.verification import CheckInputs, required_inputs

log = logging.getLogger("qc.agent")

INPUT_METHODS = {Method.MEASUREMENT, Method.VENDOR_MASTER}
RETRY_REASONS = ("Reshow", "Unable to read")

# Who may send which data-channel message. Call identities are "<role>-<user id>" (see /video-token).
_STAFF = {"qc", "approver", "admin"}
ALLOWED_SENDERS: dict[str, set[str]] = {
    "readings": {"vendor"} | _STAFF,   # vendor types the scale / tape / GSM / supplier value shown on camera
    "goto": {"vendor"} | _STAFF,
    "skip": {"vendor"} | _STAFF,
    "capture": {"vendor"} | _STAFF,
    "stop": {"vendor"} | _STAFF,
    "codes": _STAFF,                   # barcode values decide critical check 6 - never accepted from the vendor
}


def sender_allowed(kind: object, identity: str | None) -> bool:
    """True if a call participant with this identity may send this message type to the agent."""
    if not isinstance(kind, str) or not identity or identity.startswith("qc-agent-"):
        return False
    role, _, user_id = identity.partition("-")
    return user_id.isdigit() and role in ALLOWED_SENDERS.get(kind, set())


class Messenger(Protocol):
    async def send(self, msg: dict) -> None: ...


@dataclass
class AgentTiming:
    settle_s: float = 3.0            # time to bring the item in front of the camera
    window_s: float = 4.0            # frames watched to choose the best one
    no_video_timeout_s: float = 20.0
    input_timeout_s: float = 300.0   # waiting for scale / tape / supplier entry
    max_attempts: int = 3


class InspectionController:
    def __init__(self, inspection_id: int, sampler: FrameSampler, messenger: Messenger,
                 timing: AgentTiming | None = None, only: list[int] | None = None,
                 session_factory: Callable[[], Session] = SessionLocal) -> None:
        self.inspection_id = inspection_id
        self.sampler = sampler
        self.messenger = messenger
        self.t = timing or AgentTiming()
        self.only = only
        self.session_factory = session_factory
        self.readings: dict[int, dict] = {}
        self.client_codes: dict[int, list[str]] = {}
        self._wake = asyncio.Event()
        self._goto: int | None = None
        self._skip = False
        self._capture = False
        self._stop = False

    # ---------- incoming messages (call from the transport) ----------
    def handle_message(self, msg: dict) -> None:
        kind = msg.get("type")
        try:
            if kind == "readings":
                self.readings[int(msg["check_no"])] = msg
            elif kind == "codes":
                self.client_codes.setdefault(int(msg["check_no"]), []).extend(str(c) for c in msg.get("codes", []))
            elif kind == "goto":
                self._goto = int(msg["check_no"])
            elif kind == "skip":
                self._skip = True
            elif kind == "capture":
                self._capture = True
            elif kind == "stop":
                self._stop = True
            else:
                return
        except (KeyError, TypeError, ValueError):
            log.warning("bad message %s", msg)
            return
        self._wake.set()

    @property
    def _interrupted(self) -> bool:
        return self._stop or self._skip or self._goto is not None

    async def _sleep(self, seconds: float, until: Callable[[], bool] | None = None) -> None:
        """Sleep, but wake early on goto / skip / stop / capture or when `until()` becomes true."""
        deadline = time.monotonic() + seconds
        while (remaining := deadline - time.monotonic()) > 0:
            if self._interrupted or self._capture or (until and until()):
                return
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=min(remaining, 0.25))
            except asyncio.TimeoutError:
                pass

    # ---------- DB helpers ----------
    def _load(self) -> tuple[list[int], list[str], str]:
        with self.session_factory() as db:
            insp = db.get(Inspection, self.inspection_id)
            if insp is None:
                raise RuntimeError(f"Inspection {self.inspection_id} not found")
            pending = [c.check_no for c in insp.checks if c.status == CheckStatus.PENDING]
            keys = list((insp.po.size_chart.get(insp.size) or {}).keys())
            return pending, keys, insp.status

    def _fields(self, no: int) -> list[str]:
        with self.session_factory() as db:
            return required_inputs(db.get(Inspection, self.inspection_id), no)

    def _record(self, no: int, jpeg: bytes, inputs: CheckInputs) -> dict:
        with self.session_factory() as db:
            insp = db.get(Inspection, self.inspection_id)
            chk = record_evidence(db, insp, no, jpeg, inputs, source="live_agent")
            return {"status": chk.status, "reason": chk.reason, "observed": chk.observed_value,
                    "expected": chk.expected_value, "confidence": chk.confidence,
                    "summary": summarize(insp)}

    # ---------- main loop ----------
    async def run(self) -> None:
        await self.messenger.send({"type": "agent_ready", "inspection_id": self.inspection_id})
        attempted: set[int] = set()
        try:
            while not self._stop:
                pending, _, _ = await asyncio.to_thread(self._load)
                if self._goto is not None:
                    no, self._goto = self._goto, None
                    if no not in BY_NO:
                        continue
                else:
                    order = [c.no for c in CHECKLIST if c.no in pending and c.no not in attempted
                             and (self.only is None or c.no in self.only)]
                    if not order:
                        break
                    no = order[0]
                self._skip = False
                if await self._do_check(no):
                    attempted.add(no)
        except EvidenceError as e:
            await self.messenger.send({"type": "error", "message": str(e)})
            return
        pending, _, _ = await asyncio.to_thread(self._load)
        await self.messenger.send({"type": "done", "pending_checks": pending})

    async def _do_check(self, no: int) -> bool:
        d = BY_NO[no]
        fields = await asyncio.to_thread(self._fields, no)

        for attempt in range(1, self.t.max_attempts + 1):
            self._capture = False
            await self.messenger.send({"type": "instruction", "check_no": no, "title": d.title,
                                       "instruction": d.instruction, "attempt": attempt,
                                       "input_fields": fields})
            if fields and no not in self.readings:
                await self.messenger.send({"type": "waiting_for_input", "check_no": no, "fields": fields})
                await self._sleep(self.t.input_timeout_s, until=lambda: no in self.readings)
                if self._interrupted or no not in self.readings:
                    await self.messenger.send({"type": "skipped", "check_no": no,
                                               "reason": "No reading entered" if not self._interrupted else "Interrupted"})
                    return not (self._goto is not None or self._stop)

            self.sampler.decode_barcodes = d.method == Method.BARCODE_MATCH
            started = time.monotonic()
            await self._sleep(self.t.settle_s)
            window_start = time.monotonic() if not self._capture else time.monotonic() - 1.0
            if not self._capture:
                await self._sleep(self.t.window_s)
            if self._interrupted:
                self.sampler.decode_barcodes = False
                await self.messenger.send({"type": "skipped", "check_no": no, "reason": "Interrupted"})
                return not (self._goto is not None or self._stop)

            best = self.sampler.best_since(window_start)
            if best is None:  # camera not sending yet
                await self._sleep(self.t.no_video_timeout_s, until=lambda: self.sampler.best_since(window_start) is not None)
                best = self.sampler.best_since(window_start)
            self.sampler.decode_barcodes = False
            if best is None:
                await self.messenger.send({"type": "no_video", "check_no": no,
                                           "message": "No camera video received - check the vendor camera."})
                return True

            reading = self.readings.get(no, {})
            inputs = CheckInputs(
                codes=self.sampler.codes_since(started) + self.client_codes.pop(no, []),
                measured=reading.get("measured") or {},
                supplier_code=reading.get("supplier_code"),
                batch_no=reading.get("batch_no"),
            )
            result = await asyncio.to_thread(self._record, no, imaging.encode_jpeg(best.rgb), inputs)
            log.info("check %s attempt %s -> %s (%s)", no, attempt, result["status"], result["reason"])

            retry = (result["status"] == CheckStatus.MANUAL_REVIEW and result["reason"].startswith(RETRY_REASONS)
                     and attempt < self.t.max_attempts)
            if retry:
                await self.messenger.send({"type": "reshow", "check_no": no, "reason": result["reason"]})
                continue
            await self.messenger.send({"type": "result", "check_no": no, "title": d.title, **{
                k: result[k] for k in ("status", "reason", "observed", "expected", "confidence")},
                "progress": result["summary"]["progress"]})
            return True
        return True
