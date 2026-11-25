"use strict";
/* Garment QC – vendor / QC / approver page.
   Live mode : LiveKit call, the server QC agent watches the vendor camera and verifies (data topic "qc").
   Local mode: no LiveKit configured – the page captures frames from this device's camera and uploads them. */

const $ = (s) => document.querySelector(s);
const S = {
  authEnabled: true, token: null, role: null, user: null,
  insp: null, mode: null, room: null, stream: null, current: null,
  facing: "environment", readings: {}, poll: null,
};
const STATUS_LABEL = { pending: "Pending", pass: "Pass", fail: "Fail", manual_review: "Review" };
const FIELD_LABEL = { weight_g: "Weight (g)", gsm: "GSM", supplier_code: "Supplier code" };

/* ---------------- helpers ---------------- */
function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c !== null && c !== undefined) e.append(c instanceof Node ? c : String(c));
  return e;
}
function toast(msg, ms = 3500) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), ms);
}
function log(msg) {
  const li = el("li", {}, `${new Date().toLocaleTimeString()}  ${msg}`);
  $("#log").prepend(li);
  while ($("#log").children.length > 60) $("#log").lastChild.remove();
}
function flash(text, kind = "info", ms = 4000) {
  const f = $("#flash"); f.textContent = text; f.className = `flash ${kind}`; f.hidden = false;
  clearTimeout(flash._t); flash._t = setTimeout(() => (f.hidden = true), ms);
}
function show(view) {
  for (const v of document.querySelectorAll(".view")) v.hidden = v.id !== view;
}
async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (S.authEnabled && S.token) headers.Authorization = `Bearer ${S.token}`;
  if (!S.authEnabled) headers["X-Role"] = S.role || "vendor";
  let body = opts.body;
  if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; body = JSON.stringify(opts.json); }
  const r = await fetch(`/api${path}`, { method: opts.method || "GET", headers, body });
  if (r.status === 401 && S.authEnabled && S.token) { logout(); throw new Error("Session expired – sign in again"); }
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch {}
    const e = new Error(msg); e.status = r.status; throw e;
  }
  return (r.headers.get("content-type") || "").includes("json") ? r.json() : r;
}
const isVendor = () => S.role === "vendor";
const isReviewer = () => S.role === "qc" || S.role === "approver";

/* ---------------- auth ---------------- */
$("#login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const fd = new FormData(ev.target);
  try {
    const r = await fetch("/api/auth/login", { method: "POST", body: new URLSearchParams(fd) });
    if (!r.ok) throw new Error("Wrong username or password");
    const j = await r.json();
    S.token = j.access_token;
    sessionStorage.setItem("qc_token", S.token);
    await afterLogin();
  } catch (e) { toast(e.message); }
});
async function afterLogin() {
  const me = await api("/auth/me");
  S.user = me; S.role = me.role;
  $("#who-name").textContent = S.authEnabled ? me.username : "";
  $("#who-role").textContent = me.role;
  $("#who-role").hidden = !S.authEnabled;
  $("#role-switch").hidden = S.authEnabled;
  $("#btn-logout").hidden = !S.authEnabled;
  $("#whoami").hidden = false;
  await goHome();
}
$("#role-picker").onchange = async (ev) => {
  S.role = ev.target.value;
  try { localStorage.setItem("qc_role", S.role); } catch {}
  await afterLogin();
};
function logout() {
  leaveInspection();
  S.token = null; sessionStorage.removeItem("qc_token");
  $("#toast").hidden = true;
  $("#whoami").hidden = true; show("view-login");
}
$("#btn-logout").onclick = logout;
$("#btn-home").onclick = () => goHome();

