"use strict";

const ERRORS = {
  stale: "Sesiunea s-a schimbat între timp. Am reîncărcat-o: verifică din nou.",
  already_decided: "Acțiunea a fost deja decisă.",
  prompt_not_confirmed: "Confirmă promptul înainte de analiză.",
  mode_unavailable: "Modul ales nu e disponibil încă.",
  required: "Completează câmpurile obligatorii.",
  preview_required: "Ce se trimite nu e pregătit încă. Încearcă din nou într-o clipă.",
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
  not_team_leader: "Doar Team Leader-ul poate face asta.",
  signed_out: "Ai ieșit din cont. Intră din nou.",
};
// Sign-in and the Team (ADR 0006): checked before ERRORS, whose codes mean other fields elsewhere
const TEAM_ERRORS = {
  sign_in_failed: "Emailul sau parola nu sunt corecte.",
  weak_password: "Parola trebuie să aibă cel puțin 8 caractere.",
  password_mismatch: "Parolele nu sunt la fel.",
  invalid_email: "Scrie o adresă de email validă.",
  invalid_name: "Scrie un nume de cel mult 80 de caractere.",
  invalid_skills: "Un skill are cel mult 40 de caractere.",
  too_many_skills: "Cel mult 30 de skill-uri.",
  invalid_code: "Codul de invitație nu e corect pentru acest email.",
  invitation_expired: "Codul a expirat. Cere Team Leader-ului o invitație nouă.",
  already_member: "Există deja un cont cu acest email.",
  no_invitation: "Nu există o invitație pentru acest email.",
  store_without_key: "Datele de pe acest calculator nu se deschid fără cheia lor (norev.cmd import-key).",
  cannot_remove_self: "Nu te poți elimina din echipă.",
  unknown_member: "Membrul nu mai e în echipă.",
  invalid_user: "Utilizatorul Jira sau Trello nu e valid.",
  not_on_board: "Persoana aleasă nu mai e pe boardul Trello. Reîncarcă pagina Echipa.",
  session_busy: "O sesiune se înregistrează sau se analizează; ieși după ce se termină.",
};
const teamError = (err) => TEAM_ERRORS[err.code] || ERRORS[err.code] || ERRORS.internal;
const TEAM_DOMAINS = ["corporate", "others"];  // where the owner is a Member of the Team
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
  others: { context: "", focus: "", completion: "", prompt: "", completionPrompt: "", digest: null },
  sessionId: null, review: null, poll: null, rolesChanged: null, transcriptChanged: null,
  list: { kind: "recent", sessions: [], confirming: null },
  destination: {},  // action id -> the Connector the User picked (#72)
  previewRefused: {},  // previewKey -> error code: why this action cannot be approved as it is (#87)
  payloadOpen: {},  // "action id:step" -> whether the User opened what is sent
  me: null, team: null, legacyKey: false,  // the signed-in User and their Team (ADR 0006); null in the demo
  boardMembers: null,  // the people on the Trello board, for the Team Leader to link Members; null until loaded
  available: { jira: false, trello: false },  // whether Jira and Trello are set up on this machine
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
const fail = (err) => {
  if (err.code === "signed_out" && state.me) { location.reload(); return; }
  toast(ERRORS[err.code] || ERRORS.internal);
};

let queue = Promise.resolve();
function guarded(button, work) {
  if (button) { button.disabled = true; button.setAttribute("aria-busy", "true"); }
  queue = queue.then(work).catch(fail).finally(() => {
    if (button && button.isConnected) { button.disabled = false; button.removeAttribute("aria-busy"); }
  });
  return queue;
}

function show(view) {
  for (const id of ["new", "recording", "review", "list", "team"]) $("view-" + id).hidden = id !== view;
  $("inspector-new").hidden = view !== "new" && view !== "list" && view !== "team";
  $("inspector-recording").hidden = view !== "recording";
  $("inspector-review").hidden = view !== "review";
  const recording = view === "recording";
  for (const b of document.querySelectorAll(".navbtn")) b.disabled = recording;
  $("main").focus({ preventScroll: true });
}

// While a Session records or is analysed, the nav says so instead of offering a new one (#87)
function sessionInProgress(on) {
  $("nav-new-label").textContent = on ? "Sesiune în curs" : "Sesiune nouă";
  if (on) setNav("new");
}

// show() locks the nav on the recording screen; a stopped analysis unlocks it so the User is never stuck there
function navLocked(locked) {
  for (const b of document.querySelectorAll(".navbtn")) b.disabled = locked;
  sessionInProgress(locked);
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
  const o = state.others;
  return state.mode === "others" ? { context: o.context, focus: o.focus, completion: o.completion, prompt: o.prompt } : null;
}

