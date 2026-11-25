# Product Blueprint – AI-Based Garment QC Video Call Application

Source: *AI_Based_Garment_QC_Video_Call_Application_BRD v1.0*. Backend: **Python FastAPI**.

---

## 1. System architecture

```
┌──────────────────────────── Vendor (phone / laptop browser) ─────────────────────────────┐
│  SRM portal page (React/Next.js)                                                         │
│   • LiveKit client – publishes camera                                                    │
│   • zxing-wasm – decodes barcodes live from the video                                    │
│   • "Capture" → grabs current video frame (canvas.toBlob) → POST evidence                │
│   • Shows current checklist point, instruction, progress "8 of 20", Pass/Fail/Review     │
└───────────────┬───────────────────────────────────────────────┬──────────────────────────┘
                │ WebRTC media                                  │ HTTPS (JWT)
                ▼                                               ▼
     ┌────────────────────┐                       ┌──────────────────────────────────────┐
     │ LiveKit (cloud or  │◄── room token ────────│  FastAPI backend  (this repo)        │
     │ self-hosted SFU)   │                       │   api/       auth, POs, inspections, │
     └─────────┬──────────┘                       │              dashboard, report       │
               │ subscribe                        │   services/  verification engine     │
               ▼                                  │     ├─ imaging   (quality gate, ΔE)  │
   QC team / Approver browser                     │     ├─ barcode   (zxing-cpp, opt.)   │
   (watches live, guides vendor,                  │     ├─ ai_vision (Claude / mock)     │
    resolves Manual Review, decides)              │     ├─ decision  (BRD §7 rules)      │
                                                  │     └─ report    (Excel)             │
                                                  └───────┬───────────────┬──────────────┘
                                                          │               │
                                                 PostgreSQL / SQLite   File storage
                                                 (POs, inspections,    (references, evidence
                                                  results, audit)       frames → S3/Azure later)
                                                          ▲
                                       SRM / ERP PO sync job (Phase 3)
```

### 1a. Live QC agent (server watches the video itself)

```
Vendor camera ──WebRTC──► LiveKit room "qc-{inspection_id}" ◄── QC team watches
                                   │
                                   ▼  (hidden participant "qc-agent-{id}")
                  app/agent/livekit_worker.py
                   • subscribes to the vendor-* camera track, samples 3 fps
                   • FrameSampler: keeps last 60 frames + focus score, reads barcodes live
                   • InspectionController, per pending check:
                       instruction ─► (wait for typed reading if needed) ─► settle 3 s
                       ─► watch 4 s ─► pick sharpest well-lit frame ─► verify (same engine as API)
                       ─► "reshow" and retry (max 3) if blurry / unreadable ─► announce result
                   • talks to the vendor/QC page over the LiveKit data channel, topic "qc"
```

| Direction | Message (JSON) |
|---|---|
| agent → room | `agent_ready`, `instruction {check_no,title,instruction,attempt,input_fields}`, `waiting_for_input`, `reshow {reason}`, `result {status,reason,observed,expected,confidence,progress}`, `skipped`, `no_video`, `done {pending_checks}`, `error` |
| room → agent | `readings {check_no, measured:{...}}` or `{check_no, supplier_code}`, `codes {check_no, codes[]}`, `goto {check_no}`, `skip`, `capture` (take it now), `stop` |

Start it with `POST /api/inspections/{id}/agent` (dev: runs as a subprocess) or
`python -m app.agent.livekit_worker --inspection {id}` (production: its own service / container).
Because the server takes the frames itself, a vendor cannot upload an old or edited photo:
every piece of evidence is stored with `source: "live_agent"`.

**Key design rule:** the AI only *looks and reads* (label text, visual match, defects). Python code makes the
Pass/Fail decision against PO data and thresholds, so every result is explainable and auditable.
Unclear evidence, low confidence, a blurry frame or a missing reference → **Manual Review, never Pass** (BRD §3 note).

## 2. BRD module → code map