/* ---------------- home ---------------- */
async function goHome() {
  leaveInspection();
  show("view-home");
  $("#vendor-home").hidden = !isVendor();
  $("#qc-filters").hidden = isVendor();
  $("#insp-list-title").textContent = isVendor() ? "My inspections" : "Inspections";
  if (isVendor()) await renderPOs();
  await renderInspections();
}
async function renderPOs() {
  const pos = await api("/pos");
  const box = $("#po-list"); box.replaceChildren();
  if (!pos.length) box.append(el("p", { class: "muted" }, "No purchase orders assigned to you."));
  for (const po of pos) {
    const size = el("select", {}, po.sizes.map((s) => el("option", { value: s }, s)));
    const start = el("button", { class: "primary", onclick: async () => {
      try {
        const insp = await api("/inspections", { method: "POST", json: { po_id: po.id, color_name: po.color_name, size: size.value } });
        openInspection(insp.id);
      } catch (e) { toast(e.message); }
    } }, "Start video inspection");
    box.append(el("div", { class: "card po-card" },
      el("div", {}, el("b", {}, po.po_number), "  ", el("span", { class: "muted" }, po.asn_number || "")),
      el("div", { class: "small" }, `Article ${po.article_number} · ${po.color_name} · Qty ${po.order_qty}`),
      el("div", { class: "row" }, el("span", { class: "small muted" }, "Size"), size, start)));
  }
}
async function renderInspections() {
  const status = isVendor() ? "" : $("#status-filter").value;
  const list = await api(`/inspections${status ? `?status=${encodeURIComponent(status)}` : ""}`);
  const box = $("#insp-list"); box.replaceChildren();
  if (!list.length) box.append(el("p", { class: "muted" }, "No inspections yet."));
  for (const i of list) {
    const label = i.status === "in_progress" ? (isVendor() ? "Resume" : "Join live") : "Open";
    box.append(el("div", { class: "card list-item" },
      el("div", {},
        el("b", {}, `#${i.id} · ${i.po_number}`), " ",
        el("span", { class: "small muted" }, `${i.article_number} · ${i.color_name} · ${i.size} · ${i.progress}`)),
      el("div", { class: "row" },
        el("span", { class: `chip ${i.status}` }, i.status.replaceAll("_", " ")),
        el("button", { class: i.status === "in_progress" ? "primary" : "ghost", onclick: () => openInspection(i.id) }, label))));
  }
}
$("#status-filter").onchange = renderInspections;