// #83: the exact completion prompt, from the backend's template; shown before the User can confirm it
async function showCompletionPrompt() {
  const { prompt } = await api("others_completion_prompt", state.others.completion);
  state.others.completionPrompt = prompt;
  $("others-completion-prompt").textContent = prompt;
  $("others-completion-box").hidden = !prompt;
  return prompt;
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
  $("others-focus").addEventListener("input", (e) => { state.others.focus = e.target.value; invalidateOthers(); });
  $("others-completion").addEventListener("input", (e) => {
    state.others.completion = e.target.value;
    state.others.completionPrompt = "";
    $("others-completion-box").hidden = true;
    invalidateOthers();
  });
  $("others-completion").addEventListener("change", () => showCompletionPrompt().catch(() => {}));
  $("others-prompt").addEventListener("input", (e) => { state.others.prompt = e.target.value; invalidateOthers(); });
  $("others-generate").addEventListener("click", (e) => guarded(e.currentTarget, async () => {
    const { prompt } = await api("others_generate", state.others.context, state.others.focus);
    state.others.prompt = prompt;
    $("others-prompt").value = prompt;
    invalidateOthers();
    await showCompletionPrompt();
  }));
  $("others-confirm").addEventListener("click", (e) => guarded(e.currentTarget, async () => {
    const shown = state.others.completionPrompt;
    if ((await showCompletionPrompt()) !== shown) { // the User confirms only a prompt they have seen
      toast("Verifică promptul pentru rezultat, apoi confirmă.");
      return;
    }
    const o = state.others;
    const { digest } = await api("others_confirm", o.context, o.prompt, o.focus, o.completion);
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
    $("rec-status").replaceChildren("Fișier importat");
    sessionInProgress(true);
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
  sessionInProgress(true);
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
    // #87: after Stop the chip no longer says "Se înregistrează"; the analysis below says what happens now
    $("rec-status").replaceChildren(h("span", { class: "pausedot", "aria-hidden": "true" }), "Înregistrare oprită");
    await waitAnalysis(state.sessionId);
  }));
}

// Stop and import return at once; the analysis runs in the backend and reports its stage.
// start=false only shows where the analysis is (a Session reopened from the list); it never starts it
async function waitAnalysis(sessionId, start = true) {
  const line = $("analysis");
  line.hidden = false;
  const busy = (text) => line.replaceChildren(
    h("strong", { class: "analysing" }, h("span", { class: "recdot busy pulse", "aria-hidden": "true" }), `${text}…`),
    h("span", { class: "small muted" }, "Acțiunile propuse apar când se termină analiza. Poate dura câteva minute."),
    h("div", { class: "skeleton", "aria-hidden": "true" }, h("span"), h("span", { style: "width:82%" }), h("span", { style: "width:60%" })));
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
          type: "button", class: "btn", onclick: (e) => guarded(e.currentTarget, () => { navLocked(true); return waitAnalysis(sessionId); }) }, "Încearcă din nou"));
        navLocked(false);
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
    navLocked(false);
  }
}

// ---------- Review

async function openReview(sessionId) {
  state.sessionId = sessionId;
  state.quoted = null;
  state.previewRefused = {};
  sessionInProgress(false);
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
    state.transcriptChanged = null;
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
  renderResult(r);
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
    h("span", { class: "hint" }, "Acțiunile deja decise rămân neschimbate."), regenerateButton()) : null;
  $("speakers").replaceChildren(...[unknown, ...r.speakers.map((s, i) => {
    const select = h("select", { id: `role-${s.id}`, onchange: (e) => guarded(e.target, async () => {
      state.review = await api("set_role", state.sessionId, s.id, e.target.value);
      state.rolesChanged = state.sessionId;
      renderReview();
    }) }, roleOptions(s.role));
    return h("label", { for: `role-${s.id}` }, h("span", {}, `Vorbitor ${i + 1}`), select);
  }), again].filter(Boolean));
  $("suggestions").replaceChildren(...(r.suggestions.length ? r.suggestions.map((s) => h("div", { class: "well inset suggestion" },
    h("strong", {}, s.text), s.transcript_excerpt ? quote(s, "em") : null))
    : [h("p", { class: "small muted" }, "Nimic de semnalat.")]));
  autoPreview();
}

