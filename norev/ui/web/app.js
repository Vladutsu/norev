"use strict";

const ERRORS = {
  stale: "Sesiunea s-a schimbat între timp. Am reîncărcat-o: verifică din nou.",
  already_decided: "Acțiunea a fost deja decisă.",
  prompt_not_confirmed: "Confirmă promptul înainte de analiză.",
  mode_unavailable: "Modul ales nu e disponibil încă.",
  required: "Completează câmpurile obligatorii.",
  preview_required: "Vezi întâi ce se trimite.",
  preview_changed: "Ce se trimite s-a schimbat. Verifică din nou.",
  invalid_date: "Data se scrie AAAA-LL-ZZ, de exemplu 2026-11-13.",
  invalid_time: "Ora se scrie HH:MM, de exemplu 09:30.",
  unknown_fields: "Câmp necunoscut.",
  not_found: "Sesiunea nu mai există.",
  invalid: "Datele nu sunt valide.",
  internal: "Ceva n-a mers. Încearcă din nou.",
  unsupported_workflow: "Acest tip de acțiune are nevoie de un Connector care nu e încă disponibil.",
  already_running: "Acțiunea se execută deja.",
  not_executable: "Acțiunea nu poate fi executată acum.",
  llm_unavailable: "Modelul local (llama-server) nu a pornit. Verifică LLAMA_SERVER și LLAMACPP_MODEL.",
  no_microphone: "Microfonul nu a putut fi deschis.",
};
// Why connecting Google Calendar did not work (GoogleAccount.connect, #45)
const GOOGLE_FAILED = {
  access_denied: "Ai refuzat accesul în Google. Nimic nu s-a salvat.",
  timeout: "Conectarea a expirat. Încearcă din nou și termină în browser.",
  no_browser: "Browserul nu a putut fi deschis.",
  scope_not_granted: "Google nu a dat acces la evenimentele din calendar.",
  already_connecting: "Conectarea e deja în curs, în browser.",
  not_configured: "Google Calendar nu e configurat pe acest calculator.",
  unreachable: "Google nu poate fi contactat. Verifică conexiunea la internet.",
};
const STAGES = {
  transcribing: "Se face transcrierea finală",
  starting_llm: "Se pornește modelul local",
  analysing: "Se analizează conversația",
  recording: "Înregistrarea",
};
const STATUS = {
  proposed: ["În așteptare", ""],
  approved: ["Aprobat", "ok"],
  rejected: ["Respins", ""],
  executed: ["Executat", "ok"],
  failed: ["Eșuat", ""],
};
const LANGS = { auto: "Detectare automată", ro: "Română", en: "English" };
const ICON = {
  calendar: '<rect x="3.5" y="5" width="17" height="15" rx="3"/><path d="M3.5 10h17M8 3v4M16 3v4"/>',
  pill: '<rect x="2.5" y="8.5" width="19" height="7" rx="3.5" transform="rotate(-35 12 12)"/><path d="M9.6 8.6l4.8 6.8"/>',
  quote: '<path d="M7 7h4v4H7c0 3 1 4 3 5M14 7h4v4h-4c0 3 1 4 3 5"/>',
  arrow: '<path d="M4 12h12M12 6l6 6-6 6"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  pause: '<rect x="6" y="5" width="4" height="14" rx="1.5" fill="currentColor" stroke="none"/><rect x="14" y="5" width="4" height="14" rx="1.5" fill="currentColor" stroke="none"/>',
  play: '<path d="M8 5l11 7-11 7z" fill="currentColor" stroke="none"/>',
};

const state = {
  modes: [], mode: null, lang: "ro",
  others: { context: "", prompt: "", digest: null },
  sessionId: null, review: null, poll: null,
};

const $ = (id) => document.getElementById(id);

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

