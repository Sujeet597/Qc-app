"""Upload limits, safe storage, user management, nominated vendor master, PO edit, reference delete,
vendor inspection scope and inspection start checks. Each test creates its own users / POs (unique names),
so it does not depend on other tests or on test order."""
import io
import uuid
from pathlib import Path

import pytest
from conftest import login, textured_jpeg
from fastapi import HTTPException
from PIL import Image

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import Inspection, InspectionStatus
from app.services import storage, uploads


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def admin(client):
    return login(client, "admin", "admin123")


@pytest.fixture
def qc(client):
    return login(client, "qc", "qc123")


def _new_po(client, admin, vendor_code, **kw):
    body = dict(po_number=_uid("PO"), vendor_code=vendor_code, article_number="ART-1", style_number="ST-1",
                color_name="NAVY BLUE", mc="20 PCS", barcode_number="111", taffeta_number="222", sizes=["S", "M"],
                weight_target_g=200, weight_tolerance_g=10, packing_norms={"pcs_per_inner": 5}) | kw
    r = client.post("/api/pos", headers=admin, json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _new_user(client, admin, role="vendor", vendor_code=None, password="secret-pass-1"):
    name = _uid(role)
    r = client.post("/api/users", headers=admin, json={"username": name, "password": password, "role": role,
                                                        "vendor_code": vendor_code, "full_name": name.title()})
    assert r.status_code == 201, r.text
    return r.json(), login(client, name, password)


def _png(size=(200, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 20, 30)).save(buf, "PNG")
    return buf.getvalue()


# ---------- #8 uploads + storage ----------

def test_upload_checks_size_type_and_pixels(monkeypatch):
    assert uploads.check_image(textured_jpeg()) == ".jpg"
    assert uploads.check_image(_png()) == ".png"
    for bad in (b"", b"not an image at all", b"GIF89a" + b"\0" * 50):
        with pytest.raises(HTTPException) as e:
            uploads.check_image(bad)
        assert e.value.status_code in (415, 422)
    gif = io.BytesIO()
    Image.new("RGB", (10, 10)).save(gif, "GIF")
    with pytest.raises(HTTPException) as e:
        uploads.check_image(gif.getvalue())
    assert e.value.status_code == 415
    monkeypatch.setattr(settings, "max_image_pixels", 100 * 100)
    with pytest.raises(HTTPException) as e:        # small file, too many pixels: refused before decoding
        uploads.check_image(_png((400, 400)))
    assert e.value.status_code == 422
    monkeypatch.setattr(settings, "max_upload_mb", 0.001)
    with pytest.raises(HTTPException) as e:
        uploads.check_image(textured_jpeg())
    assert e.value.status_code == 413


def test_storage_never_leaves_its_folder():
    rel = storage.save_file(b"x", "references/../../../etc", ".jpg")
    assert ".." not in rel and (Path(settings.storage_dir).resolve() / rel).is_file()
    with pytest.raises(ValueError):
        storage.save_file(b"x", "references", "/../../x.php")
    with pytest.raises(ValueError):
        storage.read_file("../../outside.txt")


def test_evidence_upload_rejects_non_image(client, admin):
    po = _new_po(client, admin, _uid("V"))
    _, vendor = _new_user(client, admin, vendor_code=po["vendor_code"])
    iid = client.post("/api/inspections", headers=vendor, json={"po_id": po["id"], "color_name": "NAVY BLUE",
                                                                  "size": "M"}).json()["id"]
    r = client.post(f"/api/inspections/{iid}/checks/8/evidence", headers=vendor,
                    files={"frame": ("f.jpg", b"<?php echo 1; ?>", "image/jpeg")})
    assert r.status_code == 415


# ---------- #24 / #25 inspections ----------

def test_vendor_sees_all_inspections_of_their_vendor_code(client, admin):
    code = _uid("V")
    po = _new_po(client, admin, code)
    _, alice = _new_user(client, admin, vendor_code=code)
    _, bob = _new_user(client, admin, vendor_code=code)
    _, other = _new_user(client, admin, vendor_code=_uid("V"))
    iid = client.post("/api/inspections", headers=alice, json={"po_id": po["id"], "color_name": "NAVY BLUE",
                                                                 "size": "S"}).json()["id"]
    assert iid in [i["id"] for i in client.get("/api/inspections", headers=bob).json()]       # same vendor
    assert iid not in [i["id"] for i in client.get("/api/inspections", headers=other).json()]
    assert client.get(f"/api/inspections/{iid}", headers=other).status_code == 404


def test_inspection_color_must_be_po_color(client, admin):
    code = _uid("V")
    po = _new_po(client, admin, code)
    _, vendor = _new_user(client, admin, vendor_code=code)
    r = client.post("/api/inspections", headers=vendor, json={"po_id": po["id"], "color_name": "RED", "size": "M"})
    assert r.status_code == 422 and "NAVY BLUE" in r.text
    r = client.post("/api/inspections", headers=vendor, json={"po_id": po["id"], "color_name": " navy  blue",
                                                               "size": "M"})
    assert r.status_code == 201 and r.json()["color_name"] == "NAVY BLUE"


def test_no_video_token_after_inspection_is_closed(client, admin):
    code = _uid("V")
    po = _new_po(client, admin, code)
    _, vendor = _new_user(client, admin, vendor_code=code)
    iid = client.post("/api/inspections", headers=vendor, json={"po_id": po["id"], "color_name": "NAVY BLUE",
                                                                  "size": "M"}).json()["id"]
    with SessionLocal() as db:
        db.get(Inspection, iid).status = InspectionStatus.SUBMITTED
        db.commit()
    assert client.post(f"/api/inspections/{iid}/video-token", headers=vendor).status_code == 409


# ---------- #5 users ----------

def test_user_management(client, admin, qc):
    assert client.post("/api/users", headers=admin, json={"username": _uid("v"), "password": "secret-pass-1",
                                                           "role": "vendor"}).status_code == 422   # no vendor code
    assert client.post("/api/users", headers=admin, json={"username": _uid("q"), "password": "secret-pass-1",
                                                           "role": "qc", "vendor_code": "V1"}).status_code == 422
    assert client.post("/api/users", headers=admin, json={"username": _uid("q"), "password": "short",
                                                           "role": "qc"}).status_code == 422
    assert client.get("/api/users", headers=qc).status_code == 403

    user, _ = _new_user(client, admin, role="qc")
    dup = client.post("/api/users", headers=admin, json={"username": user["username"], "password": "secret-pass-1",
                                                          "role": "qc"})
    assert dup.status_code == 409
    assert "password_hash" not in user

    # reset password, then disable: login stops working
    assert client.post(f"/api/users/{user['id']}/password", headers=admin,
                       json={"new_password": "new-secret-2"}).status_code == 204
    login(client, user["username"], "new-secret-2")
    r = client.patch(f"/api/users/{user['id']}", headers=admin, json={"active": False})
    assert r.status_code == 200 and r.json()["active"] is False
    assert client.post("/api/auth/login", data={"username": user["username"],
                                                "password": "new-secret-2"}).status_code == 401

    # role change: staff -> vendor needs a vendor code
    assert client.patch(f"/api/users/{user['id']}", headers=admin, json={"role": "vendor"}).status_code == 422
    r = client.patch(f"/api/users/{user['id']}", headers=admin, json={"role": "vendor", "vendor_code": "V9"})
    assert r.json()["role"] == "vendor" and r.json()["vendor_code"] == "V9"


def test_admin_cannot_lock_themselves_out(client, admin):
    me = client.get("/api/auth/me", headers=admin).json()
    assert client.patch(f"/api/users/{me['id']}", headers=admin, json={"active": False}).status_code == 409
    assert client.patch(f"/api/users/{me['id']}", headers=admin, json={"role": "qc"}).status_code == 409


def test_user_changes_own_password(client, admin):
    user, token = _new_user(client, admin, role="approver")
    assert client.post("/api/auth/password", headers=token, json={"current_password": "wrong",
                                                                   "new_password": "another-pass-3"}).status_code == 403
    assert client.post("/api/auth/password", headers=token, json={"current_password": "secret-pass-1",
                                                                   "new_password": "another-pass-3"}).status_code == 204
    login(client, user["username"], "another-pass-3")


# ---------- #6 nominated vendors ----------

def test_nominated_vendor_master(client, admin, qc):
    code = _uid("SUP")
    r = client.post("/api/nominated-vendors", headers=admin, json={"category": "zip", "supplier_code": code,
                                                                     "supplier_name": "Zip Co"})
    assert r.status_code == 201
    vid = r.json()["id"]
    assert client.post("/api/nominated-vendors", headers=admin, json={
        "category": "zip", "supplier_code": f" {code.lower()} ", "supplier_name": "Dup"}).status_code == 409
    assert client.post("/api/nominated-vendors", headers=admin, json={
        "category": "buttons", "supplier_code": "X", "supplier_name": "X"}).status_code == 422
    assert client.post("/api/nominated-vendors", headers=qc, json={
        "category": "zip", "supplier_code": "Y", "supplier_name": "Y"}).status_code == 403
    assert code in [v["supplier_code"] for v in client.get("/api/nominated-vendors?category=zip", headers=qc).json()]
    r = client.patch(f"/api/nominated-vendors/{vid}", headers=admin, json={"supplier_name": "Zip Co Ltd"})
    assert r.json()["supplier_name"] == "Zip Co Ltd"
    assert client.delete(f"/api/nominated-vendors/{vid}", headers=admin).status_code == 204
    assert code not in [v["supplier_code"] for v in client.get("/api/nominated-vendors", headers=qc).json()]


# ---------- #6 PO edit ----------

def test_po_edit_rules(client, admin):
    code = _uid("V")
    po = _new_po(client, admin, code)
    r = client.patch(f"/api/pos/{po['id']}", headers=admin, json={
        "packing_norms": {"pcs_per_inner": 6}, "weight_tolerance_g": 12, "size_chart": {"M": {"chest_cm": [52, 1]}}})
    assert r.status_code == 200, r.text
    assert r.json()["packing_norms"] == {"pcs_per_inner": 6} and r.json()["weight_tolerance_g"] == 12
    assert client.patch(f"/api/pos/{po['id']}", headers=admin, json={"po_number": "X"}).status_code == 422
    assert client.patch(f"/api/pos/{po['id']}", headers=admin, json={"packing_norms": {"x": -1}}).status_code == 422
    assert client.patch(f"/api/pos/{po['id']}", headers=admin, json={"article_number": None}).status_code == 422

    _, vendor = _new_user(client, admin, vendor_code=code)
    assert client.patch(f"/api/pos/{po['id']}", headers=vendor, json={"mc": "X"}).status_code == 403
    client.post("/api/inspections", headers=vendor, json={"po_id": po["id"], "color_name": "NAVY BLUE", "size": "M"})
    assert client.patch(f"/api/pos/{po['id']}", headers=admin, json={"vendor_code": "OTHER"}).status_code == 409
    assert client.patch(f"/api/pos/{po['id']}", headers=admin, json={"sizes": ["S"]}).status_code == 409
    assert client.patch(f"/api/pos/{po['id']}", headers=admin, json={"sizes": ["S", "M", "L"]}).status_code == 200


# ---------- #6 references ----------

def test_reference_kind_view_and_delete(client, admin, qc):
    code = _uid("V")
    po = _new_po(client, admin, code)
    url = f"/api/pos/{po['id']}/references"
    img = textured_jpeg()
    assert client.post(url, headers=qc, data={"kind": "main-label"},
                       files={"file": ("a.jpg", img, "image/jpeg")}).status_code == 422
    assert client.post(url, headers=qc, data={"kind": "pps"},
                       files={"file": ("a.jpg", b"not an image", "image/jpeg")}).status_code == 415
    r = client.post(url, headers=qc, data={"kind": "pps", "color_name": " "},
                    files={"file": ("../../evil.php", img, "image/jpeg")})
    assert r.status_code == 201 and r.json()["color_name"] is None
    ref_id = r.json()["id"]

    got = client.get(f"{url}/{ref_id}/file", headers=qc)
    assert got.status_code == 200 and got.content == img
    _, other_vendor = _new_user(client, admin, vendor_code=_uid("V"))
    assert client.get(f"{url}/{ref_id}/file", headers=other_vendor).status_code == 404

    assert client.delete(f"{url}/{ref_id}", headers=qc).status_code == 204
    assert ref_id not in [x["id"] for x in client.get(url, headers=qc).json()]
    assert client.get(f"{url}/{ref_id}/file", headers=qc).status_code == 404