// #87: what an action sends is prepared (preview) as soon as it can be approved, so its card always shows the
// exact payload and "Aprobă" approves exactly that (ADR 0002). An edit or a new destination clears it in the
// backend and it is prepared again; a refusal (a Connector not set up, a title too long) is shown on the card.
const chosenOf = (a) => (a.destinations.length ? destinationOf(a) : null);
const previewKey = (a, chosen) => `${a.id}|${state.review.revision}|${chosen || ""}`;
const isPreviewed = (a, chosen) => a.prepared && (!chosen || (a.plan[0] && a.plan[0].connector === chosen));
let previewing = false;
function autoPreview() {
  const r = state.review;
  if (previewing || !r) return;
  const a = r.actions.find((x) => x.status === "proposed" && !x.missing.length && !isPreviewed(x, chosenOf(x))
    && !(previewKey(x, chosenOf(x)) in state.previewRefused));
  if (!a) return;
  const chosen = chosenOf(a);
  const key = previewKey(a, chosen);
  previewing = true;
  guarded(null, async () => {
    if (state.review !== r) return;  // re-rendered meanwhile: that render prepares what is still missing
    try {
      state.review = await api("preview", r.session_id, a.id, r.revision, ...(chosen ? [chosen] : []));
      const now = state.review.actions.find((x) => x.id === a.id);
      if (now && now.status === "proposed" && !isPreviewed(now, chosen)) state.previewRefused[previewKey(now, chosen)] = "internal";
    } catch (err) {
      if (err.code === "stale") state.review = await api("review", r.session_id);
      else state.previewRefused[key] = err.code;
    }
  }).finally(() => {
    previewing = false;
    if (state.review && state.review.session_id === r.session_id) renderReview();
  });
}

const regenerateButton = () => h("button", { type: "button", class: "btn", onclick: (e) => regenerate(e.currentTarget) }, "Regenerează acțiunile");

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
  // #82: a corrected Segment changes the actions only on the User's click, like a Role
  const corrected = (state.transcriptChanged === r.session_id || r.retranslate) && r.can_execute ? h("div", { class: "actions" },
    h("span", { class: "hint" }, "Acțiunile deja decise rămân neschimbate. Până la regenerare, citatele acțiunilor rămân cele vechi."),
    regenerateButton()) : null;
  if (corrected) $("final-transcript").open = true;  // the button is in the card: never hidden in a collapsed one
  const editable = state.transcriptVersion === "spoken";  // the User corrects only the spoken text (#82)
  const time = (sec) => `${Math.floor(sec / 60)}:${String(Math.floor(sec % 60)).padStart(2, "0")}`;
  $("final-meta").textContent = `${t.spoken.length} intervenții`;
  $("final-versions").replaceChildren(versions.length > 1 ? h("div", { class: "segmented", role: "group", "aria-label": "Varianta transcrierii" },
    versions.map(([key, lang]) => h("button", {
      type: "button", "aria-pressed": String(key === state.transcriptVersion),
      onclick: () => { state.transcriptVersion = key; renderFinalTranscript(r); },
    }, key === "spoken" ? `Original (${LANGUAGES[lang] || lang || "?"})` : `Tradus (${LANGUAGES[lang] || lang || "?"}), folosit la analiză`))) : "");
  $("final-lines").replaceChildren(...(shown.length ? shown.map((s) => {
    const n = order.indexOf(s.speaker_id);
    const quoted = state.quoted && s.start < state.quoted.end && s.end > state.quoted.start;
    return h("div", { class: quoted ? "line quoted" : "line" },
      h("span", { class: "time" }, time(s.start)),
      s.speaker_id == null ? null : h("span", { class: `speaker s${(n < 0 ? 0 : n) % 3}` }, label(s.speaker_id)),
      h("p", {}, editable ? h("button", { type: "button", class: "segment", title: "Corectează textul",
        onclick: (e) => editSegment(e.currentTarget.parentElement, s, label(s.speaker_id)) }, s.text) : s.text,
        s.untranslated ? h("span", { class: "small muted" }, " (netradus)") : null));
  }) : [h("p", { class: "small muted" }, "Transcrierea e goală.")]), ...(corrected ? [corrected] : []));
}

// A quote of an action or Suggestion links to where it was said, when the backend found it (`source`)
function quote(item, tag) {
  const text = h(tag, {}, `„${item.transcript_excerpt}”`);
  return item.source ? h("button", { type: "button", class: "quote-link", title: "Arată în transcriere",
    onclick: () => showSource(item.source) }, text) : text;
}

// Marks the lines said in the quote's time span, in whichever version is shown, and scrolls to them
function showSource(span) {
  state.quoted = span;
  $("final-transcript").open = true;
  renderFinalTranscript(state.review);
  const first = document.querySelector("#final-lines .line.quoted");
  if (first) first.scrollIntoView({ block: "center", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
}

// One spoken Segment in place: saved on leaving the box, Escape cancels; only its text changes (#82)
function editSegment(node, seg, label) {
  const area = h("textarea", { class: "field", "aria-label": `Corectează textul, ${label}` }, seg.text);
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const text = area.value.trim();
    if (save && text !== seg.text) {
      await guarded(area, async () => {
        state.review = await api("edit_transcript", state.sessionId, seg.index, text);
        state.transcriptChanged = state.sessionId;
      });
    }
    const other = [...document.querySelectorAll("#final-lines textarea")].some((t) => t !== area);
    if (!other) renderReview();  // never close another Segment the User opened meanwhile; it renders when done
  };
  area.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.preventDefault(); finish(false); } });
  area.addEventListener("blur", () => finish(true));
  node.replaceWith(area);
  area.focus();
}

