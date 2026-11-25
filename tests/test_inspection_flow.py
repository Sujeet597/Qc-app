import json

from conftest import flat_jpeg, login, textured_jpeg


def _evidence(client, h, insp_id, no, img, **form):
    r = client.post(f"/api/inspections/{insp_id}/checks/{no}/evidence", headers=h,
                    files={"frame": ("f.jpg", img, "image/jpeg")}, data=form)
    assert r.status_code == 200, r.text
    return r.json()


def test_full_inspection_flow(client):
    vendor = login(client, "vendor1", "vendor123")
    qc = login(client, "qc", "qc123")
    approver = login(client, "approver", "approver123")

    pos = client.get("/api/pos", headers=vendor).json()
    assert [p["po_number"] for p in pos] == ["PO-1001"]
    po = pos[0]

    # QC uploads the approved PPS reference
    r = client.post(f"/api/pos/{po['id']}/references", headers=qc, data={"kind": "pps", "color_name": "NAVY BLUE"},
                    files={"file": ("pps.jpg", textured_jpeg(seed=1), "image/jpeg")})
    assert r.status_code == 201, r.text

    r = client.post("/api/inspections", headers=vendor, json={"po_id": po["id"], "color_name": "NAVY BLUE", "size": "M"})
    assert r.status_code == 201, r.text
    insp = r.json()
    iid = insp["id"]
    assert insp["progress"] == "0 of 20 checks completed"

    # 1. color: same color as PPS -> pass; very different color -> fail
    assert _evidence(client, vendor, iid, 1, textured_jpeg(seed=2))["status"] == "pass"
    assert _evidence(client, vendor, iid, 1, textured_jpeg(rgb=(200, 40, 40), seed=3))["status"] == "fail"
    assert _evidence(client, vendor, iid, 1, textured_jpeg(seed=4))["status"] == "pass"

    # blurry / flat frame -> reshow (manual review), never pass
    c = _evidence(client, vendor, iid, 12, flat_jpeg())
    assert c["status"] == "manual_review" and "Reshow" in c["reason"]

    # 6. barcode: browser-decoded codes match PO -> pass; wrong code -> fail
    assert _evidence(client, vendor, iid, 6, textured_jpeg(), codes="111")["status"] == "fail"
    assert _evidence(client, vendor, iid, 6, textured_jpeg(), codes="8901234567890,TF-7788-M")["status"] == "pass"

    # 8 weight / 9 measurements / 10 GSM against PO tolerances
    assert _evidence(client, vendor, iid, 8, textured_jpeg(), measured=json.dumps({"weight_g": 214}))["status"] == "pass"
    assert _evidence(client, vendor, iid, 9, textured_jpeg(), measured=json.dumps({"chest_cm": 55, "length_cm": 70}))["status"] == "fail"
    assert _evidence(client, vendor, iid, 10, textured_jpeg(), measured="{}")["status"] == "manual_review"

    # 17 carton vendor: not nominated -> fail
    assert _evidence(client, vendor, iid, 17, textured_jpeg(), supplier_code="UNKNOWN")["status"] == "fail"

    # submitting with pending checks is refused
    assert client.post(f"/api/inspections/{iid}/submit", headers=vendor).status_code == 422

    # with the mock AI, every remaining visual check goes to manual review
    for no in range(1, 21):
        if client.get(f"/api/inspections/{iid}", headers=vendor).json()["checks"][no - 1]["status"] == "pending":
            assert _evidence(client, vendor, iid, no, textured_jpeg())["status"] == "manual_review"

    r = client.post(f"/api/inspections/{iid}/submit", headers=vendor)
    assert r.status_code == 200 and r.json()["status"] == "submitted"

    # approver cannot pass while manual reviews are open
    r = client.post(f"/api/inspections/{iid}/decision", headers=approver, json={"decision": "passed"})
    assert r.status_code == 422

    # QC resolves every manual review
    for c in client.get(f"/api/inspections/{iid}", headers=qc).json()["checks"]:
        if c["status"] == "manual_review":
            r = client.post(f"/api/inspections/{iid}/checks/{c['check_no']}/review", headers=qc,
                            json={"status": "pass", "comment": "Verified on call"})
            assert r.status_code == 200
    detail = client.get(f"/api/inspections/{iid}", headers=qc).json()
    assert detail["summary"] == "needs_qc_review"  # checks 9 and 17 still failed (non-critical)

    # vendor cannot take the final decision
    assert client.post(f"/api/inspections/{iid}/decision", headers=vendor, json={"decision": "passed"}).status_code == 403

    r = client.post(f"/api/inspections/{iid}/decision", headers=approver,
                    json={"decision": "reinspection_required", "note": "Fix chest measurement and carton supplier"})
    assert r.status_code == 200 and r.json()["status"] == "reinspection_required"

    r = client.get(f"/api/inspections/{iid}/report.xlsx", headers=qc)
    assert r.status_code == 200 and r.content[:2] == b"PK"

    # reinspection links to the previous inspection
    r = client.post("/api/inspections", headers=vendor, json={"po_id": po["id"], "color_name": "NAVY BLUE", "size": "M"})
    assert r.status_code == 201 and r.json()["parent_inspection_id"] == iid

    assert client.get("/api/dashboard/summary", headers=qc).json()["total_inspections"] >= 2


def test_critical_failure_blocks_approval(client):
    vendor = login(client, "vendor1", "vendor123")
    qc = login(client, "qc", "qc123")
    approver = login(client, "approver", "approver123")
    po = client.get("/api/pos", headers=vendor).json()[0]
    iid = client.post("/api/inspections", headers=vendor, json={"po_id": po["id"], "color_name": "NAVY BLUE", "size": "S"}).json()["id"]
    for no in range(1, 21):
        _evidence(client, vendor, iid, no, textured_jpeg(), codes="999" if no == 6 else "")
    for c in client.get(f"/api/inspections/{iid}", headers=qc).json()["checks"]:
        if c["status"] == "manual_review":
            client.post(f"/api/inspections/{iid}/checks/{c['check_no']}/review", headers=qc, json={"status": "pass"})
    client.post(f"/api/inspections/{iid}/submit", headers=vendor)
    # check 6 (critical) failed -> blocked without exception
    r = client.post(f"/api/inspections/{iid}/decision", headers=approver, json={"decision": "passed"})
    assert r.status_code == 422 and "Critical" in r.json()["detail"]
    r = client.post(f"/api/inspections/{iid}/decision", headers=approver,
                    json={"decision": "passed", "record_exception": True, "note": "Label reprint approved by merchandiser"})
    assert r.status_code == 200


def test_vendor_isolation(client):
    vendor = login(client, "vendor1", "vendor123")
    admin = login(client, "admin", "admin123")
    other = client.post("/api/pos", headers=admin, json={
        "po_number": "PO-2002", "vendor_code": "V999", "article_number": "A", "style_number": "S", "color_name": "RED",
        "mc": "10", "barcode_number": "1", "taffeta_number": "2", "sizes": ["M"]}).json()
    assert client.get(f"/api/pos/{other['id']}", headers=vendor).status_code == 404
