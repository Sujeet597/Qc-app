"""LiveKit access tokens. The browser/mobile app joins the room with this token; vendor publishes
camera, QC team subscribes. A LiveKit token is an HS256 JWT signed with the API secret."""
import time

import jwt

from app.core.config import settings


def livekit_enabled() -> bool:
    return bool(settings.livekit_url and settings.livekit_api_key and settings.livekit_api_secret)


def room_token(room: str, identity: str, name: str, can_publish: bool, ttl_seconds: int = 4 * 3600,
               hidden: bool = False) -> str:
    now = int(time.time())
    claims = {
        "iss": settings.livekit_api_key,
        "sub": identity,
        "name": name,
        "nbf": now,
        "exp": now + ttl_seconds,
        "video": {
            "room": room,
            "roomJoin": True,
            "canPublish": can_publish,
            "canSubscribe": True,
            "canPublishData": True,
            "hidden": hidden,   # the QC agent does not appear as a participant tile
        },
    }
    return jwt.encode(claims, settings.livekit_api_secret, algorithm="HS256")