/* ---------------- inspection screen ---------------- */
async function openInspection(id) {
  leaveInspection();
  show("view-insp");
  $("#log").replaceChildren();
  $("#checklist").replaceChildren();   // don't carry expanded rows over from another inspection / role
  S.readings = {}; S.current = null;
  await refresh(id);
  const live = S.insp.status === "in_progress";
  $("#vendor-controls").hidden = !live || !(isVendor() || S.role === "qc");
  $("#btn-camera").hidden = !isVendor();
  $("#ref-upload").hidden = !(S.role === "qc" || S.role === "admin");
  if (live) await connectVideo();
  else { S.mode = "closed"; $("#mode-chip").textContent = "Inspection closed"; $("#video-placeholder").textContent = "Inspection closed – see checklist and report"; }
  S.poll = setInterval(() => refresh().catch(() => {}), 5000);
}
function leaveInspection() {
  clearInterval(S.poll); S.poll = null;
  if (S.room) { S.room.disconnect(); S.room = null; }
  if (S.stream) { S.stream.getTracks().forEach((t) => t.stop()); S.stream = null; }
  const v = $("#cam"); if (v) v.srcObject = null;
  $("#banner").hidden = true; $("#readings-form").hidden = true; $("#flash").hidden = true;
  $("#video-placeholder").hidden = false; $("#video-placeholder").textContent = "Camera off";
  S.insp = null; S.mode = null;
}
async function refresh(id) {
  S.insp = await api(`/inspections/${id || S.insp.id}`);
  renderInspection();
}
function renderInspection() {
  const i = S.insp;
  const meta = $("#insp-meta"); meta.replaceChildren();
  for (const [k, v] of [["Inspection", `#${i.id}`], ["PO", i.po_number], ["ASN", i.asn_number || "-"], ["Article", i.article_number],
    ["Color", i.color_name], ["Size", i.size], ["Vendor", i.vendor_code], ["Type", i.inspection_type.replaceAll("_", " ")]]) {
    meta.append(el("span", {}, el("span", { class: "muted" }, `${k} `), el("b", {}, v)));
  }
  const done = i.checks.filter((c) => c.status !== "pending").length;
  $("#progress-bar").style.width = `${(done / i.checks.length) * 100}%`;
  $("#progress-text").textContent = `${done} of ${i.checks.length} checks completed`;
  const sc = $("#status-chip"); sc.className = `chip ${i.status}`; sc.textContent = i.status.replaceAll("_", " ");

  const ol = $("#checklist");
  const open = new Set([...ol.querySelectorAll(".check.open")].map((x) => +x.dataset.no));
  ol.replaceChildren();
  for (const c of i.checks) ol.append(renderCheck(c, open.has(c.check_no)));

  const allDone = i.checks.every((c) => c.status !== "pending");
  $("#btn-submit").hidden = !(isVendor() && i.status === "in_progress");
  $("#btn-submit").disabled = !allDone;
  $("#btn-submit").textContent = allDone ? "Submit inspection" : `Submit inspection (${i.checks.length - done} checks left)`;
  $("#decision").hidden = !(S.role === "approver" && i.status === "submitted");

  if (S.mode === "local" && isVendor() && S.current === null) selectCheck(firstPending());
}
function renderCheck(c, isOpen) {
  const li = el("li", { class: `check${c.check_no === S.current ? " current" : ""}${isOpen ? " open" : ""}`, "data-no": c.check_no });
  li.append(el("div", { class: "check-top" },
    el("span", { class: "check-no" }, c.check_no),
    el("span", { class: "check-title" }, c.title, c.critical ? el("span", { class: "crit" }, "  CRITICAL") : null),
    el("span", { class: `chip ${c.status}` }, STATUS_LABEL[c.status] || c.status)));
  const d = el("div", { class: "check-detail" },
    el("div", {}, el("b", {}, "Show: "), c.instruction),
    c.expected_value ? el("div", {}, el("b", {}, "Expected: "), c.expected_value) : null,
    c.observed_value ? el("div", {}, el("b", {}, "Observed: "), c.observed_value) : null,
    c.reason ? el("div", {}, el("b", {}, "Reason: "), c.reason) : null,
    c.confidence !== null && c.confidence !== undefined ? el("div", {}, el("b", {}, "Confidence: "), `${Math.round(c.confidence * 100)}%`) : null,
    el("div", {}, el("b", {}, "Evidence: "), `${c.evidence_count} frame(s)`),
    (c.evidence_ids || []).length ? evidenceStrip(c.evidence_ids) : null,
    c.ai_status && c.ai_status !== c.status ? el("div", {}, el("b", {}, "System result: "), STATUS_LABEL[c.ai_status]) : null,
    c.reviewer_comment ? el("div", {}, el("b", {}, "QC comment: "), c.reviewer_comment) : null,
    c.corrective_action ? el("div", {}, el("b", {}, "Corrective action: "), c.corrective_action) : null);
  if (isReviewer() && ["in_progress", "submitted"].includes(S.insp.status) && c.status !== "pending") {
    const comment = el("input", { placeholder: "QC comment / corrective action" });
    const btn = (status, cls, label) => el("button", { class: cls, onclick: async (ev) => {
      ev.stopPropagation();
      try {
        await api(`/inspections/${S.insp.id}/checks/${c.check_no}/review`, { method: "POST",
          json: { status, comment: comment.value, corrective_action: status === "fail" ? comment.value : "" } });
        log(`QC marked check ${c.check_no} ${STATUS_LABEL[status]}`);
        await refresh();
      } catch (e) { toast(e.message); }
    } }, label);
    comment.addEventListener("click", (ev) => ev.stopPropagation());
    d.append(el("div", { class: "review-row" }, comment, btn("pass", "pass", "Pass"), btn("fail", "fail", "Fail"), btn("manual_review", "review", "Review")));
  }
  li.append(d);
  li.addEventListener("click", () => {
    li.classList.toggle("open");
    if (S.insp.status !== "in_progress" || !(isVendor() || S.role === "qc")) return;
    if (S.mode === "live") { sendMsg({ type: "goto", check_no: c.check_no }); log(`Asked agent to go to check ${c.check_no}`); }
    else if (S.mode === "local" && isVendor()) selectCheck(c.check_no);
  });
  return li;
}
/* captured frames: fetched with the role header / token, cached as blob URLs so polling doesn't re-download */
const thumbCache = new Map();
function evidenceStrip(ids) {
  const box = el("div", { class: "evidence-strip" });
  for (const id of ids.slice(-4)) {
    const img = el("img", { alt: `Evidence ${id}`, title: "Click to open full size" });
    img.addEventListener("click", (ev) => { ev.stopPropagation(); if (img.src) window.open(img.src, "_blank"); });
    box.append(img);
    let p = thumbCache.get(id);
    if (!p) {
      p = api(`/inspections/${S.insp.id}/evidence/${id}`).then((r) => r.blob()).then((b) => URL.createObjectURL(b));
      p.catch(() => thumbCache.delete(id));
      thumbCache.set(id, p);
    }
    p.then((url) => { img.src = url; }).catch(() => img.replaceWith(el("span", {}, "(image unavailable)")));
  }
  return box;
}
const firstPending = () => (S.insp.checks.find((c) => c.status === "pending") || {}).check_no ?? null;
const checkByNo = (no) => S.insp.checks.find((c) => c.check_no === no);