// #83: the Others result; copied or saved only on the User's click, saving after they saw the exact file
function renderResult(r) {
  $("result").hidden = !r.completion;
  $("result-text").textContent = r.completion || "";
  $("result-save").hidden = !r.can_execute;
  // re-renders (an action prepared or edited) keep the file the User is looking at; another result closes it
  const box = $("result-preview");
  if (box.dataset.content !== r.completion) {
    box.hidden = true;
    box.replaceChildren();
    delete box.dataset.content;
  }
}

async function copyResult() {
  try {
    await navigator.clipboard.writeText(state.review.completion);
    toast("Rezultatul a fost copiat.");
  } catch {
    toast("Nu am putut copia. Selectează textul și copiază-l manual.");
  }
}

async function previewResult() {
  const { payload } = await api("completion_preview", state.sessionId);
  const box = $("result-preview");
  box.dataset.content = payload.content;
  box.replaceChildren(h("div", { class: "well inset" },
    h("span", { class: "overline" }, "Fișierul care va fi scris"),
    h("p", { class: "small mono" }, payload.destination),
    h("pre", { class: "mono", style: "white-space:pre-wrap;max-height:16rem;overflow:auto;margin:0" }, payload.content),
    h("div", { class: "actions" },
      h("button", { type: "button", class: "btn", onclick: () => { box.hidden = true; } }, "Renunță"),
      h("button", { type: "button", class: "btn primary", onclick: (e) => guarded(e.currentTarget, async () => {
        const { saved } = await api("completion_save", state.sessionId, payload);
        box.hidden = true;
        toast(saved.created ? `Salvat în ${saved.path}` : `Există deja: ${saved.path}`);
      }) }, "Aprobă și salvează"))));
  box.hidden = false;
}

// ADR 0006: the owner is a Member; the Team Leader picks them, everyone else sees who it is
function ownerPick(a, open) {
  const t = state.team;
  if (!t.leader) {
    return h("label", { for: `${a.id}-owner` }, h("span", { class: "field-label" }, "Responsabil"),
      h("input", { id: `${a.id}-owner`, class: "field", value: a.owner || "—", disabled: true, title: "Doar Team Leader-ul alege responsabilul" }));
  }
  const known = t.members.some((m) => m.email === a.assignee);
  return h("label", { for: `${a.id}-owner`, class: "owner-pick" }, h("span", { class: "field-label" }, "Responsabil"),
    h("select", { id: `${a.id}-owner`, class: "field", disabled: !open,
      onchange: (e) => act(e.target, "edit", a.id, { assignee: e.target.value || null }) },
    h("option", { value: "", selected: !known }, a.owner && !known ? `— (${a.owner}, nu e în echipă)` : "— nimeni"),
    t.members.map((m) => h("option", { value: m.email, selected: m.email === a.assignee }, m.name))));
}

