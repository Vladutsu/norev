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
  session_busy: "Sesiunea se înregistrează sau se analizează acum; încearcă după ce se termină.",
  execution_pending: "O acțiune aprobată așteaptă să fie executată. Execut-o sau rezolv-o înainte de ștergere.",
  unsupported_workflow: "Acest tip de acțiune are nevoie de un Connector care nu e încă disponibil.",
  already_running: "Acțiunea se execută deja.",
  not_executable: "Acțiunea nu poate fi executată acum.",
  llm_unavailable: "Modelul local (llama-server) nu a pornit. Verifică LLAMA_SERVER și LLAMACPP_MODEL.",
  no_microphone: "Microfonul nu a putut fi deschis.",
  regenerate_failed: "Regenerarea nu a reușit. Acțiunile de dinainte au rămas cum erau.",
  invalid_email: "Scrie adrese de email valide, separate prin virgulă.",
  invalid_header: "Subiectul și destinatarii nu pot avea mai multe rânduri.",
  invalid_text: "Textul conține caractere care nu pot fi trimise.",
  too_long: "Textul e prea lung.",
  too_many: "Prea mulți destinatari.",
  unknown_destination: "Destinația aleasă nu e disponibilă. Conecteaz-o întâi.",
  no_trello_list: "Alege întâi lista din Trello pentru sarcini.",
  no_jira_project: "Alege întâi proiectul Jira pentru sarcini.",
  invalid_summary: "Titlul trebuie să fie pe un rând, de cel mult 255 de caractere, pentru Jira.",
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
  mail: '<rect x="3" y="5.5" width="18" height="13" rx="2.5"/><path d="M3.5 7l8.5 6 8.5-6"/>',
  pause: '<rect x="6" y="5" width="4" height="14" rx="1.5" fill="currentColor" stroke="none"/><rect x="14" y="5" width="4" height="14" rx="1.5" fill="currentColor" stroke="none"/>',
  play: '<path d="M8 5l11 7-11 7z" fill="currentColor" stroke="none"/>',
};

const state = {
  modes: [], mode: null, lang: "ro",
  others: { context: "", prompt: "", digest: null },
  sessionId: null, review: null, poll: null, rolesChanged: null,
  list: { kind: "recent", sessions: [], confirming: null },
  destination: {},  // action id -> the Connector the User picked (#72)
};