function setBanner(no, title, text) {
  $("#banner").hidden = no === null;
  $("#banner-no").textContent = no ?? "";
  $("#banner-title").textContent = title || "";
  $("#banner-text").textContent = text || "";
}
function renderReadings(no, fields) {
  const form = $("#readings-form");
  const box = $("#readings-fields"); box.replaceChildren();
  form.hidden = !fields || !fields.length || !isVendor();
  form.dataset.no = no ?? "";
  if (form.hidden) return;
  $("#readings-title").textContent = `Check ${no}: enter the reading shown on camera`;
  for (const f of fields) {
    const prev = (S.readings[no] || {})[f] ?? "";
    box.append(el("label", { class: "small" }, FIELD_LABEL[f] || f.replaceAll("_", " "),
      el("input", { name: f, value: prev, required: true, type: f === "supplier_code" ? "text" : "number", step: "any", inputmode: f === "supplier_code" ? "text" : "decimal" })));
  }
  $("#btn-readings").textContent = S.mode === "live" ? "Send reading" : "Save reading";
}
$("#readings-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const no = +ev.target.dataset.no;
  const values = Object.fromEntries(new FormData(ev.target));
  S.readings[no] = values;
  const msg = { type: "readings", check_no: no };
  if ("supplier_code" in values) msg.supplier_code = values.supplier_code.trim();
  else msg.measured = Object.fromEntries(Object.entries(values).map(([k, v]) => [k, Number(v)]));
  if (S.mode === "live") { sendMsg(msg); log(`Sent reading for check ${no}`); flash("Reading sent – keep the display on camera", "info"); }
  else { toast("Reading saved – now press Capture now"); }
});

/* ---------------- video ---------------- */
function cameraBlockedReason() {
  if (navigator.mediaDevices?.getUserMedia) return null;
  if (!window.isSecureContext)
    return `Camera is blocked because this page is opened over http://. Phones only allow the camera on https:// – open the https link instead (see README → "Testing from a phone"). Current address: ${location.origin}`;
  return "This browser does not support camera access. Use Safari (iPhone) or Chrome (Android).";
}

