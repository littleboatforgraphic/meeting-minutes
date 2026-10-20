// Meeting app (PWA): record meetings, live preview, approve minutes, track tasks, manage people. No build step.
"use strict";
const S = { cfg: null, me: null, people: [], rec: null, timers: [] };
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const view = () => $("#view");

function toast(msg, ms = 3500) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), ms);
}

async function api(path, opts = {}) {
  const o = { credentials: "same-origin", ...opts, headers: { ...(opts.headers || {}) } };
  if (o.json !== undefined) { o.body = JSON.stringify(o.json); o.headers["Content-Type"] = "application/json"; delete o.json; }
  const r = await fetch(path, o);
  if (r.status === 401 && !path.startsWith("/api/auth/")) { S.me = null; location.hash = "#/login"; throw new Error("Please sign in"); }
  const ct = r.headers.get("content-type") || "";
  const body = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((body && body.detail) ? (typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail)) : `Error ${r.status}`);
  return body;
}

function every(fn, ms) { const id = setInterval(fn, ms); S.timers.push(id); return id; }
function clearTimers() { S.timers.forEach(clearInterval); S.timers = []; }
const pill = (s) => `<span class="pill ${esc(s)}">${esc(s.replace("_", " "))}</span>`;
const fmtDate = (iso) => iso ? new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "";
const fmtDur = (s) => { s = Math.round(s || 0); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(x).padStart(2, "0"); };
const personName = (id) => (S.people.find((p) => p.id === id) || {}).name || "";
const peopleOptions = (sel, blank = "— unassigned —") =>
  `<option value="">${esc(blank)}</option>` + S.people.filter((p) => p.active !== false)
    .map((p) => `<option value="${p.id}" ${p.id === sel ? "selected" : ""}>${esc(p.name)}</option>`).join("");