// The destination shown as chosen: the User's pick, else the one already previewed, else the default
function destinationOf(a) {
  const ready = a.destinations.filter((d) => d.ready).map((d) => d.connector);
  const picked = state.destination[a.id];
  if (picked && ready.includes(picked)) return picked;
  if (a.plan[0] && ready.includes(a.plan[0].connector)) return a.plan[0].connector;
  return a.destinations[0].connector;
}

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
      return h("div", { class: "line" }, s.speaker_id == null ? null : h("span", { class: `speaker s${n % 3}` }, `Vorbitor ${n + 1}`), h("p", {}, s.text));
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
// start=false only shows where the analysis is (a Session reopened from the list); it never starts it
async function waitAnalysis(sessionId, start = true) {
  const line = $("analysis");
  line.hidden = false;
  const busy = (text) => line.replaceChildren(h("span", { class: "recdot pulse", "aria-hidden": "true" }), `${text}…`);
  busy(STAGES.transcribing);
  try {
    if (start) await api("stop", sessionId);
    for (;;) {
      const p = await api("progress", sessionId);
      if (p.failed) {
        const what = p.failed === "no_audio" ? "Nu s-a înregistrat nimic. Pornește o sesiune nouă."
          : p.failed === "ModelMissing" ? "Lipsește modelul local cerut de o etapă a analizei. Rulează norev.cmd doctor și vezi logul aplicației. Ce s-a salvat până acolo e păstrat."
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

// After a Role correction, only on the User's click (#60): runs in the backend like the first analysis
async function regenerate(button) {
  await guarded(button, async () => {
    const sessionId = state.sessionId;
    await api("regenerate", sessionId);
    for (;;) {
      const p = await api("progress", sessionId);
      if (p.failed) throw Object.assign(new Error("regenerate"), { code: "regenerate_failed" });
      if (p.stage === "ready") break;
      await new Promise((r) => setTimeout(r, 1000));
    }
    state.rolesChanged = null;
    if (state.sessionId === sessionId) await reloadReview();
  });
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
  renderFinalTranscript(r);
  const partial = r.untranslated ? h("p", { class: "small muted", role: "note" },
    `Rezultat parțial: ${r.untranslated} intervenții n-au putut fi traduse și au rămas în limba vorbită.`) : null;
  $("actions").replaceChildren(...[partial, ...r.actions.map(actionCard)].filter(Boolean));

  const roleOptions = (current) => [
    h("option", { value: "", selected: !current, disabled: true }, "Alege rolul"),
    ...r.roles.map((role) => h("option", { value: role.name, selected: role.name === current }, role.label)),
  ];
  // #60: the owner of a Proposed Action comes from these Roles; an unknown one leaves it empty
  const unknown = r.speakers.some((s) => !s.role) ? h("p", { class: "small muted", role: "note" },
    "Rolul unor vorbitori nu e cunoscut, așa că responsabilul acțiunilor lor rămâne gol. Alege rolul mai jos.") : null;
  const again = state.rolesChanged === r.session_id && r.can_execute ? h("div", { class: "actions" },
    h("span", { class: "hint" }, "Acțiunile deja decise rămân neschimbate."),
    h("button", { type: "button", class: "btn", onclick: (e) => regenerate(e.currentTarget) }, "Regenerează acțiunile")) : null;
  $("speakers").replaceChildren(...[unknown, ...r.speakers.map((s, i) => {
    const select = h("select", { id: `role-${s.id}`, onchange: (e) => guarded(e.target, async () => {
      state.review = await api("set_role", state.sessionId, s.id, e.target.value);
      state.rolesChanged = state.sessionId;
      renderReview();
    }) }, roleOptions(s.role));
    return h("label", { for: `role-${s.id}` }, h("span", {}, `Vorbitor ${i + 1}`), select);
  }), again].filter(Boolean));
  $("suggestions").replaceChildren(...(r.suggestions.length ? r.suggestions.map((s) => h("div", { class: "well inset suggestion" },
    h("strong", {}, s.text), s.transcript_excerpt ? h("em", {}, `„${s.transcript_excerpt}”`) : null))
    : [h("p", { class: "small muted" }, "Nimic de semnalat.")]));
}

const LANGUAGES = { ro: "Română", en: "Engleză", de: "Germană", fr: "Franceză", it: "Italiană", es: "Spaniolă", hu: "Maghiară" };

// The final Transcript (after Stop, whole audio, with Speakers), as spoken and as the analysis read it
function renderFinalTranscript(r) {
  const t = r.transcript || { spoken: [], translated: [] };
  const versions = [["spoken", t.spoken_language], ...(t.translated.length ? [["translated", t.translated_language]] : [])];
  if (!versions.some(([k]) => k === state.transcriptVersion)) state.transcriptVersion = "spoken";
  const shown = t[state.transcriptVersion];
  const order = r.speakers.map((s) => s.id);
  const roleLabel = Object.fromEntries(r.roles.map((role) => [role.name, role.label]));
  const label = (id) => {
    const speaker = r.speakers.find((s) => s.id === id);
    return speaker && speaker.role ? roleLabel[speaker.role] || speaker.role : `Vorbitor ${order.indexOf(id) + 1}`;
  };
  const time = (sec) => `${Math.floor(sec / 60)}:${String(Math.floor(sec % 60)).padStart(2, "0")}`;
  $("final-meta").textContent = `${t.spoken.length} intervenții`;
  $("final-versions").replaceChildren(versions.length > 1 ? h("div", { class: "segmented", role: "group", "aria-label": "Varianta transcrierii" },
    versions.map(([key, lang]) => h("button", {
      type: "button", "aria-pressed": String(key === state.transcriptVersion),
      onclick: () => { state.transcriptVersion = key; renderFinalTranscript(r); },
    }, key === "spoken" ? `Original (${LANGUAGES[lang] || lang || "?"})` : `Tradus (${LANGUAGES[lang] || lang || "?"}), folosit la analiză`))) : "");
  $("final-lines").replaceChildren(...(shown.length ? shown.map((s) => {
    const n = order.indexOf(s.speaker_id);
    return h("div", { class: "line" },
      h("span", { class: "time" }, time(s.start)),
      s.speaker_id == null ? null : h("span", { class: `speaker s${(n < 0 ? 0 : n) % 3}` }, label(s.speaker_id)),
      h("p", {}, s.text, s.untranslated ? h("span", { class: "small muted" }, " (netradus)") : null));
  }) : [h("p", { class: "small muted" }, "Transcrierea e goală.")]));
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
    // an email draft (#61): To, Subject and text decoded from the file, then the exact file below
    step.readable ? h("pre", { class: "readable" },
      `Către: ${step.readable.to}\nSubiect: ${step.readable.subject}\n\n${step.readable.body}`) : null,
    step.readable ? h("span", { class: "payload-head" }, "Fișierul exact") : null,
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
  } else if (runnable && a.status === "executed" && a.draft) {
    // only when the User asks: norev never sends the email, the User does from their mail app
    buttons = h("div", { class: "actions" },
      h("span", { class: "hint" }, "Draftul e salvat. Îl trimiți tu, din aplicația de mail."),
      h("button", { type: "button", class: "btn primary", onclick: (e) => guarded(e.currentTarget, () => api("open_draft", state.sessionId, a.id)) },
        icon("mail", 18), "Deschide în mail"));
  } else if (runnable && a.status === "failed") {
    buttons = h("div", { class: "actions" },
      h("span", { class: "hint" }, a.error || "Execuția a eșuat."),
      h("button", { type: "button", class: "btn primary", onclick: (e) => call(e.currentTarget, "retry", a.id) }, "Reîncearcă"));
  } else if (open) {
    // where the work item goes, chosen per action (#72); a new choice needs a new preview
    const chosen = a.destinations.length ? destinationOf(a) : null;
    const previewed = a.prepared && (!chosen || (a.plan[0] && a.plan[0].connector === chosen));
    const hint = a.missing.length ? `Completează: ${a.missing.join(", ")}`
      : previewed ? "Se aprobă exact ce vezi mai sus." : "Vezi exact ce se trimite înainte de aprobare.";
    const next = previewed
      ? h("button", { type: "button", class: "btn primary", disabled: a.missing.length > 0,
          onclick: (e) => act(e.currentTarget, "approve", a.id) }, icon("check", 18), "Aprobă")
      : h("button", { type: "button", class: "btn primary", disabled: a.missing.length > 0,
          onclick: (e) => chosen ? call(e.currentTarget, "preview", a.id, state.review.revision, chosen)
            : act(e.currentTarget, "preview", a.id) }, "Vezi ce se trimite");
    const destinations = chosen ? h("div", { class: "stack", style: "gap:.4rem" },
      h("span", { class: "field-label", id: `${a.id}-dest` }, "Unde trimiți sarcina"),
      h("div", { class: "segmented", role: "group", "aria-labelledby": `${a.id}-dest` },
        a.destinations.map((d) => h("button", {
          type: "button", "aria-pressed": String(d.connector === chosen), disabled: !d.ready,
          title: d.ready ? "" : `Conectează ${d.label} și alege unde merg sarcinile`,
          onclick: () => { state.destination[a.id] = d.connector; renderReview(); },
        }, d.label)))) : null;
    buttons = h("div", { class: "stack", style: "gap:.8rem" }, destinations, h("div", { class: "actions" },
      h("span", { class: "hint" }, hint),
      h("button", { type: "button", class: "btn", onclick: (e) => act(e.currentTarget, "reject", a.id) }, "Respinge"),
      next));
  }

  return h("article", { class: `card raised${a.status === "rejected" ? " rejected" : ""}`, "aria-label": a.title },
    h("div", { class: "card-head" },
      h("div", { class: "brand" },
        h("span", { class: "typeicon" }, icon(a.action_type === "medication_reminder" ? "pill"
          : a.action_type === "follow_up_email" ? "mail" : "calendar", 22)),
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
  state.list = { kind, sessions: await refreshPending(), confirming: null };
  $("list-title").textContent = kind === "pending" ? "De verificat" : "Sesiuni recente";
  const domain = $("f-domain");
  if (domain.options.length === 1) {
    domain.append(...state.modes.map((m) => h("option", { value: m.id }, m.label)));
  }
  renderList();
  show("list");
}

// Search and filters run here, on the list the backend already returned: nothing is indexed (#59)
function renderList() {
  const { kind, sessions, confirming } = state.list;
  const text = $("f-text").value.trim().toLocaleLowerCase("ro-RO");
  const domain = $("f-domain").value, from = $("f-from").value, to = $("f-to").value;
  const day = (s) => new Date(s.started_at).toLocaleDateString("sv-SE"); // YYYY-MM-DD, local time
  const shown = sessions.filter((s) => (kind !== "pending" || s.pending > 0)
    && (!text || (s.contact || "").toLocaleLowerCase("ro-RO").includes(text))
    && (!domain || s.domain === domain) && (!from || day(s) >= from) && (!to || day(s) <= to));
  const filtered = text || domain || from || to;
  $("session-list").replaceChildren(...(shown.length ? shown.map(sessionRow)
    : [h("p", { class: "muted" }, filtered ? "Nicio sesiune nu se potrivește filtrelor."
      : kind === "pending" ? "Nimic de verificat." : "Nicio sesiune încă.")]));

  function sessionRow(s) {
    const name = s.contact || "Sesiune fără contact";
    const open = () => (s.ready ? openReview(s.session_id) : openAnalysis(s)).catch(fail);
    const status = s.ready ? h("span", { class: "badge", "aria-label": `${s.pending} de verificat` }, String(s.pending))
      : h("span", { class: "chip" }, "Neanalizată");
    const remove = confirming === s.session_id
      ? [h("button", { type: "button", class: "btn", onclick: () => { state.list.confirming = null; renderList(); } }, "Renunță"),
         h("button", { type: "button", class: "btn danger", onclick: (e) => guarded(e.currentTarget, async () => {
           await api("delete", s.session_id);
           await showList(kind);
           toast("Sesiunea a fost ștearsă.");
         }) }, "Confirmă ștergerea")]
      : [h("button", { type: "button", class: "btn", "aria-label": `Șterge ${name}`,
          onclick: () => { state.list.confirming = s.session_id; renderList(); } }, "Șterge")];
    return h("div", { class: "listrow" },
      h("button", { type: "button", class: "listitem", onclick: open },
        h("span", {}, h("strong", {}, name),
          h("span", { class: "small muted" }, new Date(s.started_at).toLocaleString("ro-RO", { day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" }))),
        status),
      ...remove);
  }
}

// A Session reopened before its analysis finished: show where it stopped; retrying is the User's choice
async function openAnalysis(s) {
  state.sessionId = s.session_id;
  $("rec-title").textContent = s.contact || "Fără contact";
  $("rec-meta").textContent = new Date(s.started_at).toLocaleString("ro-RO", { day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" });
  $("live").replaceChildren();
  $("rec-controls").hidden = true;
  setNav(null);
  show("recording");
  await waitAnalysis(s.session_id, false);
}

// ---------- Trello (#72): shown only when it is set up on this machine

const TRELLO_FAILED = {
  invalid_token: "Trello nu a acceptat tokenul. Obține unul nou și lipește-l din nou.",
  not_configured: "Trello nu e configurat pe acest calculator.",
  not_connected: "Conectează întâi Trello.",
  no_such_list: "Lista nu mai există în Trello. Alege alta.",
  no_browser: "Browserul nu a putut fi deschis.",
  unreachable: "Trello nu poate fi contactat. Verifică conexiunea la internet.",
  trello_error: "Trello a răspuns cu o eroare. Încearcă din nou.",
};

async function renderTrello(status) {
  $("trello").hidden = !status.available;
  if (!status.available) return;
  $("trello-connect").hidden = status.connected;
  $("trello-pick").hidden = !status.connected;
  $("trello-disconnect").hidden = !status.connected;
  $("trello-status").textContent = !status.connected ? "Neconectat. Sarcinile pot fi trimise în Trello după conectare."
    : status.list ? `Sarcinile pot merge în: ${status.list.name}` : "Conectat. Alege lista pentru sarcini.";
  if (status.connected) {
    const res = await api("trello_boards");
    $("trello-list").replaceChildren(...res.boards.map((b) => h("optgroup", { label: b.name },
      b.lists.map((l) => h("option", { value: l.id, selected: status.list && status.list.id === l.id }, l.name)))));
    if (res.failed) toast(TRELLO_FAILED[res.failed] || TRELLO_FAILED.trello_error);
  }
}

async function trelloAction(button, name, ...args) {
  // not through `guarded`: Trello answers over the network and must not hold the other actions
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  try {
    const status = await api(name, ...args);
    if (status.failed) toast(TRELLO_FAILED[status.failed] || TRELLO_FAILED.trello_error);
    else if (name === "trello_connect") toast("Trello e conectat. Alege lista pentru sarcini.");
    else if (name === "trello_choose_list") toast("Lista pentru sarcini a fost salvată.");
    else if (name === "trello_disconnect") toast(status.revoked ? "Trello a fost deconectat." : "Trello a fost deconectat aici; Trello nu a confirmat revocarea.");
    if (name === "trello_connect") $("trello-token").value = "";  // the token stays only in the store
    await renderTrello(status);
    if (state.review) renderReview();  // which destinations are ready may have changed
  } catch (err) {
    fail(err);
  } finally {
    button.disabled = false;
    button.removeAttribute("aria-busy");
  }
}

function initTrello() {
  $("trello-authorize").addEventListener("click", (e) => trelloAction(e.currentTarget, "trello_authorize"));
  $("trello-save").addEventListener("click", (e) => trelloAction(e.currentTarget, "trello_connect", $("trello-token").value));
  $("trello-choose").addEventListener("click", (e) => trelloAction(e.currentTarget, "trello_choose_list", $("trello-list").value));
  $("trello-disconnect").addEventListener("click", (e) => trelloAction(e.currentTarget, "trello_disconnect"));
}

// ---------- Jira (#73): shown only when it is set up on this machine. The project chosen here is
// used when the User picks Jira for a work item (#72); each action is still sent where it was approved.

const JIRA_FAILED = {
  access_denied: "Ai refuzat accesul în Atlassian. Nimic nu s-a salvat.",
  timeout: "Conectarea a expirat. Încearcă din nou și termină în browser.",
  no_browser: "Browserul nu a putut fi deschis.",
  port_in_use: "Portul de conectare e ocupat de alt program. Închide-l și încearcă din nou.",
  scope_not_granted: "Atlassian nu a dat acces la issue-urile Jira.",
  already_connecting: "Conectarea e deja în curs, în browser.",
  not_configured: "Jira nu e configurat pe acest calculator.",
  not_connected: "Contul Atlassian nu mai e conectat. Conectează-l din nou.",
  not_accessible: "Proiectul nu mai e accesibil contului conectat.",
  unreachable: "Atlassian nu poate fi contactat. Verifică conexiunea la internet.",
};

function jiraStatusText(status) {
  if (!status.connected) return "Neconectat. Sarcinile pot fi trimise în Jira după conectare.";
  const d = status.destination;
  return d ? `Sarcinile pot merge în: ${d.name} / ${d.project_key} (${d.project_name})`
    : "Conectat. Alege proiectul pentru sarcini.";
}

function renderJira(status, sites) {
  $("jira").hidden = !status.available;
  $("jira-status").textContent = jiraStatusText(status);
  $("jira-button").textContent = status.connected ? "Deconectează" : "Conectează";
  $("jira-button").dataset.connected = String(status.connected);
  const select = $("jira-project");
  select.hidden = $("jira-project-label").hidden = !status.connected;
  if (!status.connected) return;
  const d = status.destination;
  const chosen = d ? `${d.cloud_id}|${d.project_key}` : "";
  const options = [h("option", { value: "", disabled: true }, "Alege proiectul")];
  for (const site of sites || []) {
    for (const p of site.projects) {
      options.push(h("option", { value: `${site.cloud_id}|${p.key}` }, `${site.name} / ${p.key} (${p.name})`));
    }
  }
  if (d && !options.some((o) => o.value === chosen)) {  // the list was not loaded: keep showing the choice
    options.push(h("option", { value: chosen }, `${d.name} / ${d.project_key} (${d.project_name})`));
  }
  select.replaceChildren(...options);
  select.value = chosen;
}

async function jiraLoad() {
  const status = await api("jira_status");
  if (!status.connected) renderJira(status, []);
  else {
    const listed = await api("jira_projects");
    renderJira(listed, listed.sites);
    if (listed.failed) toast(JIRA_FAILED[listed.failed] || "Proiectele Jira nu au putut fi încărcate.");
  }
  if (state.review) renderReview();  // whether Jira can be picked for a work item may have changed
}

async function jiraChoose(select) {
  const [cloudId, key] = select.value.split("|");
  select.disabled = true;
  try {
    const status = await api("jira_choose", cloudId, key);
    $("jira-status").textContent = jiraStatusText(status);
    if (status.failed) toast(JIRA_FAILED[status.failed] || "Proiectul nu a putut fi ales.");
    await jiraLoad();
  } catch (err) {
    fail(err);
  } finally {
    select.disabled = false;
  }
}

async function jiraButton(button) {
  // not through `guarded`: signing in can take minutes in the browser and must not hold the other actions
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  try {
    if (button.dataset.connected === "true") {
      await api("jira_disconnect");
      toast("Contul Atlassian a fost deconectat aici. Revocă accesul aplicației și în contul Atlassian (Connected apps).");
    } else {
      $("jira-status").textContent = "Termină conectarea în browser…";
      const status = await api("jira_connect");
      toast(status.failed ? (JIRA_FAILED[status.failed] || "Conectarea la Jira nu a reușit.")
        : "Jira e conectat. Alege proiectul pentru sarcini.");
    }
    await jiraLoad();
  } catch (err) {
    fail(err);
  } finally {
    button.disabled = false;
    button.removeAttribute("aria-busy");
  }
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

function initList() {
  for (const id of ["f-text", "f-domain", "f-from", "f-to"]) $(id).addEventListener("input", () => renderList());
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
  initList();
  $("main").setAttribute("tabindex", "-1");
  try {
    state.modes = await api("modes");
    const first = state.modes.find((m) => m.available);
    state.mode = first ? first.id : null;
    renderModes();
    refreshPending();
    $("google-button").addEventListener("click", (e) => googleButton(e.currentTarget));
    renderGoogle(await api("google_status"));
    initTrello();
    await renderTrello(await api("trello_status"));
    $("jira-button").addEventListener("click", (e) => jiraButton(e.currentTarget));
    $("jira-project").addEventListener("change", (e) => jiraChoose(e.currentTarget));
    await jiraLoad();
  } catch (err) {
    fail(err);
  }
}

if (window.pywebview && window.pywebview.api) init();
else window.addEventListener("pywebviewready", init, { once: true });