async function connectVideo() {
  const blocked = isVendor() ? cameraBlockedReason() : null;
  if (blocked) {
    S.mode = "closed";
    $("#mode-chip").textContent = "Camera blocked";
    $("#vendor-controls").hidden = true;
    $("#video-placeholder").hidden = false;
    $("#video-placeholder").textContent = blocked;
    toast(blocked);
    return;
  }
  let tok = null;
  try { tok = await api(`/inspections/${S.insp.id}/video-token`, { method: "POST" }); }
  catch (e) { if (e.status !== 503) { toast(e.message); } }
  if (tok && window.LivekitClient) return startLive(tok);
  return startLocal();
}

async function startLive(tok) {
  const LK = window.LivekitClient;
  S.mode = "live";
  $("#mode-chip").textContent = "Live call · AI agent";
  // adaptiveStream off: it would size the vendor video to the box on screen; QC needs full detail to inspect
  const room = new LK.Room({ adaptiveStream: false, dynacast: true });
  S.room = room;
  room.on(LK.RoomEvent.DataReceived, (payload, participant, kind, topic) => {
    if (topic !== "qc") return;
    try { onAgentMessage(JSON.parse(new TextDecoder().decode(payload)), participant); } catch {}
  });
  room.on(LK.RoomEvent.TrackSubscribed, (track, pub, participant) => {
    if (track.kind === "video" && participant.identity.startsWith("vendor-") && !isVendor()) {
      pub.setVideoQuality(LK.VideoQuality.HIGH);
      track.attach($("#cam")); $("#video-placeholder").hidden = true;
    } else if (track.kind === "audio") {
      const a = track.attach(); a.hidden = true; document.body.append(a);
    }
  });
  room.on(LK.RoomEvent.LocalTrackPublished, (pub) => {
    if (pub.track && pub.track.kind === "video") { pub.track.attach($("#cam")); $("#video-placeholder").hidden = true; }
  });
  room.on(LK.RoomEvent.ParticipantConnected, (p) => log(`${p.name || p.identity} joined the call`));
  room.on(LK.RoomEvent.ParticipantDisconnected, (p) => log(`${p.name || p.identity} left the call`));
  room.on(LK.RoomEvent.Disconnected, () => log("Disconnected from the call"));
  try {
    await room.connect(tok.url, tok.token);
    log(`Joined video call ${tok.room}`);
    if (isVendor()) {
      await room.localParticipant.setCameraEnabled(true, { facingMode: S.facing, resolution: { width: 1920, height: 1080, frameRate: 30 } });
    } else {
      $("#video-placeholder").textContent = "Waiting for vendor camera…";
    }
    await room.localParticipant.setMicrophoneEnabled(true).catch(() => log("Microphone not available"));
  } catch (e) {
    toast(`Video call failed: ${e.message}`);
    log(`Video call failed: ${e.message}`);
    return;
  }
  if (isVendor() || S.role === "qc") {
    try {
      const r = await api(`/inspections/${S.insp.id}/agent`, { method: "POST" });
      log(r.status === "already_running" ? "AI inspector already in the call" : "AI inspector is joining…");
    } catch (e) { toast(`Agent: ${e.message}`); }
  }
}
function sendMsg(obj) {
  if (!S.room) return;
  S.room.localParticipant.publishData(new TextEncoder().encode(JSON.stringify(obj)), { reliable: true, topic: "qc" })
    .catch((e) => toast(`Send failed: ${e.message}`));
}
function onAgentMessage(m, participant) {
  switch (m.type) {
    case "agent_ready": log("AI inspector joined – follow the instructions on screen"); break;
    case "instruction":
      S.current = m.check_no;
      setBanner(m.check_no, m.title, m.instruction + (m.attempt > 1 ? `  (try ${m.attempt})` : ""));
      renderReadings(m.check_no, m.input_fields);
      for (const li of document.querySelectorAll(".check")) li.classList.toggle("current", +li.dataset.no === m.check_no);
      log(`Check ${m.check_no}: ${m.title}`);
      break;
    case "waiting_for_input": flash("Enter the reading below, then keep it on camera", "info", 6000); break;
    case "reshow": flash(`Show again: ${m.reason}`, "warn", 5000); log(`Reshow check ${m.check_no}: ${m.reason}`); break;
    case "result":
      flash(`${m.title}: ${STATUS_LABEL[m.status] || m.status}${m.reason ? " – " + m.reason : ""}`, m.status, 5000);
      log(`Check ${m.check_no} → ${STATUS_LABEL[m.status] || m.status}. ${m.reason || ""}`);
      refresh().catch(() => {});
      break;
    case "skipped": log(`Check ${m.check_no} skipped (${m.reason})`); refresh().catch(() => {}); break;
    case "no_video": flash(m.message, "fail", 6000); log(m.message); break;
    case "done":
      setBanner("✓", "All checks done", m.pending_checks.length ? `Still pending: ${m.pending_checks.join(", ")} – tap a check to redo it` : "Review results and submit the inspection");
      renderReadings(null, []);
      log("AI inspector finished"); refresh().catch(() => {});
      break;
    case "error": toast(m.message); log(`Agent error: ${m.message}`); break;
    case "help": if (!isVendor()) { toast(`Vendor asks for help on check ${m.check_no ?? "-"}`, 8000); log(`Vendor asked for help (check ${m.check_no ?? "-"})`); } break;
  }
}

