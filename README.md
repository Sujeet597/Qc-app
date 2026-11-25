# Garment QC Video Inspection – FastAPI backend

See **BLUEPRINT.md** for the full product design.

## Run

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env          # then edit
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000/docs → **Authorize** with a demo user:

| user | password | role |
|---|---|---|
| vendor1 | vendor123 | vendor (V001) |
| qc | qc123 | QC team |
| approver | approver123 | QC approver |
| admin | admin123 | admin |

Demo PO `PO-1001` (NAVY BLUE, sizes S/M/L) is created on first start.

## Test the web page

Open **http://127.0.0.1:8000/** (redirects to `/app/`).

**Local test mode (works now, no video account needed)**
Login is **off by default** (`AUTH_ENABLED=false`): no sign-in screen, use the **Acting as** dropdown
at the top to switch between Vendor / QC team / QC approver / Admin. Turn login on with `AUTH_ENABLED=true`
before anyone else uses it – with it off, anyone who can open the page can act as the approver.

1. Acting as **Vendor** → pick size → **Start video inspection** → allow the camera.
2. The banner tells you what to show. Hold the item steady and press **Capture now**.
   Blurry or dark frames come back as "Reshow item…" — the page stays on that check.
3. Checks 8, 9, 10 and 17–20 ask for a reading (scale, tape, GSM, supplier) — type it, then Capture.
4. Tap any check to jump to it. When all 20 are done → **Submit inspection**.
5. Switch to **QC team** → open the inspection → tap a check → Pass / Fail / Review.
   QC can also upload the PPS / sealer reference photos here (needed for color & visual checks).
6. Switch to **QC approver** → open the submitted inspection → final decision → Excel report.

**Live mode (real video call + AI agent)** — set the three `LIVEKIT_*` values in `.env` and restart.
The vendor's page then joins the call, the server agent joins hidden, gives instructions and verifies by itself;
QC opens the same inspection ("Join live") to watch, talk, jump/skip checks.

**Testing from a phone:** browsers only allow the camera on `https://` (or `localhost`).
Run the server with `--host 0.0.0.0` and expose it over HTTPS, e.g. `cloudflared tunnel --url http://localhost:8000`,
then open the https link on the phone.

## Turn on AI

In `.env`: `AI_PROVIDER=claude` and `ANTHROPIC_API_KEY=...`. With `mock`, every visual check goes to Manual Review.

## Turn on video

Create a LiveKit project (cloud.livekit.io) and set `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`.

## Live QC agent (watches the video and verifies by itself)

1. Vendor starts an inspection and joins the call with the token from `POST /api/inspections/{id}/video-token`.
2. Start the agent: `POST /api/inspections/{id}/agent`, or run it yourself:
   ```powershell
   .\.venv\Scripts\python.exe -m app.agent.livekit_worker --inspection 1
   ```
3. The agent joins hidden, tells the vendor what to show (data channel topic `qc`), picks the sharpest frame,
   verifies it, asks to reshow blurry items, and saves every result + frame as evidence.

Message format: see BLUEPRINT.md section 1a.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```
"# Qc-app" 