function icon(name, size = 20) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  for (const [k, v] of Object.entries({ width: size, height: size, viewBox: "0 0 24 24", fill: "none",
    stroke: "currentColor", "stroke-width": "1.8", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" })) {
    svg.setAttribute(k, v);
  }
  svg.innerHTML = ICON[name];
  return svg;
}

async function api(name, ...args) {
  const res = await window.pywebview.api[name](...args);
  if (!res || !res.ok) {
    const err = new Error(name);
    err.code = (res && res.error && res.error.code) || "internal";
    throw err;
  }
  return res.data;
}

let toastTimer = null;
function toast(text) {
  const el = $("toast");
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 4500);
}
const fail = (err) => toast(ERRORS[err.code] || ERRORS.internal);

let queue = Promise.resolve();
function guarded(button, work) {
  if (button) { button.disabled = true; button.setAttribute("aria-busy", "true"); }
  queue = queue.then(work).catch(fail).finally(() => {
    if (button && button.isConnected) { button.disabled = false; button.removeAttribute("aria-busy"); }
  });
  return queue;
}

function show(view) {
  for (const id of ["new", "recording", "review", "list"]) $("view-" + id).hidden = id !== view;
  $("inspector-new").hidden = view !== "new" && view !== "list";
  $("inspector-recording").hidden = view !== "recording";
  $("inspector-review").hidden = view !== "review";
  $("inspector-heading").textContent = view === "review" ? "De luat în seamă" : "Din conversație";
  const recording = view === "recording";
  for (const b of document.querySelectorAll(".navbtn")) b.disabled = recording;
  $("main").focus({ preventScroll: true });
}

function setNav(name) {
  for (const b of document.querySelectorAll(".navbtn")) {
    if (b.dataset.view === name) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  }
}

// ---------- New Session

function renderModes() {
  const box = $("modes");
  box.replaceChildren(...state.modes.map((m) => h("button", {
    type: "button", "aria-pressed": String(m.id === state.mode), disabled: !m.available,
    onclick: () => { state.mode = m.id; renderModes(); },
  }, m.label, h("small", {}, m.available ? m.hint : "în curând"))));
  const missing = state.modes.filter((m) => !m.available).map((m) => m.label);
  $("mode-note").textContent = missing.length ? `${missing.join(", ")}: Domain-ul nu e încă în proiect.` : "";
  $("others").hidden = state.mode !== "others";
  updateStart();
}

function othersConfirmed() {
  return state.others.digest !== null;
}

function invalidateOthers() {
  state.others.digest = null;
  const chip = $("prompt-status");
  chip.textContent = "Neconfirmat";
  chip.classList.remove("ok");
  updateStart();
}

function updateStart() {
  const mode = state.modes.find((m) => m.id === state.mode);
  let reason = "";
  if (!mode || !mode.available) reason = "Alege un mod disponibil.";
  else if (state.mode === "others" && !othersConfirmed()) reason = "Confirmă promptul pentru Others ca să poți începe.";
  $("start-reason").textContent = reason;
  $("start").disabled = Boolean(reason);
  $("import").disabled = Boolean(reason);
}

function othersPayload() {
  return state.mode === "others" ? { context: state.others.context, prompt: state.others.prompt } : null;
}