async function startLocal() {
  S.mode = "local";
  $("#mode-chip").textContent = "Local test mode (no video call)";
  if (!isVendor()) {
    $("#vendor-controls").hidden = true;   // nothing to control without a live call
    $("#video-placeholder").textContent = "No live video in local test mode – tap a check on the right to see the photo the vendor captured. Live video needs LiveKit (see README).";
    return;
  }
  log("Video call not configured – using this device's camera (local test mode)");
  await openLocalCamera();
  selectCheck(firstPending());
}
async function openLocalCamera() {
  if (S.stream) S.stream.getTracks().forEach((t) => t.stop());
  if (!navigator.mediaDevices?.getUserMedia) { toast("Camera needs HTTPS or localhost"); return; }
  try {
    S.stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: S.facing }, width: { ideal: 1920 }, height: { ideal: 1080 } }, audio: false });
    $("#cam").srcObject = S.stream; $("#video-placeholder").hidden = true;
  } catch (e) { toast(`Camera: ${e.message}`); $("#video-placeholder").textContent = "Camera blocked – allow camera access"; }
}
function selectCheck(no) {
  S.current = no;
  for (const li of document.querySelectorAll(".check")) li.classList.toggle("current", +li.dataset.no === no);
  if (no === null) { setBanner("✓", "All checks captured", "Review results and submit the inspection"); renderReadings(null, []); return; }
  const c = checkByNo(no);
  setBanner(no, c.title, c.instruction);
  renderReadings(no, c.input_fields);
}
async function captureLocal() {
  const no = S.current;
  if (no === null) return toast("All checks are done");
  const c = checkByNo(no);
  if (c.input_fields.length && !S.readings[no]) return toast("Enter the reading first");
  const v = $("#cam");
  if (!v.videoWidth) return toast("Camera not ready");
  const canvas = document.createElement("canvas");
  canvas.width = v.videoWidth; canvas.height = v.videoHeight;
  canvas.getContext("2d").drawImage(v, 0, 0);
  const blob = await new Promise((r) => canvas.toBlob(r, "image/jpeg", 0.92));
  const fd = new FormData();
  fd.append("frame", blob, `check-${no}.jpg`);
  const rd = S.readings[no] || {};
  if ("supplier_code" in rd) fd.append("supplier_code", rd.supplier_code);
  else if (Object.keys(rd).length) fd.append("measured", JSON.stringify(Object.fromEntries(Object.entries(rd).map(([k, x]) => [k, Number(x)]))));
  $("#btn-capture").disabled = true;
  flash("Checking…", "info", 20000);
  try {
    const res = await api(`/inspections/${S.insp.id}/checks/${no}/evidence`, { method: "POST", body: fd });
    const retry = res.status === "manual_review" && /^(Reshow|Unable to read)/.test(res.reason);
    flash(`${res.title}: ${STATUS_LABEL[res.status]}${res.reason ? " – " + res.reason : ""}`, retry ? "warn" : res.status, 5000);
    log(`Check ${no} → ${STATUS_LABEL[res.status]}. ${res.reason}`);
    await refresh();
    if (!retry) selectCheck(firstPending());
  } catch (e) { toast(e.message); flash(e.message, "fail"); }
  finally { $("#btn-capture").disabled = false; }
}