function actionCard(a) {
  const open = a.status === "proposed";
  const [statusLabel, statusClass] = STATUS[a.status] || [a.status, ""];
  // change gets the trimmed text; owner, due date and Domain fields send null when empty, title and description ""
  const input = (id, label, value, placeholder, change, missing = false, required = false, multiline = false) => h("label", { for: id },
    h("span", { class: "field-label" }, label, missing ? h("span", { class: "need" }, "De completat") : null),
    h(multiline ? "textarea" : "input", {
      id, class: missing ? "field missing" : "field", value: multiline ? null : value, placeholder, rows: multiline ? 2 : null,
      disabled: !open, "aria-required": String(required), "aria-invalid": String(missing),
      onchange: (e) => act(e.target, "edit", a.id, change(e.target.value.trim())),
    }, multiline ? value : null));
  // #87: the title is edited in the card head, not shown twice; an empty one is marked like a required field
  const titleMissing = !a.title.trim();
  const title = h("input", {
    id: `${a.id}-title`, class: titleMissing ? "field atitle missing" : "field atitle", value: a.title, "aria-label": "Titlu",
    disabled: !open, "aria-required": "true", "aria-invalid": String(titleMissing),
    onchange: (e) => act(e.target, "edit", a.id, { title: e.target.value.trim() }),
  });
  const text = input(`${a.id}-description`, "Descriere", a.description, "", (v) => ({ description: v }), false, false, true);
  const fields = [
    state.team && TEAM_DOMAINS.includes(state.review.domain.name) ? ownerPick(a, open)
      : input(`${a.id}-owner`, "Responsabil", a.owner, "", (v) => ({ owner: v || null })),
    input(`${a.id}-due`, "Termen", a.due_date, "AAAA-LL-ZZ", (v) => ({ due_date: v || null })),
    ...a.fields.map((f) => input(`${a.id}-${f.name}`, f.label, f.value,
      f.type === "date" ? "AAAA-LL-ZZ" : f.type === "time" ? "HH:MM" : "",
      (v) => ({ fields: { [f.name]: v || null } }), f.missing, f.required)),
  ];

  const plan = a.plan.map((step) => h("details", {
    class: "payload inset", open: state.payloadOpen[`${a.id}:${step.step}`] || null,
    ontoggle: (e) => { state.payloadOpen[`${a.id}:${step.step}`] = e.currentTarget.open; },
  },
    h("summary", { class: "payload-head" }, icon("arrow", 16), `Ce se trimite · ${step.connector_label}`,
      h("span", { class: "payload-note" }, open ? "se aprobă exact asta" : a.status === "approved" ? "aprobat" : "")),
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
    // where the work item goes, chosen per action (#72); a new choice is prepared again (#87)
    const chosen = chosenOf(a);
    const previewed = isPreviewed(a, chosen);
    const refused = !a.missing.length && !previewed ? state.previewRefused[previewKey(a, chosen)] : null;
    // #87: "Aprobă" approves the payload prepared above; when it cannot, the button says why (design.md section 5)
    const ready = previewed && !a.missing.length;
    const label = a.missing.length ? `Completează: ${a.missing.join(", ")}`
      : ready ? "Aprobă" : refused ? "Nu se poate aproba" : "Se pregătește…";
    const hint = refused ? (ERRORS[refused] || ERRORS.internal) : "";
    const next = h("button", { type: "button", class: "btn primary", disabled: !ready,
      onclick: (e) => act(e.currentTarget, "approve", a.id) }, ready ? icon("check", 18) : null, label);
    const destinations = chosen ? h("div", { class: "dest" },
      h("span", { class: "field-label", id: `${a.id}-dest` }, "Unde trimiți"),
      h("div", { class: "segmented", role: "group", "aria-labelledby": `${a.id}-dest` },
        a.destinations.map((d) => h("button", {
          type: "button", "aria-pressed": String(d.connector === chosen), disabled: !d.ready,
          title: d.ready ? "" : `Conectează ${d.label} și alege unde merg sarcinile`,
          onclick: () => { state.destination[a.id] = d.connector; renderReview(); },
        }, d.label)))) : null;
    buttons = h("div", { class: "actions" }, destinations,
      h("span", { class: "hint" }, hint),
      h("button", { type: "button", class: "btn", onclick: (e) => act(e.currentTarget, "reject", a.id) }, "Respinge"),
      next);
  }

  return h("article", { class: `card raised${a.status === "rejected" ? " rejected" : ""}`, "aria-label": a.title || "Fără titlu" },
    h("div", { class: "card-head" },
      h("div", { class: "brand" },
        h("span", { class: "typeicon" }, icon(a.action_type === "medication_reminder" ? "pill"
          : a.action_type === "follow_up_email" ? "mail" : "calendar", 22)),
        h("div", { class: "stack grow", style: "gap:.2rem" },
          h("span", { class: "typename" }, a.type_label, titleMissing ? h("span", { class: "need" }, "Titlu de completat") : null), title)),
      h("span", { class: `chip ${statusClass}` }, statusLabel)),
    text,
    h("div", { class: "fields" }, fields),
    a.transcript_excerpt ? h("blockquote", {}, icon("quote", 18), quote(a, "span")) : null,
    plan,
    buttons);
}

// ---------- Sign-in (ADR 0006): the store opens only with a User's password

const auth = { status: null };

function showAuth(status) {
  auth.status = status;
  document.querySelector(".app").hidden = true;
  $("auth").hidden = false;
  setAuthTab(status.first_run ? "up" : "in");
}

function setAuthTab(tab) {
  const s = auth.status;
  const box = $("auth");
  box.dataset.tab = tab;
  if (tab === "up" && !s.first_run) box.dataset.invited = ""; else delete box.dataset.invited;
  for (const b of $("auth-tabs").querySelectorAll("button")) {
    b.setAttribute("aria-pressed", String(b.dataset.tab === tab));
    b.disabled = s.first_run && b.dataset.tab === "in";  // nobody to sign in as yet
  }
  $("auth-title").textContent = tab === "in" ? "Intră în cont" : s.first_run ? "Creează echipa" : "Cont nou";
  $("auth-note").textContent = s.locked_out ? TEAM_ERRORS.store_without_key
    : tab === "in" ? "Parola ta deschide datele de pe acest calculator."
    : s.first_run ? `Primul cont creează echipa pe acest calculator; vei fi Team Leader.${s.legacy_key ? " Datele existente rămân și se deschid de acum cu parola ta." : ""} Parola nu poate fi recuperată: păstreaz-o.`
    : "Ai nevoie de codul de invitație primit de la Team Leader.";
  $("auth-submit").textContent = tab === "in" ? "Intră" : s.first_run ? "Creează echipa" : "Creează contul";
  $("auth-submit").disabled = Boolean(s.locked_out);
  $("a-password").autocomplete = tab === "in" ? "current-password" : "new-password";
  $("auth-error").textContent = "";
}

function initAuth() {
  for (const b of $("auth-tabs").querySelectorAll("button")) b.addEventListener("click", () => setAuthTab(b.dataset.tab));
  $("auth-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const button = $("auth-submit");
    const up = $("auth").dataset.tab === "up";
    $("auth-error").textContent = "";
    if (up && $("a-password").value !== $("a-password2").value) {
      $("auth-error").textContent = TEAM_ERRORS.password_mismatch;
      return;
    }
    button.disabled = true;
    try {
      const status = up
        ? await api("sign_up", $("a-email").value, $("a-name").value, $("a-password").value, $("a-skills").value, $("a-code").value)
        : await api("sign_in", $("a-email").value, $("a-password").value);
      for (const id of ["a-password", "a-password2", "a-code"]) $(id).value = "";
      await startApp(status);
    } catch (err) {
      $("auth-error").textContent = teamError(err);
    } finally {
      button.disabled = false;
    }
  });
  $("sign-out").addEventListener("click", (e) => guarded(e.currentTarget, async () => {
    try {
      await api("sign_out");
      location.reload();  // nothing of the signed-out User stays in the page
    } catch (err) {
      toast(teamError(err));
    }
  }));
}

