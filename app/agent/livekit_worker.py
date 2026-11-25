"""Joins the inspection's LiveKit room as a hidden participant, reads the vendor's camera,
and runs the InspectionController.

    python -m app.agent.livekit_worker --inspection 12
"""
import argparse
import asyncio
import json
import logging
import time

import numpy as np
from livekit import rtc

from app.agent.controller import AgentTiming, InspectionController
from app.agent.frames import FrameSampler
from app.core.config import settings
from app.core.database import SessionLocal
from app.models import Inspection
from app.services import video

TOPIC = "qc"
SAMPLE_FPS = 3          # frames per second analysed (video itself stays full rate for humans)
log = logging.getLogger("qc.agent")


class LiveKitMessenger:
    def __init__(self, room: rtc.Room) -> None:
        self.room = room

    async def send(self, msg: dict) -> None:
        log.info("-> %s", msg)
        try:
            await self.room.local_participant.publish_data(json.dumps(msg, default=str), reliable=True, topic=TOPIC)
        except Exception:
            log.exception("publish_data failed")


async def _consume_video(track: rtc.Track, sampler: FrameSampler) -> None:
    stream = rtc.VideoStream(track, format=rtc.VideoBufferType.RGBA)
    last = 0.0
    try:
        async for event in stream:
            now = time.monotonic()
            if now - last < 1 / SAMPLE_FPS:
                continue
            last = now
            f = event.frame
            rgba = np.frombuffer(f.data, dtype=np.uint8).reshape(f.height, f.width, 4)
            await asyncio.to_thread(sampler.push, rgba[:, :, :3].copy(), now)
    finally:
        await stream.aclose()


async def run_agent(inspection_id: int, timing: AgentTiming | None = None) -> None:
    if not video.livekit_enabled():
        raise SystemExit("LiveKit is not configured (LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET)")
    with SessionLocal() as db:
        insp = db.get(Inspection, inspection_id)
        if insp is None or not insp.video_room:
            raise SystemExit(f"Inspection {inspection_id} not found")
        room_name = insp.video_room

    room = rtc.Room()
    sampler = FrameSampler()
    controller = InspectionController(inspection_id, sampler, LiveKitMessenger(room), timing)
    video_tasks: set[asyncio.Task] = set()
    disconnected = asyncio.Event()

    @room.on("track_subscribed")
    def on_track(track: rtc.Track, publication: rtc.RemoteTrackPublication, participant: rtc.RemoteParticipant):
        if track.kind == rtc.TrackKind.KIND_VIDEO and participant.identity.startswith("vendor-"):
            log.info("watching camera of %s", participant.identity)
            task = asyncio.create_task(_consume_video(track, sampler))
            video_tasks.add(task)
            task.add_done_callback(video_tasks.discard)

    @room.on("data_received")
    def on_data(packet: rtc.DataPacket):
        if packet.topic != TOPIC:
            return
        try:
            msg = json.loads(packet.data)
        except ValueError:
            return
        log.info("<- %s", msg)
        controller.handle_message(msg)

    @room.on("disconnected")
    def on_disconnected(*_):
        disconnected.set()
        controller.handle_message({"type": "stop"})

    token = video.room_token(room_name, f"qc-agent-{inspection_id}", "QC Agent", can_publish=False, hidden=True)
    await room.connect(settings.livekit_url, token)
    log.info("joined room %s", room_name)
    try:
        await controller.run()
    finally:
        for t in list(video_tasks):
            t.cancel()
        if not disconnected.is_set():
            await room.disconnect()


def main() -> None:
    p = argparse.ArgumentParser(description="Live QC video agent")
    p.add_argument("--inspection", type=int, required=True)
    p.add_argument("--settle", type=float, default=3.0, help="seconds to position item after instruction")
    p.add_argument("--window", type=float, default=4.0, help="seconds of video watched per check")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run_agent(args.inspection, AgentTiming(settle_s=args.settle, window_s=args.window)))


if __name__ == "__main__":
    main()
