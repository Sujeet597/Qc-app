# Runs last (file name order): it adds a V001 PO, and earlier tests expect PO-1001 to be the only one.
from conftest import login, textured_jpeg


def test_qc_can_view_vendor_capture(client):
    vendor = login(client, "vendor1", "vendor123")
    qc = login(client, "qc", "qc123")
    admin = login(client, "admin", "admin123")
    # own PO so this test doesn't collide with inspections opened by other tests
    po = client.post("/api/pos", headers=admin, json={
        "po_number": "PO-EV-1", "vendor_code": "V001", "article_number": "A", "style_number": "S", "color_name": "NAVY BLUE",
        "mc": "10", "barcode_number": "1", "taffeta_number": "2", "sizes": ["M"]}).json()
    iid = client.post("/api/inspections", headers=vendor, json={"po_id": po["id"], "color_name": "NAVY BLUE", "size": "M"}).json()["id"]
    img = textured_jpeg(seed=7)
    r = client.post(f"/api/inspections/{iid}/checks/2/evidence", headers=vendor, files={"frame": ("f.jpg", img, "image/jpeg")})
    assert r.status_code == 200, r.text

    chk = next(c for c in client.get(f"/api/inspections/{iid}", headers=qc).json()["checks"] if c["check_no"] == 2)
    assert len(chk["evidence_ids"]) == 1
    ev = chk["evidence_ids"][0]
    r = client.get(f"/api/inspections/{iid}/evidence/{ev}", headers=qc)
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and len(r.content) > 0
    # evidence id must belong to the inspection in the URL
    assert client.get(f"/api/inspections/{iid + 999}/evidence/{ev}", headers=qc).status_code == 404