// ---------------- routing ----------------
const routes = [
  [/^#\/login$/, viewLogin], [/^#\/invite\/(.+)$/, viewInvite], [/^#\/?$/, viewStart], [/^#\/record\/(\d+)$/, viewRecord],
  [/^#\/meetings$/, viewMeetings], [/^#\/meetings\/(\d+)$/, viewMeeting], [/^#\/tasks$/, viewTasks],
  [/^#\/people$/, viewPeople], [/^#\/me$/, viewMe],
];
async function route() {
  clearTimers();
  const hash = location.hash || "#/";
  const pub = /^#\/(login|invite\/)/.test(hash);
  if (!pub && !S.me) {
    try { S.me = await api("/api/me"); } catch { return; }
  }
  if (S.me && !S.people.length) { try { S.people = await api("/api/people"); } catch {} }
  $("#nav").hidden = !S.me; $("#nav-people").hidden = !(S.me && S.me.role === "admin");
  $$("#nav a").forEach((a) => a.classList.toggle("active", a.getAttribute("href") === hash.replace(/\/\d+$/, "")));
  if (S.rec && !hash.startsWith("#/record/")) {
    if (!confirm("A recording is running. Leave this page? Recording continues only on the recording page.")) {
      location.hash = `#/record/${S.rec.meetingId}`; return;
    }
  }
  for (const [re, fn] of routes) {
    const m = hash.match(re);
    if (m) { try { await fn(...m.slice(1)); } catch (e) { view().innerHTML = `<div class="card">${esc(e.message)}</div>`; } return; }
  }
  view().innerHTML = `<div class="card">Page not found. <a href="#/">Home</a></div>`;
}

// ---------------- auth ----------------
async function viewLogin() {
  const m = S.cfg.auth_method;
  view().innerHTML = `<div class="card" style="max-width:420px;margin:30px auto">
    <h1>Sign in</h1><p class="muted">${esc(S.cfg.company)}</p>
    <form id="f1"><label>Email, phone or Telegram username</label><input name="identifier" autocomplete="username" required>
      ${m !== "otp" ? `<label>Password</label><input name="password" type="password" autocomplete="current-password" required>` : ""}
      <div class="actions"><button>${m === "password" ? "Sign in" : "Send me a code"}</button></div></form>
    <form id="f2" hidden><label>Code sent to you</label><input name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="6" required>
      <div class="actions"><button>Verify</button></div></form>
    ${S.cfg.remember_device ? `<p class="muted">You stay signed in on this device until you sign out or the app is updated.</p>` : ""}</div>`;
  let challenge = null;
  $("#f1").onsubmit = async (e) => {
    e.preventDefault();
    const fd = Object.fromEntries(new FormData(e.target));
    try {
      const r = await api("/api/auth/login", { method: "POST", json: fd });
      if (r.ok) return signedIn();
      challenge = r.otp_challenge; $("#f1").hidden = true; $("#f2").hidden = false; $("#f2 input").focus();
    } catch (err) { toast(err.message); }
  };
  $("#f2").onsubmit = async (e) => {
    e.preventDefault();
    try { await api("/api/auth/otp", { method: "POST", json: { challenge, code: e.target.code.value } }); signedIn(); }
    catch (err) { toast(err.message); }
  };
}
async function signedIn() { S.me = await api("/api/me"); S.people = []; location.hash = "#/"; }

async function viewInvite(token) {
  view().innerHTML = `<div class="card" style="max-width:420px;margin:30px auto"><h1>Set your password</h1>
    <form><label>New password (8+ characters)</label><input name="password" type="password" minlength="8" autocomplete="new-password" required>
    <div class="actions"><button>Save and sign in</button></div></form></div>`;
  $("form").onsubmit = async (e) => {
    e.preventDefault();
    try { await api(`/api/auth/invite/${encodeURIComponent(token)}`, { method: "POST", json: { password: e.target.password.value } }); signedIn(); }
    catch (err) { toast(err.message); }
  };
}

// ---------------- start meeting ----------------
async function viewStart() {
  let devices = [];
  try { devices = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "audioinput"); } catch {}
  const canTab = !!(navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia) && !/Mobi|Android|iPhone|iPad/.test(navigator.userAgent);
  view().innerHTML = `<div class="card"><h1>New meeting</h1>
    <label>Title</label><input id="title" placeholder="e.g. Weekly production meeting" maxlength="300">
    <div class="row"><div><label>Chaired by</label><select id="chair">${peopleOptions(S.me.id, "— me —")}</select></div>
      <div><label>Microphone</label><select id="mic"><option value="">Default microphone</option>
      ${devices.filter((d) => d.deviceId && d.deviceId !== "default").map((d, i) => `<option value="${esc(d.deviceId)}">${esc(d.label || "Microphone " + (i + 1))}</option>`).join("")}</select></div></div>
    ${canTab ? `<label class="switch"><input type="checkbox" id="tab"> Also capture meeting audio from this computer (Zoom/Teams/Meet/Webex)</label>` : ""}
    <label>Attendees <span class="muted" id="attcount"></span></label><input id="attq" placeholder="Search names">
    <div class="chips" id="att"></div>
    ${S.cfg.voiceprints_allowed ? `<label class="switch"><input type="checkbox" id="vp" ${S.cfg.voiceprints_default_on ? "checked" : ""}> Recognise speakers by voice (voiceprints) for this meeting</label>` : ""}
    <div class="actions"><button class="big-btn" id="go">● Start recording</button></div>
    <p class="muted">Or upload a recording: <input type="file" id="file" accept="audio/*,video/*" style="width:auto"></p></div>`;
  const chosen = new Set([S.me.id]);
  const drawChips = () => {
    const q = $("#attq").value.toLowerCase();
    $("#att").innerHTML = S.people.filter((p) => p.active !== false && (!q || p.name.toLowerCase().includes(q) || chosen.has(p.id)))
      .map((p) => `<span class="chip ${chosen.has(p.id) ? "on" : ""}" data-id="${p.id}">${esc(p.name)}</span>`).join("");
    $("#attcount").textContent = `(${chosen.size} selected)`;
  };
  $("#attq").oninput = drawChips;
  $("#att").onclick = (e) => { const id = +e.target.dataset.id; if (!id) return; chosen.has(id) ? chosen.delete(id) : chosen.add(id); drawChips(); };
  drawChips();
  const create = async () => {
    const title = $("#title").value.trim() || `Meeting ${new Date().toLocaleDateString()}`;
    return api("/api/meetings", { method: "POST", json: { title, chair_id: +$("#chair").value || null,
      attendee_ids: [...chosen], voiceprints_on: $("#vp") ? $("#vp").checked : false } });
  };
  $("#go").onclick = async () => {
    $("#go").disabled = true;
    try {
      const rec = new Recorder();
      await rec.open($("#mic").value, $("#tab") && $("#tab").checked);
      const m = await create();
      rec.meetingId = m.id; S.rec = rec; rec.start();
      location.hash = `#/record/${m.id}`;
    } catch (e) { toast(e.message || String(e)); $("#go").disabled = false; }
  };
  $("#file").onchange = async (e) => {
    const f = e.target.files[0]; if (!f) return;
    try {
      const m = await create(); const fd = new FormData(); fd.append("file", f);
      toast("Uploading…", 60000);
      await api(`/api/meetings/${m.id}/upload`, { method: "POST", body: fd });
      toast("Uploaded. Transcribing now."); location.hash = `#/meetings/${m.id}`;
    } catch (err) { toast(err.message); }
  };
}

// ---------------- recorder ----------------
// Captures audio, resamples to 16 kHz mono 16-bit PCM in an AudioWorklet and uploads ~10 s chunks in order.
const WORKLET = `class P extends AudioWorkletProcessor{constructor(){super();this.r=sampleRate/16000;this.pos=0;this.buf=[];this.sq=0;this.n=0}
process(inp){const ch=inp[0];if(!ch||!ch[0])return true;const L=ch[0].length;const x=new Float32Array(L);
for(let c=0;c<ch.length;c++){const d=ch[c];for(let i=0;i<L;i++)x[i]+=d[i]/ch.length}
let p=this.pos;while(p<L){const i=Math.floor(p),f=p-i,a=x[i],b=i+1<L?x[i+1]:a;let v=a+(b-a)*f;v=Math.max(-1,Math.min(1,v));
this.buf.push(v<0?v*32768:v*32767);this.sq+=v*v;this.n++;p+=this.r}this.pos=p-L;
if(this.buf.length>=8000){this.port.postMessage({pcm:Int16Array.from(this.buf),rms:Math.sqrt(this.sq/this.n)});this.buf=[];this.sq=0;this.n=0}return true}}
registerProcessor("pcm16k",P);`;
const CHUNK_SAMPLES = 16000 * 10;

class Recorder {
  constructor() { this.seq = 0; this.queue = []; this.parts = []; this.partLen = 0; this.paused = false; this.elapsed = 0; this.level = 0; this.uploading = false; this.failed = 0; }
  async open(deviceId, withTab) {
    if (!navigator.mediaDevices || !window.AudioWorkletNode) throw new Error("This browser cannot record audio. Use Chrome, Edge or Safari (HTTPS).");
    this.streams = [];
    const mic = await navigator.mediaDevices.getUserMedia({ audio: { deviceId: deviceId ? { exact: deviceId } : undefined,
      echoCancellation: false, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
    this.streams.push(mic);
    if (withTab) {
      const d = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true });
      if (!d.getAudioTracks().length) { d.getTracks().forEach((t) => t.stop()); throw new Error("No audio was shared. Tick 'Share audio' when choosing the tab/screen."); }
      this.streams.push(d);
    }
    this.ctx = new AudioContext();
    await this.ctx.audioWorklet.addModule(URL.createObjectURL(new Blob([WORKLET], { type: "text/javascript" })));
    this.node = new AudioWorkletNode(this.ctx, "pcm16k");
    for (const s of this.streams) this.ctx.createMediaStreamSource(new MediaStream(s.getAudioTracks())).connect(this.node);
    const sink = this.ctx.createGain(); sink.gain.value = 0; this.node.connect(sink).connect(this.ctx.destination);
    this.node.port.onmessage = (e) => this.onPcm(e.data);
  }
  start() { this.running = true; this.ctx.resume(); this.wake(); this._vis = () => document.visibilityState === "visible" && this.wake();
    document.addEventListener("visibilitychange", this._vis); }
  async wake() { try { this.lock = await navigator.wakeLock.request("screen"); } catch {} }
  onPcm({ pcm, rms }) {
    this.level = rms;
    if (!this.running || this.paused) return;
    this.parts.push(pcm); this.partLen += pcm.length; this.elapsed += pcm.length / 16000;
    if (this.partLen >= CHUNK_SAMPLES) this.cut();
  }
  cut() {
    if (!this.partLen) return;
    const out = new Int16Array(this.partLen); let o = 0;
    for (const p of this.parts) { out.set(p, o); o += p.length; }
    this.parts = []; this.partLen = 0;
    this.queue.push({ seq: this.seq++, data: out.buffer }); this.pump();
  }
  async pump() {
    if (this.uploading) return; this.uploading = true;
    while (this.queue.length) {
      const c = this.queue[0];
      try {
        await api(`/api/meetings/${this.meetingId}/chunks?seq=${c.seq}&ext=pcm`, { method: "POST", body: c.data, headers: { "Content-Type": "application/octet-stream" } });
        this.queue.shift(); this.failed = 0;
      } catch (e) {
        this.failed++; await new Promise((r) => setTimeout(r, Math.min(30000, 1000 * 2 ** Math.min(this.failed, 5))));
      }
    }
    this.uploading = false;
  }
  async stop() {
    this.running = false; this.cut();
    this.streams.forEach((s) => s.getTracks().forEach((t) => t.stop()));
    try { await this.ctx.close(); } catch {}
    try { this.lock && this.lock.release(); } catch {}
    document.removeEventListener("visibilitychange", this._vis);
    while (this.queue.length || this.uploading) { await this.pump(); await new Promise((r) => setTimeout(r, 500)); }
  }
}

async function viewRecord(id) {
  const rec = S.rec && S.rec.meetingId === +id ? S.rec : null;
  if (!rec) { location.hash = `#/meetings/${id}`; return; }
  view().innerHTML = `<div class="card"><div class="row"><h1 style="flex:3">Recording</h1><span style="flex:0">${pill("recording")}</span></div>
    <div class="rec-big" id="clock">0:00</div><div class="meter"><div id="lvl"></div></div>
    <p class="muted" id="upl"></p>
    <div class="warn">Keep this screen open and the phone plugged in. Place the phone/mic in the middle of the table.</div>
    <div class="actions"><button class="secondary" id="pause">Pause</button><button class="danger" id="stop">■ Stop and make minutes</button></div></div>
    <div class="card"><h2>Live transcript <span class="muted">(rough, delayed ~30 s)</span></h2><div class="live" id="live"></div></div>`;
  every(() => {
    $("#clock").textContent = fmtDur(rec.elapsed);
    $("#lvl").style.width = Math.min(100, Math.round(rec.level * 400)) + "%";
    $("#upl").textContent = rec.queue.length > 1 ? `${rec.queue.length} audio pieces waiting to upload (check the connection)…` : "";
  }, 250);
  let after = 0;
  const poll = async () => {
    try {
      const r = await api(`/api/meetings/${id}/live?after=${after}`);
      for (const l of r.lines) { after = l.id; $("#live").insertAdjacentHTML("beforeend", `<p><span class="t">${esc(l.t)}</span>${esc(l.text)}</p>`); }
      if (r.lines.length) $("#live").scrollTop = 1e9;
    } catch {}
  };
  every(poll, 5000);
  $("#pause").onclick = () => { rec.paused = !rec.paused; $("#pause").textContent = rec.paused ? "Resume" : "Pause"; };
  $("#stop").onclick = async () => {
    if (!confirm("Stop recording and generate the minutes?")) return;
    $("#stop").disabled = $("#pause").disabled = true; $("#stop").textContent = "Finishing upload…";
    await rec.stop();
    await api(`/api/meetings/${id}/stop`, { method: "POST" });
    S.rec = null; location.hash = `#/meetings/${id}`;
  };
}

// ---------------- meetings ----------------
async function viewMeetings() {
  const ms = await api("/api/meetings");
  view().innerHTML = `<div class="card"><h1>Meetings</h1><div class="list">${ms.map((m) => `<a href="#/meetings/${m.id}">
    <div class="row"><b style="flex:3">${esc(m.title)}</b><span style="flex:0">${pill(m.status)}</span></div>
    <div class="muted">${fmtDate(m.started_at)} · ${fmtDur(m.audio_seconds)} · chair ${esc(m.chair || "-")}</div></a>`).join("") || `<p class="muted">No meetings yet.</p>`}
    </div><p class="muted">Minutes and tasks are kept ${esc("for the retention period")}; transcripts for one week.</p></div>`;
}

const lines = (arr) => (arr || []).join("\n");
const unlines = (s) => s.split("\n").map((x) => x.trim()).filter(Boolean);

async function viewMeeting(id) {
  const m = await api(`/api/meetings/${id}`);
  const busy = ["recording", "processing", "approved"].includes(m.status);
  const head = `<div class="card"><div class="row"><h1 style="flex:3">${esc(m.title)}</h1><span style="flex:0">${pill(m.status)}</span></div>
    <div class="muted">${fmtDate(m.started_at)} · ${fmtDur(m.audio_seconds)} · chair ${esc(m.chair || "-")}</div>
    ${m.status === "processing" ? `<p>Transcribing and drafting minutes… ${m.audio_seconds ? Math.round(100 * m.processed_seconds / m.audio_seconds) : 0}% transcribed. The chair will be notified when the draft is ready.</p>` : ""}
    ${m.status === "recording" ? `<p class="muted">Recording in progress on another device.</p>` : ""}
    ${m.status === "approved" ? `<p>Approved. Sending to attendees…</p>` : ""}
    ${m.status === "failed" ? `<p class="warn">Failed: ${esc(m.error)}</p>${m.can_manage ? `<button id="retry">Retry</button>` : ""}` : ""}
    ${m.minutes && !busy ? `<div class="actions"><a class="btn secondary" href="/api/meetings/${m.id}/minutes.pdf" target="_blank">Minutes PDF</a>
      ${!m.transcript_purged ? `<a class="btn secondary" href="/api/meetings/${m.id}/transcript.txt" target="_blank">Transcript</a>` : ""}</div>` : ""}</div>`;
  if (busy) every(async () => { const n = await api(`/api/meetings/${id}`); if (n.status !== m.status || n.processed_seconds !== m.processed_seconds) route(); }, 5000);
  if (m.status === "draft" && m.can_manage) return draftEditor(m, head);
  view().innerHTML = head + (m.minutes ? minutesReadonly(m) : "") + transcriptHtml(m);
  if ($("#retry")) $("#retry").onclick = async () => { await api(`/api/meetings/${id}/retry`, { method: "POST" }); route(); };
}

function minutesReadonly(m) {
  const d = m.minutes;
  return `<div class="card"><h2>Summary</h2><p>${esc(d.summary)}</p>
    ${(d.agenda || []).map((a, i) => `<h3>${i + 1}. ${esc(a.title)}</h3><ul>${a.discussion.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
      ${a.decisions.length ? `<b>Decisions</b><ul>${a.decisions.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}`).join("")}
    ${d.other_business && d.other_business.length ? `<h3>Other business</h3><ul>${d.other_business.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
    ${d.next_meeting ? `<p><b>Next meeting:</b> ${esc(d.next_meeting)}</p>` : ""}</div>
    <div class="card"><h2>Action items</h2>${taskTable(m.tasks.length ? m.tasks : (d.action_items || []).map((a) => ({ title: a.task, owner: personName(a.owner_id) || a.owner, due_date: a.due_date, remark: a.remark, status: "draft" })))}</div>`;
}

function taskTable(ts, editable = false) {
  if (!ts.length) return `<p class="muted">No tasks.</p>`;
  return `<table><tr><th>Task</th><th>Owner</th><th>Due</th><th class="hide-sm">Remark</th><th>Status</th></tr>${ts.map((t) => `<tr>
    <td>${esc(t.title)}${t.meeting && editable ? `<div class="muted"><a href="#/meetings/${t.meeting_id}">${esc(t.meeting)}</a></div>` : ""}</td>
    <td>${esc(t.owner || "—")}</td><td>${t.due_date ? esc(t.due_date) : `<span class="muted">${esc(t.due_as_said || "—")}</span>`}</td>
    <td class="hide-sm">${esc(t.remark || "")}</td>
    <td>${editable && t.id ? `<select data-task="${t.id}">${["open", "in_progress", "done", "cancelled"].map((s) => `<option ${s === t.status ? "selected" : ""} value="${s}">${s.replace("_", " ")}</option>`).join("")}</select>` : pill(t.status)}</td></tr>`).join("")}</table>`;
}

function transcriptHtml(m) {
  if (m.transcript_purged) return `<div class="card muted">The transcript was deleted after the one-week retention period.</div>`;
  if (!m.transcript.length) return "";
  return `<div class="card"><h2>Transcript</h2><div class="transcript">${m.transcript.map((u) =>
    `<p><span class="muted">[${esc(u.t)}]</span> <b>${esc(u.name || "")}:</b> ${esc(u.text)}</p>`).join("")}</div></div>`;
}

function draftEditor(m, head) {
  const d = structuredClone(m.minutes);
  const spk = Object.fromEntries(m.speakers.map((s) => [s.label, s.person_id]));
  const draw = () => {
    view().innerHTML = head + `<div class="card"><h2>Review the draft</h2>
      <p class="muted">Nothing is sent until you approve. Then attendees get the minutes and their own tasks.</p>
      ${m.speakers.length > 1 || m.voiceprints_on ? `<h3>Who is who</h3><div class="row">${m.speakers.map((s) => `<div><label>${esc(s.label)} <span class="muted">(${fmtDur(s.seconds)}${s.auto_matched ? ", matched by voice" : ""})</span></label>
        <select data-spk="${esc(s.label)}">${peopleOptions(spk[s.label], "— unknown —")}</select></div>`).join("")}</div>` : ""}
      <label>Title</label><input data-k="title" value="${esc(d.title)}">
      <label>Summary</label><textarea data-k="summary">${esc(d.summary)}</textarea>
      <h3>Agenda</h3>${d.agenda.map((a, i) => `<div class="item"><div class="row"><input data-a="${i}" data-f="title" value="${esc(a.title)}" style="flex:5">
        <button class="secondary" data-del-a="${i}" style="flex:0">✕</button></div>
        <label>Discussion (one point per line)</label><textarea data-a="${i}" data-f="discussion">${esc(lines(a.discussion))}</textarea>
        <label>Decisions (one per line)</label><textarea data-a="${i}" data-f="decisions">${esc(lines(a.decisions))}</textarea></div>`).join("")}
      <button class="secondary" id="add-a">+ Agenda item</button>
      <h3>Action items</h3>${d.action_items.map((t, i) => `<div class="item">
        <div class="row"><input data-t="${i}" data-f="task" value="${esc(t.task)}" style="flex:5"><button class="secondary" data-del-t="${i}" style="flex:0">✕</button></div>
        <div class="row"><div><label>Owner ${t.owner && !t.owner_id ? `<span class="muted">(said: "${esc(t.owner)}")</span>` : ""}</label><select data-t="${i}" data-f="owner_id">${peopleOptions(t.owner_id)}</select></div>
          <div><label>Due ${t.due_as_said ? `<span class="muted">(said: "${esc(t.due_as_said)}")</span>` : ""}</label><input type="date" data-t="${i}" data-f="due_date" value="${esc(t.due_date || "")}"></div></div>
        <label>Remark</label><input data-t="${i}" data-f="remark" value="${esc(t.remark || "")}"></div>`).join("")}
      <button class="secondary" id="add-t">+ Action item</button>
      <label>Other business (one per line)</label><textarea data-k="other_business">${esc(lines(d.other_business))}</textarea>
      <label>Next meeting</label><input data-k="next_meeting" value="${esc(d.next_meeting || "")}">
      <div class="actions"><button class="secondary" id="save">Save draft</button><button id="approve">Approve and send</button></div></div>` + transcriptHtml(m);
    bind();
  };
  const collect = () => {
    $$("[data-k]").forEach((el) => { const k = el.dataset.k; d[k] = k === "other_business" ? unlines(el.value) : (el.value.trim() || (k === "next_meeting" ? null : "")); });
    $$("[data-a]").forEach((el) => { const a = d.agenda[+el.dataset.a]; a[el.dataset.f] = el.dataset.f === "title" ? el.value : unlines(el.value); });
    $$("[data-t]").forEach((el) => {
      const t = d.action_items[+el.dataset.t], f = el.dataset.f;
      if (f === "owner_id") { const id = +el.value || null; if (id !== t.owner_id) { t.owner_id = id; t.owner = id ? personName(id) : t.owner; } }
      else if (f === "due_date") { const v = el.value || null; if (v !== t.due_date) { t.due_date = v; if (v) t.remark = t.remark === "No deadline stated in meeting" ? null : t.remark; } }
      else t[f] = el.value.trim() || (f === "task" ? "" : null);
    });
    $$("[data-spk]").forEach((el) => (spk[el.dataset.spk] = +el.value || null));
    d.action_items = d.action_items.filter((t) => t.task);
    return { minutes: d, speakers: spk };
  };
  const bind = () => {
    $("#add-a").onclick = () => { collect(); d.agenda.push({ title: "New item", discussion: [], decisions: [] }); draw(); };
    $("#add-t").onclick = () => { collect(); d.action_items.push({ task: "New task", owner: null, owner_id: null, due_date: null, due_as_said: null, remark: null, owner_candidates: [] }); draw(); };
    $$("[data-del-a]").forEach((b) => (b.onclick = () => { collect(); d.agenda.splice(+b.dataset.delA, 1); draw(); }));
    $$("[data-del-t]").forEach((b) => (b.onclick = () => { collect(); d.action_items.splice(+b.dataset.delT, 1); draw(); }));
    $("#save").onclick = async () => { try { const r = await api(`/api/meetings/${m.id}/draft`, { method: "PUT", json: collect() }); Object.assign(d, r.minutes); toast("Draft saved"); draw(); } catch (e) { toast(e.message); } };
    $("#approve").onclick = async () => {
      const body = collect();
      const un = body.minutes.action_items.filter((t) => !t.owner_id).length;
      if (!confirm(`Approve and send the minutes to all attendees?${un ? `\n\n${un} task(s) have no owner in the directory.` : ""}`)) return;
      try { await api(`/api/meetings/${m.id}/approve`, { method: "POST", json: body }); toast("Approved. Sending now."); route(); } catch (e) { toast(e.message); }
    };
  };
  draw();
}

// ---------------- tasks ----------------
async function viewTasks() {
  const scope = new URLSearchParams(location.hash.split("?")[1] || "").get("scope") || S._taskScope || "mine";
  const ts = await api(`/api/tasks?scope=${scope}`);
  const tabs = [["mine", "Mine"], ["chaired", "Meetings I chaired"]].concat(S.me.role === "admin" ? [["all", "All"]] : []);
  view().innerHTML = `<div class="card"><h1>Tasks</h1><div class="actions" style="margin-top:0">${tabs.map(([k, l]) =>
    `<button class="${k === scope ? "" : "secondary"}" data-scope="${k}">${l}</button>`).join("")}</div>
    <div style="overflow:auto;margin-top:10px">${taskTable(ts, true)}</div></div>`;
  $$("[data-scope]").forEach((b) => (b.onclick = () => { S._taskScope = b.dataset.scope; route(); }));
  $$("[data-task]").forEach((s) => (s.onchange = async () => {
    try { await api(`/api/tasks/${s.dataset.task}`, { method: "PATCH", json: { status: s.value } }); toast("Updated"); } catch (e) { toast(e.message); }
  }));
}

// ---------------- people (admin) ----------------
async function viewPeople() {
  if (S.me.role !== "admin") { location.hash = "#/"; return; }
  S.people = await api("/api/people");
  const form = (p = {}) => `<form id="pf" class="card"><h2>${p.id ? "Edit " + esc(p.name) : "Add person"}</h2>
    <div class="row"><div><label>Name</label><input name="name" value="${esc(p.name)}" required></div>
      <div><label>Other names / nicknames (comma separated)</label><input name="aliases" value="${esc((p.aliases || []).join(", "))}"></div></div>
    <div class="row"><div><label>Email</label><input name="email" type="email" value="${esc(p.email)}"></div>
      <div><label>WhatsApp / phone</label><input name="phone" value="${esc(p.phone)}" placeholder="012-3456789"></div></div>
    <div class="row"><div><label>Telegram username</label><input name="telegram_username" value="${esc(p.telegram_username)}"></div>
      <div><label>ERP user ID</label><input name="erp_user_id" value="${esc(p.erp_user_id)}"></div>
      <div><label>Role</label><select name="role"><option value="member">member</option><option value="admin" ${p.role === "admin" ? "selected" : ""}>admin</option></select></div></div>
    <label class="switch"><input type="checkbox" name="voiceprint_opt_out" ${p.voiceprint_opt_out ? "checked" : ""}> Opted out of voiceprints (deletes any stored voiceprint)</label>
    <label class="switch"><input type="checkbox" name="active" ${p.active !== false ? "checked" : ""}> Active</label>
    <div class="actions"><button>Save</button>${p.id ? `<button type="button" class="secondary" id="inv">Invite link (set password)</button><button type="button" class="secondary" id="cancel">Cancel</button>` : ""}</div></form>`;
  const draw = (edit) => {
    view().innerHTML = form(edit) + `<div class="card"><div class="row"><h2 style="flex:3">People (${S.people.length})</h2>
      <label class="btn secondary" style="flex:0;white-space:nowrap;margin:0">Import Excel/CSV<input type="file" id="imp" accept=".csv,.xlsx" hidden></label></div>
      <div style="overflow:auto"><table><tr><th>Name</th><th class="hide-sm">Email</th><th class="hide-sm">Phone</th><th>Telegram</th><th>Role</th></tr>
      ${S.people.map((p) => `<tr data-pid="${p.id}" style="cursor:pointer;${p.active ? "" : "opacity:.5"}"><td>${esc(p.name)}</td><td class="hide-sm">${esc(p.email || "")}</td>
        <td class="hide-sm">${esc(p.phone || "")}</td><td>${p.telegram_linked ? "linked" : esc(p.telegram_username || "")}</td><td>${esc(p.role)}</td></tr>`).join("")}</table></div>
      <p class="muted">Import columns: Name, Email, Phone, Telegram, ERP user ID, Aliases, Role.</p></div>`;
    $("#pf").onsubmit = async (e) => {
      e.preventDefault();
      const f = e.target, body = { name: f.name.value, email: f.email.value || null, phone: f.phone.value || null,
        telegram_username: f.telegram_username.value || null, erp_user_id: f.erp_user_id.value || null, role: f.role.value,
        aliases: f.aliases.value.split(",").map((x) => x.trim()).filter(Boolean), voiceprint_opt_out: f.voiceprint_opt_out.checked, active: f.active.checked };
      try { await api(edit && edit.id ? `/api/people/${edit.id}` : "/api/people", { method: edit && edit.id ? "PUT" : "POST", json: body }); toast("Saved"); viewPeople(); }
      catch (err) { toast(err.message); }
    };
    if ($("#inv")) $("#inv").onclick = async () => { const r = await api(`/api/people/${edit.id}/invite`, { method: "POST" });
      try { await navigator.clipboard.writeText(r.url); toast("Invite link copied. Send it to the person."); } catch { prompt("Invite link:", r.url); } };
    if ($("#cancel")) $("#cancel").onclick = () => draw();
    $$("[data-pid]").forEach((tr) => (tr.onclick = () => { draw(S.people.find((p) => p.id === +tr.dataset.pid)); scrollTo(0, 0); }));
    $("#imp").onchange = async (e) => {
      const fd = new FormData(); fd.append("file", e.target.files[0]);
      try { const r = await api("/api/people/import", { method: "POST", body: fd }); toast(`Imported: ${r.created} new, ${r.updated} updated`); viewPeople(); }
      catch (err) { toast(err.message); }
    };
  };
  draw();
}

// ---------------- me ----------------
async function viewMe() {
  const me = S.me = await api("/api/me");
  view().innerHTML = `<div class="card"><h1>${esc(me.name)}</h1><p class="muted">${esc(me.email || "")} ${esc(me.phone || "")} · ${esc(me.role)}</p>
    ${S.cfg.telegram_bot ? `<p>Telegram: ${me.telegram_linked ? "linked ✓" : "not linked"} <button class="secondary" id="tg">${me.telegram_linked ? "Re-link" : "Link Telegram"}</button></p>` : ""}
    <form id="pw"><label>Change password</label><div class="row"><input type="password" name="password" minlength="8" autocomplete="new-password" placeholder="New password">
      <button style="flex:0">Save</button></div></form>
    <div class="actions"><button class="danger" id="out">Sign out</button></div>
    <p class="muted">${esc(S.cfg.company)} · Meeting Minutes v${esc(S.cfg.version)}</p></div>`;
  if ($("#tg")) $("#tg").onclick = async () => { try { const r = await api("/api/me/telegram-link", { method: "POST" }); location.href = r.url; } catch (e) { toast(e.message); } };
  $("#pw").onsubmit = async (e) => { e.preventDefault(); try { await api("/api/me/password", { method: "POST", json: { password: e.target.password.value } }); toast("Password changed"); e.target.reset(); } catch (err) { toast(err.message); } };
  $("#out").onclick = async () => { await api("/api/auth/logout", { method: "POST" }); S.me = null; S.people = []; location.hash = "#/login"; };
}

// ---------------- boot ----------------
window.addEventListener("hashchange", route);
window.addEventListener("beforeunload", (e) => { if (S.rec) { e.preventDefault(); e.returnValue = ""; } });
(async () => {
  S.cfg = await api("/api/config");
  $("#brand").textContent = S.cfg.company + " · Minutes";
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  route();
})();