// ---------- Team dashboard (ADR 0006)

async function showTeam() {
  state.team = await api("team");
  const me = state.team.members.find((m) => m.email === state.team.me);
  $("p-name").value = me.name;
  $("p-skills").value = me.skills.join(", ");
  $("invite-code").hidden = true;
  renderTeam();
  setNav("team");
  show("team");
  // the Team Leader can say which person on the Trello board each Member is (no email to match by)
  state.boardMembers = null;
  if (state.team.leader) {
    api("team_trello_members").then((r) => { state.boardMembers = r.failed ? null : r.members; renderTeam(); }).catch(() => {});
  }
}

function trelloPick(m) {
  const people = state.boardMembers;
  const known = people.some((p) => p.id === m.trello_id);
  return h("label", { class: "stack", style: "gap:.25rem", for: `trello-of-${m.email}` },
    h("span", { class: "small" }, "Pe boardul Trello"),
    h("select", { id: `trello-of-${m.email}`, class: "field", onchange: (e) => guarded(e.target, async () => {
      try {
        const r = await api("team_link_trello", m.email, e.target.value || null);
        state.team = r;
        if (r.failed) toast(TRELLO_FAILED[r.failed] || TRELLO_FAILED.trello_error);
        else toast(e.target.value ? `${m.name} va fi pus pe cardurile Trello care îi revin.` : `${m.name} nu mai e legat de Trello.`);
      } catch (err) {
        toast(teamError(err));
      }
      renderTeam();
    }) },
    h("option", { value: "", selected: !m.trello_id }, "— nelegat"),
    !known && m.trello_id ? h("option", { value: m.trello_id, selected: true }, `${m.trello_user} (nu mai e pe board)`) : null,
    people.map((p) => h("option", { value: p.id, selected: p.id === m.trello_id }, p.name ? `${p.name} (@${p.username})` : `@${p.username}`))));
}

const confirmingMember = { email: null };