function initNew() {
  const today = new Date().toLocaleDateString("ro-RO", { weekday: "long", day: "numeric", month: "long" });
  $("today").textContent = today.charAt(0).toUpperCase() + today.slice(1);

  for (const b of $("languages").querySelectorAll("button")) {
    b.addEventListener("click", () => {
      state.lang = b.dataset.lang;
      for (const o of $("languages").querySelectorAll("button")) o.setAttribute("aria-pressed", String(o === b));
    });
  }
  $("others-context").addEventListener("input", (e) => { state.others.context = e.target.value; invalidateOthers(); });
  $("others-prompt").addEventListener("input", (e) => { state.others.prompt = e.target.value; invalidateOthers(); });
  $("others-generate").addEventListener("click", (e) => guarded(e.currentTarget, async () => {
    const { prompt } = await api("others_generate", state.others.context);
    state.others.prompt = prompt;
    $("others-prompt").value = prompt;
    invalidateOthers();
  }));
  $("others-confirm").addEventListener("click", (e) => guarded(e.currentTarget, async () => {
    const { digest } = await api("others_confirm", state.others.context, state.others.prompt);
    state.others.digest = digest;
    const chip = $("prompt-status");
    chip.textContent = "Confirmat";
    chip.classList.add("ok");
    updateStart();
  }));
  $("start").addEventListener("click", (e) => guarded(e.currentTarget, async () => {
    const { session_id } = await api("start", state.mode, state.lang, $("contact").value.trim(), othersPayload());
    startRecording(session_id);
  }));
  $("import").addEventListener("click", (e) => guarded(e.currentTarget, async () => {
    const { session_id } = await api("import_file", state.mode, state.lang, $("contact").value.trim(), othersPayload());
    if (!session_id) return; // the User closed the file dialog
    state.sessionId = session_id;
    $("rec-title").textContent = $("contact").value.trim() || "Fără contact";
    $("rec-meta").textContent = `${LANGS[state.lang]} · fișier importat`;
    $("live").replaceChildren();
    $("rec-controls").hidden = true;
    show("recording");
    await waitAnalysis(session_id);
  }));
}

// ---------- Recording

function startRecording(sessionId) {
  state.sessionId = sessionId;
  $("rec-title").textContent = $("contact").value.trim() || "Fără contact";
  const time = new Date().toLocaleTimeString("ro-RO", { hour: "2-digit", minute: "2-digit" });
  $("rec-meta").textContent = `${LANGS[state.lang]} · început la ${time}`;
  $("meter").replaceChildren(...Array.from({ length: 24 }, () => h("span")));
  $("live").replaceChildren();
  $("analysis").hidden = true;
  $("rec-controls").hidden = false;
  $("stop").disabled = false;
  show("recording");
  pollLive();
  state.poll = setInterval(pollLive, 800);
}

