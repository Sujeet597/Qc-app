"""Runs the live agent controller against a fake video feed (no LiveKit server needed)."""
import asyncio

import numpy as np
import pytest
from conftest import login
from PIL import Image

from app.agent.controller import AgentTiming, InspectionController
from app.agent.frames import FrameSampler

FAST = AgentTiming(settle_s=0.05, window_s=0.15, no_video_timeout_s=0.3, input_timeout_s=0.5, max_attempts=3)


class FakeMessenger:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, msg):
        self.sent.append(msg)

    def of(self, kind, no=None):
        return [m for m in self.sent if m["type"] == kind and (no is None or m.get("check_no") == no)]


def fabric(rgb=(30, 40, 90), seed=0):
    rng = np.random.default_rng(seed)
    return np.clip(np.array(rgb) + rng.normal(0, 25, (480, 640, 3)), 0, 255).astype("uint8")


def barcode_frame(text="8901234567890"):
    zxingcpp = pytest.importorskip("zxingcpp")
    code = zxingcpp.create_barcode(text, zxingcpp.BarcodeFormat.EAN13).to_image(scale=3)
    img = Image.new("RGB", (640, 480), "white")
    img.paste(Image.fromarray(np.asarray(code)).convert("RGB"), (60, 120))
    return np.asarray(img)


async def feed(sampler, frame_fn, stop: asyncio.Event, fps=30):
    while not stop.is_set():
        sampler.push(frame_fn())
        await asyncio.sleep(1 / fps)


def new_inspection(client, size):
    vendor = login(client, "vendor1", "vendor123")
    po = client.get("/api/pos", headers=vendor).json()[0]
    r = client.post("/api/inspections", headers=vendor, json={"po_id": po["id"], "color_name": "NAVY BLUE", "size": size})
    assert r.status_code == 201, r.text
    return r.json()["id"], vendor


def test_agent_runs_all_checks_from_video(client):
    iid, vendor = new_inspection(client, "L")

    async def scenario():
        sampler, msgr = FrameSampler(), FakeMessenger()
        ctl = InspectionController(iid, sampler, msgr, FAST)
        # the vendor types readings in the UI; they arrive over the data channel
        ctl.handle_message({"type": "readings", "check_no": 8, "measured": {"weight_g": 205}})
        ctl.handle_message({"type": "readings", "check_no": 10, "measured": {"gsm": 200}})
        ctl.handle_message({"type": "codes", "check_no": 6, "codes": ["8901234567890", "TF-7788-M"]})
        for no in (17, 18, 19, 20):
            ctl.handle_message({"type": "readings", "check_no": no, "supplier_code": "NOT-NOMINATED"})
        stop = asyncio.Event()
        feeder = asyncio.create_task(feed(sampler, lambda: fabric(seed=np.random.randint(1e6)), stop))
        await ctl.run()
        stop.set()
        await feeder
        return msgr

    msgr = asyncio.run(scenario())
    results = {m["check_no"]: m["status"] for m in msgr.of("result")}

    assert msgr.sent[0]["type"] == "agent_ready" and msgr.sent[-1]["type"] == "done"
    assert len(msgr.of("instruction")) >= 20 - 1            # every check was announced (9 skipped -> still announced)
    assert results[6] == "pass"                              # codes match PO
    assert results[8] == "pass"                              # 205 g within 210 +/- 10
    assert results[10] == "fail"                             # 200 gsm vs 180 +/- 8
    assert all(results[n] == "fail" for n in (17, 18, 19, 20))  # supplier not nominated
    assert results[16] == "manual_review"                    # mock AI never passes visual checks
    # size L has no size chart on the demo PO -> reading fields empty -> check 9 goes to QC
    assert results[9] == "manual_review"

    qc = login(client, "qc", "qc123")
    detail = client.get(f"/api/inspections/{iid}", headers=qc).json()
    assert detail["progress"] == "20 of 20 checks completed"


def test_agent_asks_to_reshow_blurry_frames_and_reads_barcode_from_video(client):
    iid, _ = new_inspection(client, "S")

    async def scenario():
        sampler, msgr = FrameSampler(), FakeMessenger()
        ctl = InspectionController(iid, sampler, msgr, FAST, only=[16, 6])
        # checks run in checklist order: 6 (barcode) first, then 16 (ironing)
        state = {"frame": barcode_frame()}                   # vendor shows the barcode label

        async def vendor_reacts():
            while not msgr.of("instruction", 16):
                await asyncio.sleep(0.01)
            state["frame"] = np.full((480, 640, 3), 120, dtype="uint8")  # garment out of focus
            while not msgr.of("reshow", 16):
                await asyncio.sleep(0.01)
            state["frame"] = fabric()                        # vendor refocuses

        stop = asyncio.Event()
        feeder = asyncio.create_task(feed(sampler, lambda: state["frame"], stop))
        reactor = asyncio.create_task(vendor_reacts())
        await ctl.run()
        stop.set()
        await feeder
        reactor.cancel()
        return msgr

    msgr = asyncio.run(scenario())
    assert msgr.of("reshow", 16), "agent should ask to reshow the blurry item"
    r16 = msgr.of("result", 16)[0]
    assert "Reshow" not in r16["reason"]                     # final verdict came from a sharp frame
    # barcode decoded from the video itself; taffeta not shown -> QC confirms
    r6 = msgr.of("result", 6)[0]
    assert "8901234567890" in r6["observed"]
    assert r6["status"] == "manual_review" and "taffeta" in r6["reason"]


def test_agent_reports_missing_video(client):
    vendor = login(client, "vendor1", "vendor123")
    insp = next(i for i in client.get("/api/inspections", headers=vendor).json()
                if i["status"] == "in_progress" and i["size"] == "S")   # from the previous test; check 12 still pending

    async def scenario():
        msgr = FakeMessenger()
        await InspectionController(insp["id"], FrameSampler(), msgr, FAST, only=[12]).run()
        return msgr

    msgr = asyncio.run(scenario())
    assert msgr.of("no_video", 12)