| BRD module (§6)       | Where in code |
|-----------------------|---------------|
| Vendor login          | `app/api/auth.py`, `app/api/deps.py` (JWT, roles, vendor sees only own POs) |
| Reference master      | `app/api/purchase_orders.py`, models `PurchaseOrder`, `ReferenceAsset`, `NominatedVendor` |
| Video inspection      | `app/services/video.py` (LiveKit token), `POST /inspections/{id}/video-token` |
| AI verification       | `app/services/verification.py`, `ai_vision.py`, `imaging.py`, `barcode.py` |
| QC decision engine    | `app/services/decision.py` |
| Evidence management   | model `Evidence`, `app/services/storage.py` |
| Approval workflow     | `/checks/{no}/review`, `/submit`, `/decision` in `app/api/inspections.py` |
| Reports & dashboard   | `app/services/report.py` (Excel), `app/api/dashboard.py` |

## 3. Checklist → verification method (`app/checklist.py`)

| # | Check | Method | How it decides | Critical |
|---|-------|--------|----------------|:-:|
| 1 | Color vs PPS | `color_compare` | ΔE (CIE76) vs PPS image: ≤3 pass, >6 fail, between → review | ✔ |
| 2 | Barcode color name vs PO | `ocr_match` | AI reads text → code compares with `po.color_name` | ✔ |
| 3 | MC vs PO | `ocr_match` | AI reads → compare `po.mc` | |
| 4 | Main label | `visual_compare` | AI: frame vs `main_label` reference | |
| 5 | Size label | `ocr_match` | AI reads → compare inspection size | |
| 6 | Barcode / taffeta | `barcode_match` | decoded codes == `po.barcode_number` + `po.taffeta_number` (AI OCR fallback for taffeta) | ✔ |
| 7 | Trims | `visual_compare` | AI vs sealer | |
| 8 | Weight | `measurement` | entered scale reading vs target ± tolerance | |
| 9 | Measurements | `measurement` | each point of measure vs size chart ± tolerance | |
| 10 | GSM | `measurement` | GSM reading vs costing ± tolerance | |
| 11 | Print | `visual_compare` | AI vs sealer | ✔ |
| 12 | Fabric | `visual_compare` | AI vs sealer | |
| 13 | Embroidery / appliqué | `visual_compare` | AI vs sealer | |
| 14 | Packing list | `document_reconcile` | AI reads list → PO, article, color must appear | |
| 15 | Inner pack | `visual_compare` | AI vs `packing` reference | |
| 16 | Ironing | `defect_scan` | AI looks for creases / shine / burns | |
| 17–20 | Carton / pocketing / zip / poly bag vendor | `vendor_master` | supplier code ∈ nominated vendor master, + AI visual quality | |

Every AI-based check first passes the **frame quality gate** (sharpness + brightness). A bad frame returns
"Reshow item: …" so the vendor re-shows it (BRD §5 warning requirement).

## 4. Data model (`app/models.py`)

```
User(role: vendor|qc|approver|admin, vendor_code)
PurchaseOrder(po_number, asn, vendor_code, article, style, color, mc, barcode, taffeta,
              sizes[], weight±tol, gsm±tol, size_chart{size:{pom:[target,tol]}}, packing_norms)
 └─ ReferenceAsset(kind: pps|sealer|main_label|packing…, color, size, file)
NominatedVendor(category, supplier_code, supplier_name)
Inspection(po, vendor_user, color, size, status, parent_inspection_id → reinspection history,
           video_room, submitted/decided at/by, decision_note, exception_recorded)
 └─ CheckResult × 20 (status, ai_status, confidence, expected, observed, reason,
                      reviewer_comment, corrective_action)
      └─ Evidence (frame file, captured_at, extracted JSON: codes / ΔE / AI raw output)
```

## 5. Status flows (BRD §7)