function renderTeam() {
  const t = state.team;
  $("team-title").textContent = t.name || "Echipa";
  $("team-rename").hidden = !t.leader;
  if (document.activeElement !== $("team-name")) $("team-name").value = t.name;
  $("team-invite").hidden = !t.leader;
  $("team-connections").hidden = !t.leader;
  const user = (label, name) => `${label}: ${name ? `${name} ✓` : "—"}`;
  const me = t.members.find((m) => m.email === t.me);
  // a service not set up on this machine offers no button that can only fail
  const notSetUp = "Nu e configurat pe acest calculator.";
  $("link-jira-status").textContent = me.jira_user ? `Conectat ca ${me.jira_user}.` : state.available.jira ? "Neconectat." : notSetUp;
  $("link-jira").textContent = me.jira_user ? "Deconectează" : "Conectează Jira";
  $("link-jira").hidden = !me.jira_user && !state.available.jira;
  $("link-trello-status").textContent = me.trello_user ? `Conectat ca @${me.trello_user}.` : state.available.trello ? "Neconectat." : notSetUp;
  $("link-trello").textContent = me.trello_user ? "Deconectează" : "Conectează Trello";
  $("link-trello").hidden = !me.trello_user && !state.available.trello;
  $("members").replaceChildren(...t.members.map((m) => {
    const remove = !t.leader || m.email === t.me ? []
      : confirmingMember.email === m.email
        ? [h("button", { type: "button", class: "btn", onclick: () => { confirmingMember.email = null; renderTeam(); } }, "Renunță"),
           h("button", { type: "button", class: "btn danger", onclick: (e) => guarded(e.currentTarget, async () => {
             state.team = await api("team_remove", m.email);
             confirmingMember.email = null;
             renderTeam();
             toast(`${m.name} nu mai e în echipă.`);
           }) }, "Confirmă eliminarea")]
        : [h("button", { type: "button", class: "btn", "aria-label": `Elimină ${m.name}`,
            onclick: () => { confirmingMember.email = m.email; renderTeam(); } }, "Elimină")];
    return h("div", { class: "member" },
      h("div", { class: "who" }, h("strong", {}, m.name, m.email === t.me ? " (tu)" : ""), h("span", {}, m.email)),
      m.leader ? h("span", { class: "chip ok" }, "Team Leader") : null,
      h("div", { class: "skills", "aria-label": "Skill-uri" },
        m.skills.length ? m.skills.map((s) => h("span", { class: "chip" }, s)) : h("span", { class: "small muted" }, "fără skill-uri")),
      h("div", { class: "tools" }, user("Jira", m.jira_user), h("br"), user("Trello", m.trello_user)),
      t.leader && state.boardMembers ? trelloPick(m) : null,
      ...remove);
  }));
  $("invitations").replaceChildren(...t.invitations.map((i) => h("div", { class: "member" },
    h("div", { class: "who" }, h("strong", {}, i.email),
      h("span", {}, i.expired ? "Invitație expirată" : `Invitație valabilă până la ${new Date(i.expires_at).toLocaleString("ro-RO", { day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" })}`)),
    h("button", { type: "button", class: "btn", onclick: (e) => guarded(e.currentTarget, async () => {
      state.team = await api("team_cancel_invitation", i.email);
      $("invite-code").hidden = true;
      renderTeam();
    }) }, "Anulează"))));
  let legacy = $("legacy-key");
  if (t.leader && state.legacyKey) {
    if (!legacy) {
      legacy = h("div", { class: "well inset", id: "legacy-key", role: "note" },
        h("strong", {}, "Cheia veche a datelor e încă pe disc."),
        h("p", { class: "small muted" }, "Datele se deschid acum cu parola ta. Fă un backup al cheii (norev.cmd export-key), apoi șterge fișierul."),
        h("div", { class: "actions" }, h("button", { type: "button", class: "btn", onclick: (e) => guarded(e.currentTarget, async () => {
          const s = await api("legacy_key_delete");
          state.legacyKey = s.legacy_key;
          renderTeam();
          toast("Cheia veche a fost ștearsă.");
        }) }, "Șterge cheia veche")));
      $("view-team").insertBefore(legacy, $("view-team").children[1]);
    }
  } else if (legacy) legacy.remove();
}

