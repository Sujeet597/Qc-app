import io
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="qc-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["STORAGE_DIR"] = f"{_tmp}/storage"
os.environ["AI_PROVIDER"] = "mock"
os.environ["AUTH_ENABLED"] = "true"   # tests cover login + permissions; test_no_auth switches it off
os.environ["JWT_SECRET"] = "test-secret-that-is-at-least-32-bytes-long"
os.environ["SEED_DEMO_DATA"] = "true"

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


def login(client, user, pw):
    r = client.post("/api/auth/login", data={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def textured_jpeg(rgb=(30, 40, 90), seed=0) -> bytes:
    """A sharp, fabric-like image (noise around a base color) so the quality gate passes."""
    rng = np.random.default_rng(seed)
    arr = np.clip(np.array(rgb) + rng.normal(0, 25, (400, 400, 3)), 0, 255).astype("uint8")
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def flat_jpeg(rgb=(30, 40, 90)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (400, 400), rgb).save(buf, format="JPEG")
    return buf.getvalue()