```
Check:       pending → pass | fail | manual_review  → (QC review) pass | fail
Inspection:  in_progress → submitted → passed | failed | reinspection_required
                                            │
               new inspection, parent_inspection_id = previous  ◄──┘
```
- Cannot submit while any check is pending.
- Approver cannot pass while any Manual Review is open.
- A failed **critical** check blocks "passed" unless `record_exception=true` with a note.
- QC cannot mark a check Pass without evidence. The original system result stays in `ai_status` for audit.

## 6. API (open `/docs` for the live Swagger UI)

| Method | Path | Who |
|---|---|---|
| POST | `/api/auth/login` | all |
| GET | `/api/pos`, `/api/pos/{id}` | all (vendor: own only) |
| POST | `/api/pos` | admin (later: ERP sync) |
| POST/GET | `/api/pos/{id}/references` | QC/admin upload, all read |
| GET | `/api/checklist` | all |
| POST | `/api/inspections` | vendor – start (PO, color, size) |
| GET | `/api/inspections`, `/api/inspections/{id}` | all |
| POST | `/api/inspections/{id}/video-token` | all – LiveKit join token |
| POST / GET | `/api/inspections/{id}/agent` | vendor/QC – start / status of the live QC agent |
| POST | `/api/inspections/{id}/checks/{no}/evidence` | vendor/QC – frame + codes/measured/supplier |
| POST | `/api/inspections/{id}/submit` | vendor |
| POST | `/api/inspections/{id}/checks/{no}/review` | QC/approver |
| POST | `/api/inspections/{id}/decision` | approver |
| GET | `/api/inspections/{id}/report.xlsx` | all |
| GET | `/api/dashboard/summary` | QC/approver/admin |

## 7. Frontend (to build next)

React (Next.js) page embedded in the SRM portal:
1. **Start screen:** PO/ASN → article, color, size → `POST /inspections`.
2. **Inspection screen:** LiveKit `<VideoConference>` using `/video-token`; side panel with 20 checks,
   current instruction, progress bar, result chips.
3. **Agent mode (default):** call `POST /agent`, then listen to `room.on('dataReceived')` on topic `qc`:
   show each `instruction`, flash `reshow`, update the checklist on `result`. Buttons send `capture`, `skip`, `goto`.
4. Inputs for scale / tape / GSM readings and supplier code on checks 8–10 and 17–20 → send a `readings` message.
5. **Fallback manual capture:** draw the `<video>` onto a `<canvas>`, `toBlob('image/jpeg')`, POST to `/evidence`.
6. **QC console:** list of submitted inspections, evidence thumbnails, Pass/Fail/Review buttons, final decision.

## 8. Phases (BRD §9) and status in this repo

| Phase | Scope | Status |
|---|---|---|
| 1 | Digital checklist, PO data, video token, evidence capture, manual Y/N, report | **Backend done** (frontend pending; PO data entered by API, ERP sync pending) |
| 2 | Barcode / OCR, color, label, print/embroidery, sealer comparison | **Backend done** (set `AI_PROVIDER=claude`; tune on real garments) |
| 3 | Scale / measuring devices, GSM records, vendor master, packing data | Manual entry done; device integration pending |
| 4 | Confidence-based automation, dashboards, vendor performance | Basic dashboard done; analytics pending |

## 9. Before go-live

- **Color:** require a printed color reference card + standard lamp (D65) in every color frame; calibrate ΔE limits on real PPS vs bulk.
- **AI threshold:** run 100+ past inspections through it, compare with QC's decisions, then set `AI_CONFIDENCE_THRESHOLD`.
- **Security:** change `JWT_SECRET`, restrict CORS to the SRM domain, use SSO from the SRM portal, store files in S3/Azure with signed URLs.
- **DB:** move to PostgreSQL and add Alembic migrations.
- **PDF report:** Excel is implemented; add PDF (e.g. WeasyPrint/ReportLab) if needed.
- **Open BRD question (§11):** one garment per PO/article/color/size, or several random garments per call? If several, add a `sample_no` to `CheckResult` and an AQL sample-size table.