function initTeam() {
  const save = (button, work) => guarded(button, async () => {
    try { await work(); } catch (err) { toast(teamError(err)); }
  });
  $("profile-save").addEventListener("click", (e) => save(e.currentTarget, async () => {
    state.team = await api("profile_save", $("p-name").value, $("p-skills").value);
    const me = state.team.members.find((m) => m.email === state.team.me);
    state.me = { ...state.me, name: me.name };
    $("me-name").textContent = me.name;
    renderTeam();
    toast("Profilul a fost salvat.");
  }));
  // the Member's own Jira and Trello users: one sign-in each, nothing of it kept (ADR 0006)
  const linked = (service) => Boolean(state.team.members.find((m) => m.email === state.team.me)[`${service}_user`]);
  // not through `guarded`: signing in can take minutes in the browser and must not hold the other actions
  $("link-jira").addEventListener("click", async (e) => {
    const button = e.currentTarget;
    button.disabled = true;
    try {
      if (linked("jira")) state.team = await api("profile_unlink", "jira");
      else {
        $("link-jira-status").textContent = "Termină conectarea în browser…";
        const r = await api("profile_jira_connect");
        state.team = r;
        if (r.failed) toast(r.failed === "no_site" ? "Alege întâi, ca Team Leader, proiectul Jira (contul tău are acces la mai multe site-uri)."
          : JIRA_FAILED[r.failed] || "Conectarea la Jira nu a reușit.");
      }
      renderTeam();
    } catch (err) {
      toast(teamError(err));
    } finally {
      button.disabled = false;
    }
  });
  $("link-trello").addEventListener("click", async (e) => {
    const button = e.currentTarget;
    button.disabled = true;
    try {
      if (linked("trello")) state.team = await api("profile_unlink", "trello");
      else {
        $("link-trello-status").textContent = "Termină conectarea în browser…";
        const r = await api("profile_trello_connect");  // read-only, one hour, revoked at once
        state.team = r;
        if (r.failed && r.failed !== "cancelled") toast(TRELLO_FAILED[r.failed] || TRELLO_FAILED.trello_error);
      }
      renderTeam();
    } catch (err) {
      toast(teamError(err));
    } finally {
      button.disabled = false;
    }
  });
  $("team-rename-save").addEventListener("click", (e) => save(e.currentTarget, async () => {
    state.team = await api("team_rename", $("team-name").value);
    renderTeam();
  }));
  $("invite-send").addEventListener("click", (e) => save(e.currentTarget, async () => {
    const r = await api("team_invite", $("invite-email").value);
    state.team = r;
    $("invite-email").value = "";
    const until = new Date(r.expires_at).toLocaleString("ro-RO", { day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" });
    $("invite-code").replaceChildren(
      h("span", { class: "small" }, `Codul pentru ${r.email}:`), h("strong", {}, r.code),
      h("span", { class: "small muted" }, `Valabil până la ${until}. Trimite-i codul; nu va mai fi afișat. Cu el, ${r.email} își face cont din ecranul „Cont nou”.`));
    $("invite-code").hidden = false;
    renderTeam();
  }));
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
  $("rec-status").replaceChildren("Analiză neterminată");
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
  disconnect_first: "Deconectează întâi Trello, apoi schimbă cheia.",
  invalid_key: "Cheia API Trello are doar cifre și litere a–f (de obicei 32). Copiaz-o din tab-ul API key, fără spații sau alt text.",
  access_denied: "Ai refuzat accesul în Trello. Nimic nu s-a salvat.",
  timeout: "Conectarea a expirat. Încearcă din nou și termină în browser.",
  port_in_use: "Portul de conectare e ocupat de alt program. Închide-l și încearcă din nou.",
};

async function renderTrello(status) {
  state.available.trello = status.available;
  // the Team Leader can enter the app's key here (ADR 0006); the whole card stays hidden without a store for it
  $("trello").hidden = !status.available && !status.key_settable;
  $("trello-key").hidden = status.available;
  // a key entered in the app can be changed while Trello is not connected (the token belongs to the key)
  $("trello-key-change").hidden = !(status.available && status.key_in_app && !status.connected);
  if (!status.available) {
    for (const id of ["trello-sign-in", "trello-pick", "trello-disconnect"]) $(id).hidden = true;
    $("trello-status").textContent = "Lipsește cheia API a aplicației pentru Trello.";
    return;
  }
  $("trello-sign-in").hidden = status.connected;
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
    if (status.failed === "cancelled") return;  // a newer sign-in took over; it reports for both
    if (status.failed) toast(TRELLO_FAILED[status.failed] || TRELLO_FAILED.trello_error);
    else if (name === "trello_sign_in") toast("Trello e conectat. Alege lista pentru sarcini.");
    else if (name === "trello_set_key") toast("Cheia Trello a fost salvată. Acum conectează Trello.");
    else if (name === "trello_choose_list") toast("Lista pentru sarcini a fost salvată.");
    else if (name === "trello_disconnect") toast(status.revoked ? "Trello a fost deconectat." : "Trello a fost deconectat aici; Trello nu a confirmat revocarea.");
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
  $("trello-key-change").addEventListener("click", () => {
    $("trello-key").hidden = false;
    $("trello-key-change").hidden = true;
    $("trello-key-input").focus();
  });
  $("trello-key-save").addEventListener("click", async (e) => {
    await trelloAction(e.currentTarget, "trello_set_key", $("trello-key-input").value);
    if (state.available.trello) $("trello-key-input").value = "";  // kept to fix a typo when refused
    if (state.team) renderTeam();  // the Team Leader's own profile can now link Trello too
  });
  // the browser comes back to the app with the token, as Jira's sign-in does: nothing to paste
  $("trello-sign-in").addEventListener("click", (e) => {
    $("trello-status").textContent = "Termină conectarea în browser…";
    trelloAction(e.currentTarget, "trello_sign_in");
  });
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
  state.available.jira = status.available;
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
  $("result-copy").addEventListener("click", () => copyResult());
  $("result-save").addEventListener("click", (e) => guarded(e.currentTarget, previewResult));
}

function initNav() {
  for (const b of document.querySelectorAll(".navbtn")) {
    b.addEventListener("click", () => {
      setNav(b.dataset.view);
      if (b.dataset.view === "new") { show("new"); updateStart(); }
      else if (b.dataset.view === "team") showTeam().catch(fail);
      else showList(b.dataset.view).catch(fail);
    });
  }
}

async function init() {
  initNew();
  initRecording();
  initNav();
  initList();
  initAuth();
  initTeam();
  $("main").setAttribute("tabindex", "-1");
  try {
    const auth = await api("auth_status");
    if (auth.accounts && !auth.signed_in) showAuth(auth);
    else await startApp(auth);
  } catch (err) {
    fail(err);
  }
}

// After sign-in (or at once, without Users): the app as it was, for this User
async function startApp(auth) {
  $("auth").hidden = true;
  document.querySelector(".app").hidden = false;
  state.me = auth.user || null;
  state.legacyKey = Boolean(auth.legacy_key);
  $("me").hidden = $("nav-team").hidden = !state.me;
  if (state.me) {
    $("me-name").textContent = state.me.name;
    state.team = await api("team");
  }
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