/* ---------------- buttons ---------------- */
$("#btn-capture").onclick = () => {
  if (S.mode === "live") { sendMsg({ type: "capture" }); flash("Capturing…", "info", 2000); }
  else captureLocal();
};
$("#btn-skip").onclick = () => {
  if (S.mode === "live") { sendMsg({ type: "skip" }); log("Skipped current check"); return; }
  const pend = S.insp.checks.filter((c) => c.status === "pending").map((c) => c.check_no);
  const next = pend.find((n) => n > (S.current ?? 0)) ?? pend[0] ?? null;
  selectCheck(next === S.current ? null : next);
};
$("#btn-help").onclick = () => { sendMsg({ type: "help", check_no: S.current }); toast(S.mode === "live" ? "QC team notified" : "Help needs a live video call"); };
$("#btn-camera").onclick = async () => {
  S.facing = S.facing === "environment" ? "user" : "environment";
  if (S.mode === "live" && S.room) {
    await S.room.localParticipant.setCameraEnabled(false);
    await S.room.localParticipant.setCameraEnabled(true, { facingMode: S.facing, resolution: { width: 1920, height: 1080, frameRate: 30 } });
  } else if (S.mode === "local") await openLocalCamera();
};
$("#btn-submit").onclick = async () => {
  try {
    await api(`/inspections/${S.insp.id}/submit`, { method: "POST" });
    sendMsg({ type: "stop" });
    toast("Inspection submitted to QC");
    await goHome();
  } catch (e) { toast(e.message); }
};
$("#btn-report").onclick = async () => {
  try {
    const r = await api(`/inspections/${S.insp.id}/report.xlsx`);
    const url = URL.createObjectURL(await r.blob());
    const a = el("a", { href: url, download: `QC_${S.insp.po_number}_${S.insp.id}.xlsx` });
    document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 5000);
  } catch (e) { toast(e.message); }
};
for (const b of document.querySelectorAll("#decision button")) {
  b.onclick = async () => {
    try {
      await api(`/inspections/${S.insp.id}/decision`, { method: "POST", json: {
        decision: b.dataset.decision, note: $("#decision-note").value, record_exception: $("#decision-exception").checked } });
      toast("Decision saved"); await refresh();
    } catch (e) { toast(e.message, 6000); }
  };
}
$("#ref-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const fd = new FormData(ev.target);
  fd.append("color_name", S.insp.color_name);
  try {
    await api(`/pos/${S.insp.po_id}/references`, { method: "POST", body: fd });
    toast("Reference uploaded"); ev.target.reset();
  } catch (e) { toast(e.message); }
});

/* ---------------- start ---------------- */
(async () => {
  try {
    const cfg = await (await fetch("/api/auth/config")).json();
    S.authEnabled = cfg.auth_enabled;
  } catch {}
  if (!S.authEnabled) {   // no login: pick a role from the dropdown
    let saved = null;
    try { saved = localStorage.getItem("qc_role"); } catch {}
    S.role = ["vendor", "qc", "approver", "admin"].includes(saved) ? saved : "vendor";
    $("#role-picker").value = S.role;
    try { await afterLogin(); } catch (e) { toast(e.message, 8000); }
    return;
  }
  S.token = sessionStorage.getItem("qc_token");
  if (S.token) { try { await afterLogin(); return; } catch { S.token = null; } }
  show("view-login");
})();