function clock(seconds) {
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

async function pollLive() {
  try {
    const live = await api("live", state.sessionId);
    const order = [];
    const lines = live.segments.map((s) => {
      if (!order.includes(s.speaker_id)) order.push(s.speaker_id);
      const n = order.indexOf(s.speaker_id);
      return h("div", { class: "line" }, h("span", { class: `speaker s${n % 3}` }, `Vorbitor ${n + 1}`), h("p", {}, s.text));
    });
    const box = $("live");
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    if (box.children.length !== lines.length) {
      box.replaceChildren(...lines);
      if (atBottom) box.scrollTop = box.scrollHeight;
    }
    const status = $("rec-status");
    status.replaceChildren(
      h("span", { class: live.paused ? "pausedot" : "recdot pulse", "aria-hidden": "true" }),
      `${live.paused ? "În pauză" : "Se înregistrează"} · ${clock(live.elapsed)}`,
    );
    $("view-recording").classList.toggle("recording", !live.paused);
    const pause = $("pause");
    pause.replaceChildren(icon(live.paused ? "play" : "pause", 24));
    pause.setAttribute("aria-label", live.paused ? "Reia înregistrarea" : "Pune pe pauză");
  } catch (err) {
    stopPolling();
    fail(err);
  }
}

function stopPolling() {
  clearInterval(state.poll);
  state.poll = null;
}

function initRecording() {
  $("pause").addEventListener("click", (e) => guarded(e.currentTarget, async () => {
    await api("pause", state.sessionId);
    await pollLive();
  }));
  $("stop").addEventListener("click", (e) => guarded(e.currentTarget, async () => {
    stopPolling();
    $("view-recording").classList.remove("recording");
    $("rec-controls").hidden = true;
    await waitAnalysis(state.sessionId);
  }));
}

// Stop and import return at once; the analysis runs in the backend and reports its stage.
async function waitAnalysis(sessionId) {
  const line = $("analysis");
  line.hidden = false;
  const busy = (text) => line.replaceChildren(h("span", { class: "recdot pulse", "aria-hidden": "true" }), `${text}…`);
  busy(STAGES.transcribing);
  try {
    await api("stop", sessionId);
    for (;;) {
      const p = await api("progress", sessionId);
      if (p.failed) {
        const what = p.failed === "no_audio" ? "Nu s-a înregistrat nimic. Pornește o sesiune nouă."
          : `Analiza s-a oprit la etapa „${STAGES[p.stage] || p.stage}”. Ce s-a salvat până acolo e păstrat.`;
        line.replaceChildren(what, " ", p.failed === "no_audio" ? "" : h("button", {
          type: "button", class: "btn", onclick: (e) => guarded(e.currentTarget, () => waitAnalysis(sessionId)) }, "Încearcă din nou"));
        return;
      }
      if (p.stage === "ready") break;
      busy(STAGES[p.stage] || STAGES.analysing);
      await new Promise((r) => setTimeout(r, 1000));
    }
    line.hidden = true;
    await openReview(sessionId);
  } catch (err) {
    line.replaceChildren(ERRORS[err.code] || ERRORS.internal);
  }
}

// ---------- Review

async function openReview(sessionId) {
  state.sessionId = sessionId;
  state.review = await api("review", sessionId);
  renderReview();
  setNav(null);
  show("review");
  refreshPending();
}

async function reloadReview() {
  state.review = await api("review", state.sessionId);
  renderReview();
  refreshPending();
}

// edit, preview, approve, reject: checked against the revision the User saw
const act = (button, name, ...args) => call(button, name, ...args, state.review.revision);

// execute, retry, reconcile: the executor checks the approved payload itself (#23)
async function call(button, name, ...args) {
  await guarded(button, async () => {
    try {
      state.review = await api(name, state.sessionId, ...args);
      renderReview();
      refreshPending();
    } catch (err) {
      if (err.code === "stale" || err.code === "already_decided") await reloadReview();
      throw err;
    }
  });
}

function renderReview() {
  const r = state.review;
  const started = new Date(r.started_at).toLocaleString("ro-RO", { weekday: "long", day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" });
  $("review-meta").textContent = `${r.domain.label} · ${started}`;
  $("review-title").textContent = r.contact || "Sesiune fără contact";
  const decided = r.actions.filter((a) => a.status !== "proposed").length;
  $("decided").textContent = `${decided} din ${r.actions.length} decise`;
  $("decided-bar").style.width = r.actions.length ? `${Math.round((decided / r.actions.length) * 100)}%` : "0";
  $("summary").textContent = r.summary || "—";
  const partial = r.untranslated ? h("p", { class: "small muted", role: "note" },
    `Rezultat parțial: ${r.untranslated} intervenții n-au putut fi traduse și au rămas în limba vorbită.`) : null;
  $("actions").replaceChildren(...[partial, ...r.actions.map(actionCard)].filter(Boolean));

  const roleOptions = (current) => [
    h("option", { value: "", selected: !current, disabled: true }, "Alege rolul"),
    ...r.roles.map((role) => h("option", { value: role.name, selected: role.name === current }, role.label)),
  ];
  $("speakers").replaceChildren(...r.speakers.map((s, i) => {
    const select = h("select", { id: `role-${s.id}`, onchange: (e) => guarded(e.target, async () => {
      state.review = await api("set_role", state.sessionId, s.id, e.target.value);
      renderReview();
    }) }, roleOptions(s.role));
    return h("label", { for: `role-${s.id}` }, h("span", {}, `Vorbitor ${i + 1}`), select);
  }));
  $("suggestions").replaceChildren(...(r.suggestions.length ? r.suggestions.map((s) => h("div", { class: "well inset suggestion" },
    h("strong", {}, s.text), s.transcript_excerpt ? h("em", {}, `„${s.transcript_excerpt}”`) : null))
    : [h("p", { class: "small muted" }, "Nimic de semnalat.")]));
}

function actionCard(a) {
  const open = a.status === "proposed";
  const [statusLabel, statusClass] = STATUS[a.status] || [a.status, ""];
  const input = (id, label, value, placeholder, change, missing = false, required = false) => h("label", { for: id },
    h("span", { class: "field-label" }, label, missing ? h("span", { class: "need" }, "De completat") : null),
    h("input", {
      id, class: missing ? "field missing" : "field", value, placeholder,
      disabled: !open, "aria-required": String(required), "aria-invalid": String(missing),
      onchange: (e) => act(e.target, "edit", a.id, change(e.target.value.trim() || null)),
    }));
  const fields = [
    input(`${a.id}-owner`, "Responsabil", a.owner, "", (v) => ({ owner: v })),
    input(`${a.id}-due`, "Termen", a.due_date, "AAAA-LL-ZZ", (v) => ({ due_date: v })),
    ...a.fields.map((f) => input(`${a.id}-${f.name}`, f.label, f.value,
      f.type === "date" ? "AAAA-LL-ZZ" : f.type === "time" ? "HH:MM" : "",
      (v) => ({ fields: { [f.name]: v } }), f.missing, f.required)),
  ];

  const plan = a.plan.map((step) => h("div", { class: "payload inset" },
    h("span", { class: "payload-head" }, icon("arrow", 16), `Ce se trimite · ${step.connector_label}`),
    // nested values (the calendar event) are shown in full, not as [object Object]
    h("pre", {}, Object.entries(step.payload).map(([k, v]) => `${k}: ${v === null || v === "" ? "—"
      : typeof v === "object" ? JSON.stringify(v, null, 2) : v}`).join("\n"))));

  let buttons = null;
  const runnable = state.review.can_execute;
  if (runnable && a.status === "approved" && a.uncertain_step) {
    const uncertain = a.plan.find((s) => s.step === a.uncertain_step);
    const calendar = uncertain && uncertain.connector === "google_calendar";
    buttons = h("div", { class: "actions" },
      h("span", { class: "hint" }, calendar ? "Execuția a fost întreruptă. Evenimentul apare în Google Calendar?"
        : "Execuția a fost întreruptă. S-a salvat documentul?"),
      h("button", { type: "button", class: "btn", onclick: (e) => call(e.currentTarget, "reconcile", a.id, a.uncertain_step, false) }, calendar ? "Nu apare" : "Nu s-a salvat"),
      h("button", { type: "button", class: "btn", onclick: (e) => call(e.currentTarget, "reconcile", a.id, a.uncertain_step, true) }, calendar ? "Apare" : "S-a salvat"));
  } else if (runnable && a.status === "approved") {
    buttons = h("div", { class: "actions" },
      h("span", { class: "hint" }, "Se trimite exact ce ai aprobat."),
      h("button", { type: "button", class: "btn primary", onclick: (e) => call(e.currentTarget, "execute", a.id) }, icon("arrow", 18), "Execută"));
  } else if (runnable && a.status === "failed") {
    buttons = h("div", { class: "actions" },
      h("span", { class: "hint" }, a.error || "Execuția a eșuat."),
      h("button", { type: "button", class: "btn primary", onclick: (e) => call(e.currentTarget, "retry", a.id) }, "Reîncearcă"));
  } else if (open) {
    const hint = a.missing.length ? `Completează: ${a.missing.join(", ")}`
      : a.prepared ? "Se aprobă exact ce vezi mai sus." : "Vezi exact ce se trimite înainte de aprobare.";
    const next = a.prepared
      ? h("button", { type: "button", class: "btn primary", disabled: a.missing.length > 0,
          onclick: (e) => act(e.currentTarget, "approve", a.id) }, icon("check", 18), "Aprobă")
      : h("button", { type: "button", class: "btn primary", disabled: a.missing.length > 0,
          onclick: (e) => act(e.currentTarget, "preview", a.id) }, "Vezi ce se trimite");
    buttons = h("div", { class: "actions" },
      h("span", { class: "hint" }, hint),
      h("button", { type: "button", class: "btn", onclick: (e) => act(e.currentTarget, "reject", a.id) }, "Respinge"),
      next);
  }

  return h("article", { class: `card raised${a.status === "rejected" ? " rejected" : ""}`, "aria-label": a.title },
    h("div", { class: "card-head" },
      h("div", { class: "brand" },
        h("span", { class: "typeicon" }, icon(a.action_type === "medication_reminder" ? "pill" : "calendar", 22)),
        h("div", { class: "stack", style: "gap:.2rem" }, h("span", { class: "typename" }, a.type_label), h("span", { class: "atitle" }, a.title))),
      h("span", { class: `chip ${statusClass}` }, statusLabel)),
    a.description ? h("p", { class: "small", style: "margin:0;color:var(--text-2)" }, a.description) : null,
    h("div", { class: "fields" }, fields),
    a.transcript_excerpt ? h("blockquote", {}, icon("quote", 18), h("span", {}, `„${a.transcript_excerpt}”`)) : null,
    plan,
    buttons);
}

// ---------- Lists

async function refreshPending() {
  try {
    const sessions = await api("sessions");
    $("pending-count").textContent = String(sessions.reduce((n, s) => n + s.pending, 0));
    return sessions;
  } catch (err) {
    return [];
  }
}

async function showList(kind) {
  const sessions = await refreshPending();
  const shown = kind === "pending" ? sessions.filter((s) => s.pending > 0) : sessions;
  $("list-title").textContent = kind === "pending" ? "De verificat" : "Sesiuni recente";
  $("session-list").replaceChildren(...(shown.length ? shown.map((s) => h("button", {
    type: "button", class: "listitem", onclick: () => openReview(s.session_id).catch(fail),
  }, h("span", {}, h("strong", {}, s.contact || "Sesiune fără contact"),
      h("span", { class: "small muted" }, new Date(s.started_at).toLocaleString("ro-RO", { day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" }))),
    h("span", { class: "badge", "aria-label": `${s.pending} de verificat` }, String(s.pending))))
    : [h("p", { class: "muted" }, kind === "pending" ? "Nimic de verificat." : "Nicio sesiune încă.")]));
  show("list");
}

// ---------- Google Calendar (#45): shown only when it is set up on this machine

function renderGoogle(status) {
  $("google").hidden = !status.available;
  $("google-status").textContent = status.connected ? "Conectat. Evenimentele aprobate pot fi trimise."
    : "Neconectat. Conectează contul ca să trimiți evenimente aprobate.";
  $("google-button").textContent = status.connected ? "Deconectează" : "Conectează";
  $("google-button").dataset.connected = String(status.connected);
}

async function googleButton(button) {
  // not through `guarded`: signing in can take minutes in the browser and must not hold the other actions
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  try {
    if (button.dataset.connected === "true") {
      const status = await api("google_disconnect");
      renderGoogle(status);
      toast(status.revoked ? "Contul Google a fost deconectat." : "Contul a fost deconectat aici; Google nu a confirmat revocarea.");
    } else {
      $("google-status").textContent = "Termină conectarea în browser…";
      const status = await api("google_connect");
      renderGoogle(status);
      toast(status.failed ? (GOOGLE_FAILED[status.failed] || "Conectarea la Google nu a reușit.") : "Google Calendar e conectat.");
    }
  } catch (err) {
    fail(err);
  } finally {
    button.disabled = false;
    button.removeAttribute("aria-busy");
  }
}

function initNav() {
  for (const b of document.querySelectorAll(".navbtn")) {
    b.addEventListener("click", () => {
      setNav(b.dataset.view);
      if (b.dataset.view === "new") { show("new"); updateStart(); }
      else showList(b.dataset.view).catch(fail);
    });
  }
}

async function init() {
  initNew();
  initRecording();
  initNav();
  $("main").setAttribute("tabindex", "-1");
  try {
    state.modes = await api("modes");
    const first = state.modes.find((m) => m.available);
    state.mode = first ? first.id : null;
    renderModes();
    refreshPending();
    $("google-button").addEventListener("click", (e) => googleButton(e.currentTarget));
    renderGoogle(await api("google_status"));
  } catch (err) {
    fail(err);
  }
}

if (window.pywebview && window.pywebview.api) init();
else window.addEventListener("pywebviewready", init, { once: true });
