const STORAGE_KEY = "legal-workbench.encrypted.v1";
const state = {
  catalog: null, records: [], recordsById: new Map(), visible: [], page: 1, pageSize: 16,
  lens: "", fullTextMatches: new Map(), searchRequestId: 0, compare: new Set(),
  workspace: null, workspacePassphrase: null, secureMode: "", attachRecordId: "",
};
const el = {};
const worker = new Worker("search-worker.js");

const LENSES = {
  ac: ["מזגן", "מיזוג", "רעש", "רטט", "מעבה", "אקוסטי"],
  water: ["מים", "רטיבות", "נזילה", "ניקוז", "מרזב", "צנרת", "איטום"],
  camera: ["מצלמה", "צילום", "פרטיות", "התחקות", "האזנת סתר"],
  roof: ["גג", "פרגולה", "גגון", "חצר", "גינה", "יונים"],
  common: ["רכוש משותף", "שימוש ייחודי", "הצמדה", "הסגת גבול", "סילוק יד"],
  jurisdiction: ["סמכות", "סעיף 72", "פיצול סעדים", "סעד", "מפקח"],
};

const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;").replaceAll("'", "&#039;");

const normalize = (value) => String(value ?? "")
  .normalize("NFKD").replace(/[\u0591-\u05C7]/g, "")
  .replace(/[\u200e\u200f\u202a-\u202e]/g, "")
  .replace(/[^\p{L}\p{N}]+/gu, " ").trim().toLocaleLowerCase("he");

const formatDate = (value) => {
  if (!value) return "ללא תאריך";
  const [year, month, day] = value.split("-");
  return `${day}.${month}.${year}`;
};

const debounce = (fn, delay = 260) => {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), delay); };
};

function toast(message) {
  el.toast.textContent = message;
  el.toast.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.toast.hidden = true; }, 3200);
}

function cacheElements() {
  [
    "corpus-status", "query", "clear-query", "search-progress", "office-filter",
    "municipality-filter", "adjudicator-filter", "year-filter", "category-filter",
    "type-filter", "status-filter", "full-text-filter", "ashdod-filter", "show-duplicates",
    "sort-select", "result-count", "result-list", "pagination", "loading-state", "empty-state",
    "error-state", "reset-filters", "mobile-filter-button", "filters", "updated-at",
    "compare-count", "comparison-empty", "comparison-table", "clear-comparison", "case-dialog",
    "dialog-content", "secure-dialog", "secure-form", "secure-title", "secure-help", "secure-error",
    "workspace-passphrase", "workspace-passphrase-confirm", "confirm-label", "workspace-status",
    "workspace-locked", "workspace-content", "workspace-unlock", "workspace-lock", "workspace-lock-icon",
    "add-claim", "claims-list", "readiness-strip", "import-seed", "seed-file", "export-word",
    "print-package", "export-backup", "import-backup", "backup-file", "delete-workspace",
    "attach-dialog", "attach-form", "attach-claim", "attach-side", "attach-page", "attach-quote",
    "attach-verified", "corpus-facts", "toast",
  ].forEach((id) => { el[id] = document.getElementById(id); });
}

function addOptions(select, values) {
  for (const value of values) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    select.append(option);
  }
}

function metadataHaystack(record) {
  if (!record._haystack) {
    record._haystack = normalize([
      record.caseNumber, record.date, record.type, record.office, record.adjudicator,
      record.municipality, record.address, record.plaintiffs, record.defendants,
      record.representatives, ...(record.categories || []), ...(record.keywords || []),
      record.summary, record.operativeExcerpt, record.sourceName,
    ].join(" "));
  }
  return record._haystack;
}

function parseMetadataTokens(value) {
  return normalize(value.replaceAll('"', " ").replace(/\bOR\b/gi, " ").replace(/\sאו\s/g, " "))
    .split(/\s+/).filter(Boolean);
}

function lensMatches(record) {
  if (!state.lens) return true;
  const haystack = metadataHaystack(record);
  return LENSES[state.lens].some((term) => haystack.includes(normalize(term)));
}

function ranking(record, tokens) {
  let score = Number(record.verificationRank || 0) * 1000;
  const reasons = [record.verification || "מעמד המקור לא סווג"];
  const haystack = metadataHaystack(record);
  let matched = 0;
  for (const token of tokens) if (haystack.includes(token)) matched += 1;
  if (matched) { score += matched * 85; reasons.push(`התאמה במטא־דאטה: ${matched}`); }
  const fullText = state.fullTextMatches.get(record.id);
  if (fullText) { score += 55; reasons.push(`התאמה בטקסט המלא${fullText.pages?.length ? ` בעמ׳ ${fullText.pages.slice(0, 3).join(", ")}` : ""}`); }
  if (state.lens && lensMatches(record)) { score += 80; reasons.push("התאמה למיקוד הנבחר"); }
  if (record.ashdodRelation) { score += 25; reasons.push("זיקה לאשדוד"); }
  if (normalize(record.caseNumber) === normalize(el.query.value)) { score += 180; reasons.push("מספר תיק מדויק"); }
  return { score, reasons };
}

function updateUrl() {
  const params = new URLSearchParams();
  const mappings = [
    ["q", el.query.value.trim()], ["office", el["office-filter"].value],
    ["city", el["municipality-filter"].value], ["judge", el["adjudicator-filter"].value],
    ["year", el["year-filter"].value], ["topic", el["category-filter"].value],
    ["type", el["type-filter"].value], ["status", el["status-filter"].value], ["lens", state.lens],
  ];
  mappings.forEach(([key, value]) => { if (value) params.set(key, value); });
  if (el["full-text-filter"].checked) params.set("fullText", "1");
  if (el["ashdod-filter"].checked) params.set("ashdod", "1");
  history.replaceState({}, "", `${location.pathname}${params.size ? `?${params}` : ""}${location.hash}`);
}

function requestFullTextSearch(query) {
  const trimmed = query.trim();
  state.searchRequestId += 1;
  const requestId = state.searchRequestId;
  if (normalize(trimmed).length < 2) {
    state.fullTextMatches.clear();
    el["search-progress"].textContent = "";
    applyFilters();
    return;
  }
  el["search-progress"].textContent = "מחפש בטקסט המלא…";
  worker.postMessage({ type: "search", query: trimmed, requestId });
}

worker.onmessage = (event) => {
  const message = event.data;
  if (message.requestId && message.requestId !== state.searchRequestId) return;
  if (message.type === "progress") {
    el["search-progress"].textContent = message.message || "מחפש…";
    return;
  }
  if (message.type === "error") {
    el["search-progress"].textContent = "חיפוש הטקסט המלא לא הושלם; תוצאות המטא־דאטה עדיין זמינות.";
    return;
  }
  state.fullTextMatches = new Map(message.matches.map((item) => [item.id, item]));
  el["search-progress"].textContent = `נמצאו ${message.matches.length.toLocaleString("he-IL")} התאמות בטקסט מלא`;
  state.page = 1;
  applyFilters(false);
};

function applyFilters(writeUrl = true) {
  const query = el.query.value.trim();
  const tokens = parseMetadataTokens(query);
  const filters = {
    office: el["office-filter"].value, municipality: el["municipality-filter"].value,
    adjudicator: el["adjudicator-filter"].value, year: el["year-filter"].value,
    category: el["category-filter"].value, type: el["type-filter"].value,
    status: el["status-filter"].value,
  };
  state.visible = state.records.filter((record) => {
    if (!el["show-duplicates"].checked && (record.duplicateGroup || record.isTest)) return false;
    if (filters.office && record.office !== filters.office) return false;
    if (filters.municipality && record.municipality !== filters.municipality) return false;
    if (filters.adjudicator && record.adjudicator !== filters.adjudicator) return false;
    if (filters.year && record.year !== filters.year) return false;
    if (filters.category && !(record.categories || []).includes(filters.category)) return false;
    if (filters.type && record.type !== filters.type) return false;
    if (filters.status && record.sourceStatus !== filters.status) return false;
    if (el["full-text-filter"].checked && !record.hasFullText) return false;
    if (el["ashdod-filter"].checked && !record.ashdodRelation) return false;
    if (!lensMatches(record)) return false;
    if (!tokens.length) return true;
    const metadataMatch = tokens.every((token) => metadataHaystack(record).includes(token));
    return metadataMatch || state.fullTextMatches.has(record.id);
  });
  state.visible.forEach((record) => { record._ranking = ranking(record, tokens); });
  const sort = el["sort-select"].value;
  state.visible.sort((left, right) => {
    if (sort === "date-desc") return right.date.localeCompare(left.date);
    if (sort === "date-asc") return left.date.localeCompare(right.date);
    if (sort === "case") return left.caseNumber.localeCompare(right.caseNumber, "he", { numeric: true });
    return right._ranking.score - left._ranking.score || right.date.localeCompare(left.date);
  });
  state.page = Math.min(state.page, Math.max(1, Math.ceil(state.visible.length / state.pageSize)));
  renderResults();
  if (writeUrl) updateUrl();
}

function badge(value, className = "pill") {
  return value ? `<span class="${className}">${escapeHtml(value)}</span>` : "";
}

function resultCard(record) {
  const summary = record.summary || "אין תקציר זמין; יש לעיין במסמך המקור.";
  const tags = (record.categories || []).slice(0, 4).map((tag) => badge(tag, "tag")).join("");
  const fullText = state.fullTextMatches.get(record.id);
  const pages = fullText?.pages?.length ? ` · התאמה בעמ׳ ${fullText.pages.slice(0, 4).join(", ")}` : "";
  const reasons = (record._ranking?.reasons || []).join(" · ");
  const compareSelected = state.compare.has(record.id);
  return `<article class="result-card" data-id="${escapeHtml(record.id)}">
    <div class="result-main">
      <div class="case-line"><span class="case-number">${escapeHtml(record.caseNumber)}</span>${badge(formatDate(record.date))}${badge(record.type)}</div>
      <div class="badge-line"><span class="rank-pill rank-${record.verificationRank}">${escapeHtml(record.verification)}</span>${badge(record.sourceStatus)}${record.ashdodRelation ? badge("אשדוד") : ""}${record.hasFullText ? badge("טקסט מלא") : ""}</div>
      <h3>${escapeHtml(record.office || "לשכה לא ידועה")}${record.adjudicator ? ` · ${escapeHtml(record.adjudicator)}` : ""}</h3>
      <p>${escapeHtml(summary)}</p>
      ${fullText?.snippet ? `<p class="why-row">${escapeHtml(fullText.snippet)}</p>` : ""}
      <div class="meta-line">${record.municipality ? `<span>יישוב: ${escapeHtml(record.municipality)}</span>` : ""}${record.address ? `<span>כתובת: ${escapeHtml(record.address)}</span>` : ""}${record.pages ? `<span>${record.pages} עמודים</span>` : ""}</div>
      <div class="why-row"><strong>מדוע דורג כאן:</strong> ${escapeHtml(reasons)}${escapeHtml(pages)}</div>
      <div class="tags">${tags}</div>
    </div>
    <div class="card-actions">
      <button class="primary" type="button" data-action="details">פרטים וציטוט</button>
      <button type="button" data-action="compare" class="${compareSelected ? "selected" : ""}">${compareSelected ? "הוסר מהשוואה" : "להשוואה"}</button>
      <button type="button" data-action="attach">הוספה לטענה</button>
      <button type="button" data-action="copy">העתקת אסמכתה</button>
      ${record.pdf ? `<a href="${escapeHtml(record.pdf)}" target="_blank" rel="noopener">פתיחת PDF</a>` : ""}
    </div>
  </article>`;
}

function renderResults() {
  const start = (state.page - 1) * state.pageSize;
  const records = state.visible.slice(start, start + state.pageSize);
  el["result-count"].textContent = `${state.visible.length.toLocaleString("he-IL")} רשומות`;
  el["loading-state"].hidden = true;
  el["error-state"].hidden = true;
  el["empty-state"].hidden = state.visible.length !== 0;
  el["result-list"].innerHTML = records.map(resultCard).join("");
  renderPagination();
}

function renderPagination() {
  const total = Math.ceil(state.visible.length / state.pageSize);
  if (total <= 1) { el.pagination.innerHTML = ""; return; }
  const buttons = [`<button data-page="${state.page - 1}" ${state.page === 1 ? "disabled" : ""} aria-label="עמוד קודם">‹</button>`];
  const from = Math.max(1, state.page - 2); const to = Math.min(total, state.page + 2);
  if (from > 1) buttons.push(`<button data-page="1">1</button><span>…</span>`);
  for (let page = from; page <= to; page += 1) buttons.push(`<button data-page="${page}" ${page === state.page ? 'aria-current="page"' : ""}>${page}</button>`);
  if (to < total) buttons.push(`<span>…</span><button data-page="${total}">${total}</button>`);
  buttons.push(`<button data-page="${state.page + 1}" ${state.page === total ? "disabled" : ""} aria-label="עמוד הבא">›</button>`);
  el.pagination.innerHTML = buttons.join("");
}

function citation(record) {
  const forum = record.office ? `המפקח/ת על רישום מקרקעין – ${record.office}` : "המפקח/ת על רישום מקרקעין";
  return `${forum}, תיק ${record.caseNumber} (${formatDate(record.date)})`;
}

async function copyText(value) {
  try { await navigator.clipboard.writeText(value); toast("הטקסט הועתק"); }
  catch { toast("לא ניתן היה להעתיק אוטומטית"); }
}

function detailItem(label, value) {
  return value ? `<div class="detail-item"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>` : "";
}

async function pageExcerpt(record, page) {
  if (!record.text || !page) return "";
  try {
    const text = await fetch(record.text).then((response) => response.text());
    const pattern = new RegExp(`=== PDF PAGE ${page} ===([\\s\\S]*?)(?==== PDF PAGE|$)`);
    return (text.match(pattern)?.[1] || "").trim().slice(0, 2800);
  } catch { return ""; }
}

async function openDetails(record) {
  const match = state.fullTextMatches.get(record.id);
  const matchedPage = match?.pages?.[0] || "";
  const excerpt = match?.snippet || await pageExcerpt(record, matchedPage);
  el["dialog-content"].innerHTML = `
    <div class="dialog-kicker">${escapeHtml(record.type)} · ${escapeHtml(record.verification)}</div>
    <h2 class="dialog-title" id="dialog-title">${escapeHtml(record.caseNumber)}</h2>
    <p>${escapeHtml(citation(record))}</p>
    <div class="dialog-summary">${escapeHtml(record.summary || "אין תקציר זמין")}</div>
    <div class="detail-grid">${detailItem("לשכה", record.office)}${detailItem("מפקח/ת", record.adjudicator)}${detailItem("תובעים", record.plaintiffs)}${detailItem("נתבעים", record.defendants)}${detailItem("יישוב", record.municipality)}${detailItem("כתובת", record.address)}${detailItem("מעמד מקור", record.sourceStatus)}${detailItem("אימות", record.reviewStatus)}${detailItem("זיקה לאשדוד", record.ashdodRelation)}</div>
    <div class="dialog-section"><h3>נושאים</h3><div class="tags">${(record.categories || []).map((tag) => badge(tag, "tag")).join("")}</div></div>
    ${record.operativeExcerpt ? `<div class="dialog-section"><h3>קטע אופרטיבי לאיתור</h3><div class="page-hit">${escapeHtml(record.operativeExcerpt)}</div></div>` : ""}
    ${excerpt ? `<div class="dialog-section"><h3>התאמה בטקסט${matchedPage ? ` — עמוד ${matchedPage}` : ""}</h3><div class="page-hit">${escapeHtml(excerpt)}</div></div>` : ""}
    <div class="dialog-section"><h3>אזהרת שימוש</h3><p>${record.sourceStatus === "רשמי" ? "יש לבדוק ערעור ומעמד עדכני לפני הסתמכות." : "זה אינו מסמך מקור רשמי מלא; אין לייחס להחלטה פרטים מעבר למקור המקושר."}</p></div>
    <div class="dialog-actions"><button class="primary-button" data-dialog-action="attach" data-id="${escapeHtml(record.id)}" data-page="${matchedPage}">הוספה לטענה</button><button data-dialog-action="copy" data-id="${escapeHtml(record.id)}">העתקת אסמכתה</button>${record.pdf ? `<a class="primary-button" href="${escapeHtml(record.pdf)}" target="_blank" rel="noopener">פתיחת PDF</a>` : ""}${record.sourceUrl && record.sourceUrl !== record.pdf ? `<a href="${escapeHtml(record.sourceUrl)}" target="_blank" rel="noopener">מקור ציבורי</a>` : ""}</div>`;
  el["case-dialog"].showModal();
}

function switchView(name) {
  document.querySelectorAll(".view").forEach((view) => { view.hidden = view.dataset.view !== name; view.classList.toggle("active", view.dataset.view === name); });
  document.querySelectorAll("[data-view-target]").forEach((button) => button.classList.toggle("active", button.dataset.viewTarget === name));
  location.hash = name === "search" ? "" : name;
  if (name === "compare") renderComparison();
  if (name === "workspace") renderWorkspace();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function toggleCompare(id) {
  if (state.compare.has(id)) state.compare.delete(id);
  else if (state.compare.size >= 4) { toast("ניתן להשוות עד ארבע החלטות"); return; }
  else state.compare.add(id);
  el["compare-count"].textContent = state.compare.size;
  renderResults();
  renderComparison();
}

function renderComparison() {
  const records = [...state.compare].map((id) => state.recordsById.get(id)).filter(Boolean);
  el["comparison-empty"].hidden = records.length > 0;
  el["comparison-table"].hidden = records.length === 0;
  if (!records.length) return;
  const fields = [
    ["מקור ואימות", (r) => `${r.verification} · ${r.sourceStatus}`], ["לשכה ומפקח/ת", (r) => `${r.office} · ${r.adjudicator}`],
    ["עובדות ותקציר", (r) => r.summary], ["נושאים", (r) => (r.categories || []).join("; ")],
    ["תוצאה אופרטיבית", (r) => r.outcome || "לא חולצה"], ["זיקה לאשדוד", (r) => r.ashdodRelation || "אין"],
    ["מצב בדיקה", (r) => r.reviewStatus],
  ];
  const cols = `180px repeat(${records.length}, minmax(230px, 1fr))`;
  let html = `<div class="comparison-grid" style="grid-template-columns:${cols}"><div class="comparison-cell comparison-head">שדה</div>`;
  html += records.map((record) => `<div class="comparison-cell comparison-head"><strong>${escapeHtml(record.caseNumber)}</strong><br><small>${escapeHtml(formatDate(record.date))}</small><br><button data-remove-compare="${escapeHtml(record.id)}">הסרה</button></div>`).join("");
  for (const [label, getter] of fields) {
    html += `<div class="comparison-cell comparison-label">${escapeHtml(label)}</div>`;
    html += records.map((record) => `<div class="comparison-cell"><p>${escapeHtml(getter(record) || "—")}</p></div>`).join("");
  }
  html += "</div>";
  el["comparison-table"].innerHTML = html;
}

function defaultWorkspace() {
  return { version: 1, title: "תיק עבודה", createdAt: new Date().toISOString(), updatedAt: new Date().toISOString(), claims: [] };
}

function newClaim(title = "טענה חדשה") {
  return { id: crypto.randomUUID(), title, fact: "", elements: "", evidence: "", missingEvidence: "", defenses: "", remedy: "", forum: "", warnings: "", notes: "", readiness: "חסר", authorities: [] };
}

const toBase64 = (bytes) => btoa(String.fromCharCode(...bytes));
const fromBase64 = (value) => Uint8Array.from(atob(value), (char) => char.charCodeAt(0));

async function deriveKey(passphrase, salt) {
  const material = await crypto.subtle.importKey("raw", new TextEncoder().encode(passphrase), "PBKDF2", false, ["deriveKey"]);
  return crypto.subtle.deriveKey({ name: "PBKDF2", salt, iterations: 180000, hash: "SHA-256" }, material, { name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]);
}

async function encryptWorkspace(workspace, passphrase) {
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const key = await deriveKey(passphrase, salt);
  const cipher = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, key, new TextEncoder().encode(JSON.stringify(workspace)));
  return { version: 1, kdf: "PBKDF2-SHA256-180000", cipher: "AES-256-GCM", salt: toBase64(salt), iv: toBase64(iv), data: toBase64(new Uint8Array(cipher)) };
}

async function decryptWorkspace(envelope, passphrase) {
  const key = await deriveKey(passphrase, fromBase64(envelope.salt));
  const plain = await crypto.subtle.decrypt({ name: "AES-GCM", iv: fromBase64(envelope.iv) }, key, fromBase64(envelope.data));
  return JSON.parse(new TextDecoder().decode(plain));
}

async function saveWorkspace() {
  if (!state.workspace || !state.workspacePassphrase) return;
  state.workspace.updatedAt = new Date().toISOString();
  localStorage.setItem(STORAGE_KEY, JSON.stringify(await encryptWorkspace(state.workspace, state.workspacePassphrase)));
  el["workspace-status"].textContent = `פתוח · נשמר מוצפן במכשיר · עודכן ${new Date().toLocaleTimeString("he-IL", { hour: "2-digit", minute: "2-digit" })}`;
}
const saveWorkspaceDebounced = debounce(() => saveWorkspace().catch(() => toast("שמירת התיק נכשלה")), 500);

function openSecureDialog() {
  const exists = Boolean(localStorage.getItem(STORAGE_KEY));
  state.secureMode = exists ? "unlock" : "create";
  el["secure-title"].textContent = exists ? "פתיחת סביבת התיק" : "יצירת סביבת תיק מוצפנת";
  el["secure-help"].textContent = exists ? "הקלידו את הסיסמה שבה הוצפן התיק במכשיר זה." : "בחרו סיסמה בת שמונה תווים לפחות. היא אינה נשמרת ולא ניתן לשחזר אותה.";
  el["confirm-label"].hidden = exists;
  el["workspace-passphrase-confirm"].hidden = exists;
  el["secure-error"].textContent = "";
  el["workspace-passphrase"].value = "";
  el["workspace-passphrase-confirm"].value = "";
  el["secure-dialog"].showModal();
  el["workspace-passphrase"].focus();
}

function lockWorkspace() {
  state.workspace = null;
  state.workspacePassphrase = null;
  renderWorkspace();
}

function authorityHtml(authority) {
  const record = state.recordsById.get(authority.recordId);
  if (!record) return "";
  return `<div class="authority-item ${authority.side}" data-authority-id="${escapeHtml(authority.id)}"><strong>${escapeHtml(citation(record))}</strong><small>${authority.side === "support" ? "תומכת" : "מחלישה / טענת הגנה"}${authority.page ? ` · עמ׳ ${escapeHtml(authority.page)}` : ""} · ${authority.verified ? "אומת מול PDF" : "טרם אומת"}</small>${authority.quote ? `<p>“${escapeHtml(authority.quote)}”</p>` : ""}<button type="button" data-remove-authority="${escapeHtml(authority.id)}">הסרה</button></div>`;
}

function renderWorkspace() {
  const open = Boolean(state.workspace);
  el["workspace-locked"].hidden = open;
  el["workspace-content"].hidden = !open;
  el["workspace-unlock"].hidden = open;
  el["workspace-lock"].hidden = !open;
  el["workspace-lock-icon"].textContent = open ? "🔓" : "🔒";
  if (!open) { el["workspace-status"].textContent = "הסביבה נעולה. שום פרט אינו נשלח או מתפרסם."; return; }
  const claims = state.workspace.claims || [];
  const ready = claims.filter((claim) => claim.readiness === "מוכן לבדיקה").length;
  const authorities = claims.reduce((sum, claim) => sum + (claim.authorities || []).length, 0);
  const verified = claims.reduce((sum, claim) => sum + (claim.authorities || []).filter((item) => item.verified).length, 0);
  el["readiness-strip"].innerHTML = [
    [claims.length, "טענות"], [authorities, "אסמכתאות"], [verified, "ציטוטים מאומתים"], [ready, "מוכנות לבדיקה"],
  ].map(([number, label]) => `<div class="readiness-card"><strong>${number}</strong><span>${label}</span></div>`).join("");
  if (!claims.length) {
    el["claims-list"].innerHTML = `<div class="state-card"><strong>אין עדיין טענות</strong><span>ייבאו את התיק המקומי או צרו טענה חדשה.</span></div>`;
    return;
  }
  el["claims-list"].innerHTML = claims.map((claim) => `<article class="claim-card" data-claim-id="${escapeHtml(claim.id)}">
    <div class="claim-head"><div><span class="eyebrow">טענה</span><h2>${escapeHtml(claim.title || "ללא כותרת")}</h2></div><button class="danger-button" type="button" data-remove-claim>מחיקה</button></div>
    <div class="claim-body">
      <div><label>כותרת</label><input data-field="title" value="${escapeHtml(claim.title)}"></div>
      <div><label>מצב</label><select data-field="readiness"><option ${claim.readiness === "חסר" ? "selected" : ""}>חסר</option><option ${claim.readiness === "בעבודה" ? "selected" : ""}>בעבודה</option><option ${claim.readiness === "מוכן לבדיקה" ? "selected" : ""}>מוכן לבדיקה</option></select></div>
      <div class="field-wide"><label>העובדה שיש להוכיח</label><textarea rows="3" data-field="fact">${escapeHtml(claim.fact)}</textarea></div>
      <div><label>יסודות העילה והסמכות</label><textarea rows="4" data-field="elements">${escapeHtml(claim.elements)}</textarea></div>
      <div><label>פורום</label><textarea rows="4" data-field="forum">${escapeHtml(claim.forum)}</textarea></div>
      <div><label>ראיות קיימות</label><textarea rows="5" data-field="evidence">${escapeHtml(claim.evidence)}</textarea></div>
      <div><label>ראיות חסרות</label><textarea rows="5" data-field="missingEvidence">${escapeHtml(claim.missingEvidence)}</textarea></div>
      <div><label>טענות הגנה צפויות</label><textarea rows="5" data-field="defenses">${escapeHtml(claim.defenses)}</textarea></div>
      <div><label>סעד מבוקש</label><textarea rows="5" data-field="remedy">${escapeHtml(claim.remedy)}</textarea></div>
      <div class="field-wide"><label>אזהרות: פיצול, כפל, ערעור ומגבלות</label><textarea rows="3" data-field="warnings">${escapeHtml(claim.warnings)}</textarea></div>
      <div class="field-wide"><label>הערות</label><textarea rows="3" data-field="notes">${escapeHtml(claim.notes)}</textarea></div>
      <div class="field-wide"><label>אסמכתאות</label><div class="authority-list">${(claim.authorities || []).map(authorityHtml).join("") || "טרם נוספו אסמכתאות."}</div></div>
    </div>
  </article>`).join("");
}

function ensureWorkspaceForAttach(record) {
  if (!state.workspace) {
    state.attachRecordId = record.id;
    switchView("workspace");
    openSecureDialog();
    toast("יש לפתוח את סביבת התיק לפני הוספת אסמכתה");
    return false;
  }
  if (!state.workspace.claims.length) state.workspace.claims.push(newClaim());
  return true;
}

function openAttachDialog(record, page = "", quote = "") {
  if (!ensureWorkspaceForAttach(record)) return;
  state.attachRecordId = record.id;
  el["attach-claim"].innerHTML = state.workspace.claims.map((claim) => `<option value="${escapeHtml(claim.id)}">${escapeHtml(claim.title)}</option>`).join("");
  el["attach-page"].value = page || "";
  el["attach-quote"].value = quote || "";
  el["attach-verified"].checked = false;
  el["attach-dialog"].showModal();
}

function downloadBlob(name, type, content) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const link = document.createElement("a");
  link.href = url; link.download = name; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function packageHtml() {
  const claims = state.workspace.claims.map((claim, index) => {
    const authorities = (claim.authorities || []).map((authority) => {
      const record = state.recordsById.get(authority.recordId);
      if (!record) return "";
      return `<li><strong>${escapeHtml(citation(record))}</strong> — ${authority.side === "support" ? "תומכת" : "מחלישה / הגנה"}${authority.page ? `, עמ׳ ${escapeHtml(authority.page)}` : ""}; ${authority.verified ? "אומת מול PDF" : "טרם אומת"}${authority.quote ? `<blockquote>${escapeHtml(authority.quote)}</blockquote>` : ""}</li>`;
    }).join("");
    const field = (label, value) => `<h3>${label}</h3><p>${escapeHtml(value || "—").replaceAll("\n", "<br>")}</p>`;
    return `<section><h2>${index + 1}. ${escapeHtml(claim.title)}</h2>${field("העובדה שיש להוכיח", claim.fact)}${field("יסודות העילה והסמכות", claim.elements)}${field("ראיות קיימות", claim.evidence)}${field("ראיות חסרות", claim.missingEvidence)}${field("טענות הגנה", claim.defenses)}${field("סעד", claim.remedy)}${field("פורום", claim.forum)}${field("אזהרות", claim.warnings)}<h3>אסמכתאות</h3><ol>${authorities || "<li>לא נוספו</li>"}</ol></section>`;
  }).join("");
  return `<!doctype html><html dir="rtl" lang="he"><head><meta charset="utf-8"><title>${escapeHtml(state.workspace.title)}</title><style>body{font-family:Arial;line-height:1.55;margin:2cm;color:#172b3a}h1{border-bottom:3px solid #0c6973;padding-bottom:10px}h2{page-break-before:auto;border-bottom:1px solid #bbb;padding-bottom:5px}h3{margin-bottom:3px}p{margin-top:0}blockquote{border-right:3px solid #b7792b;padding-right:12px;color:#333}section{page-break-inside:avoid;margin-bottom:28px}</style></head><body><h1>${escapeHtml(state.workspace.title)}</h1><p>חבילת עבודה מחקרית · הופקה ${new Date().toLocaleDateString("he-IL")} · יש לאמת כל מקור לפני הגשה.</p>${claims}</body></html>`;
}

function bindEvents() {
  document.querySelectorAll("[data-view-target]").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.viewTarget)));
  const queryChanged = debounce(() => {
    el["clear-query"].hidden = !el.query.value;
    state.page = 1;
    applyFilters();
    requestFullTextSearch(el.query.value);
  });
  el.query.addEventListener("input", queryChanged);
  el["clear-query"].addEventListener("click", () => { el.query.value = ""; el["clear-query"].hidden = true; requestFullTextSearch(""); el.query.focus(); });
  document.querySelectorAll("[data-lens]").forEach((button) => button.addEventListener("click", () => {
    state.lens = button.dataset.lens;
    document.querySelectorAll("[data-lens]").forEach((item) => item.classList.toggle("active", item === button));
    state.page = 1; applyFilters();
  }));
  ["office-filter", "municipality-filter", "adjudicator-filter", "year-filter", "category-filter", "type-filter", "status-filter", "full-text-filter", "ashdod-filter", "show-duplicates", "sort-select"].forEach((id) => {
    el[id].addEventListener("change", () => { state.page = 1; applyFilters(); });
  });
  el["reset-filters"].addEventListener("click", () => {
    el.query.value = "";
    ["office-filter", "municipality-filter", "adjudicator-filter", "year-filter", "category-filter", "type-filter", "status-filter"].forEach((id) => { el[id].value = ""; });
    ["full-text-filter", "ashdod-filter", "show-duplicates"].forEach((id) => { el[id].checked = false; });
    state.lens = ""; state.fullTextMatches.clear(); state.page = 1;
    document.querySelectorAll("[data-lens]").forEach((button) => button.classList.toggle("active", button.dataset.lens === ""));
    applyFilters();
  });
  el["mobile-filter-button"].addEventListener("click", () => {
    const open = el.filters.classList.toggle("open");
    el["mobile-filter-button"].setAttribute("aria-expanded", String(open));
  });
  el["result-list"].addEventListener("click", (event) => {
    const action = event.target.closest("[data-action]");
    if (!action) return;
    const record = state.recordsById.get(action.closest("[data-id]").dataset.id);
    if (action.dataset.action === "details") openDetails(record);
    if (action.dataset.action === "compare") toggleCompare(record.id);
    if (action.dataset.action === "attach") openAttachDialog(record, state.fullTextMatches.get(record.id)?.pages?.[0] || "");
    if (action.dataset.action === "copy") copyText(citation(record));
  });
  el.pagination.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-page]");
    if (!button || button.disabled) return;
    state.page = Number(button.dataset.page); renderResults(); document.getElementById("results-title").scrollIntoView();
  });
  document.querySelectorAll("[data-close-dialog]").forEach((button) => button.addEventListener("click", () => document.getElementById(button.dataset.closeDialog).close()));
  el["dialog-content"].addEventListener("click", (event) => {
    const button = event.target.closest("[data-dialog-action]"); if (!button) return;
    const record = state.recordsById.get(button.dataset.id);
    if (button.dataset.dialogAction === "copy") copyText(citation(record));
    if (button.dataset.dialogAction === "attach") openAttachDialog(record, button.dataset.page || "");
  });
  el["clear-comparison"].addEventListener("click", () => { state.compare.clear(); el["compare-count"].textContent = "0"; renderComparison(); renderResults(); });
  el["comparison-table"].addEventListener("click", (event) => { const button = event.target.closest("[data-remove-compare]"); if (button) toggleCompare(button.dataset.removeCompare); });
  el["workspace-unlock"].addEventListener("click", openSecureDialog);
  el["workspace-lock"].addEventListener("click", lockWorkspace);
  el["secure-form"].addEventListener("submit", async (event) => {
    event.preventDefault();
    const passphrase = el["workspace-passphrase"].value;
    if (passphrase.length < 8) { el["secure-error"].textContent = "נדרשת סיסמה בת שמונה תווים לפחות."; return; }
    if (state.secureMode === "create" && passphrase !== el["workspace-passphrase-confirm"].value) { el["secure-error"].textContent = "הסיסמאות אינן זהות."; return; }
    try {
      state.workspace = state.secureMode === "create" ? defaultWorkspace() : await decryptWorkspace(JSON.parse(localStorage.getItem(STORAGE_KEY)), passphrase);
      state.workspacePassphrase = passphrase;
      if (state.secureMode === "create") await saveWorkspace();
      el["secure-dialog"].close(); renderWorkspace();
      if (state.attachRecordId) { const record = state.recordsById.get(state.attachRecordId); state.attachRecordId = ""; if (record) openAttachDialog(record); }
    } catch { el["secure-error"].textContent = "הסיסמה אינה נכונה או שהקובץ המקומי פגום."; }
  });
  el["add-claim"].addEventListener("click", () => { state.workspace.claims.push(newClaim()); renderWorkspace(); saveWorkspaceDebounced(); });
  el["claims-list"].addEventListener("input", (event) => {
    const field = event.target.dataset.field; if (!field) return;
    const claim = state.workspace.claims.find((item) => item.id === event.target.closest("[data-claim-id]").dataset.claimId);
    claim[field] = event.target.value;
    if (field === "title") event.target.closest(".claim-card").querySelector("h2").textContent = event.target.value || "ללא כותרת";
    saveWorkspaceDebounced();
  });
  el["claims-list"].addEventListener("click", (event) => {
    const card = event.target.closest("[data-claim-id]"); if (!card) return;
    const claim = state.workspace.claims.find((item) => item.id === card.dataset.claimId);
    if (event.target.closest("[data-remove-claim]") && confirm("למחוק את הטענה ואת קישורי האסמכתאות שלה?")) {
      state.workspace.claims = state.workspace.claims.filter((item) => item.id !== claim.id); renderWorkspace(); saveWorkspaceDebounced();
    }
    const authorityButton = event.target.closest("[data-remove-authority]");
    if (authorityButton) { claim.authorities = claim.authorities.filter((item) => item.id !== authorityButton.dataset.removeAuthority); renderWorkspace(); saveWorkspaceDebounced(); }
  });
  el["attach-form"].addEventListener("submit", (event) => {
    event.preventDefault();
    const claim = state.workspace.claims.find((item) => item.id === el["attach-claim"].value);
    claim.authorities ||= [];
    claim.authorities.push({ id: crypto.randomUUID(), recordId: state.attachRecordId, side: el["attach-side"].value, page: el["attach-page"].value.trim(), quote: el["attach-quote"].value.trim(), verified: el["attach-verified"].checked });
    state.attachRecordId = ""; el["attach-dialog"].close(); renderWorkspace(); saveWorkspaceDebounced(); toast("האסמכתה נוספה לטענה");
  });
  el["import-seed"].addEventListener("click", () => el["seed-file"].click());
  el["seed-file"].addEventListener("change", async () => {
    try {
      const data = JSON.parse(await el["seed-file"].files[0].text());
      if (!Array.isArray(data.claims)) throw new Error("claims");
      state.workspace = { ...defaultWorkspace(), ...data, version: 1 };
      renderWorkspace(); await saveWorkspace(); toast("התיק המקומי יובא והוצפן");
    } catch { toast("קובץ הייבוא אינו תקין"); }
    el["seed-file"].value = "";
  });
  el["export-word"].addEventListener("click", () => downloadBlob("argument-package.doc", "application/msword;charset=utf-8", packageHtml()));
  el["print-package"].addEventListener("click", () => window.print());
  el["export-backup"].addEventListener("click", () => {
    const value = localStorage.getItem(STORAGE_KEY); if (!value) return;
    downloadBlob("legal-workspace-backup.json", "application/json", value); toast("הגיבוי המוצפן נשמר");
  });
  el["import-backup"].addEventListener("click", () => el["backup-file"].click());
  el["backup-file"].addEventListener("change", async () => {
    try {
      const envelope = JSON.parse(await el["backup-file"].files[0].text());
      state.workspace = await decryptWorkspace(envelope, state.workspacePassphrase);
      localStorage.setItem(STORAGE_KEY, JSON.stringify(envelope)); renderWorkspace(); toast("הגיבוי שוחזר");
    } catch { toast("לא ניתן לשחזר: הסיסמה או הקובץ אינם מתאימים"); }
    el["backup-file"].value = "";
  });
  el["delete-workspace"].addEventListener("click", () => {
    if (!confirm("למחוק לצמיתות את סביבת התיק המוצפנת מהמכשיר?")) return;
    localStorage.removeItem(STORAGE_KEY); lockWorkspace(); toast("המידע המקומי נמחק");
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "/" && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) { event.preventDefault(); switchView("search"); el.query.focus(); }
  });
}

function restoreSearchFromUrl() {
  const params = new URLSearchParams(location.search);
  const mappings = { q: "query", office: "office-filter", city: "municipality-filter", judge: "adjudicator-filter", year: "year-filter", topic: "category-filter", type: "type-filter", status: "status-filter" };
  Object.entries(mappings).forEach(([key, id]) => { if (params.get(key)) el[id].value = params.get(key); });
  state.lens = params.get("lens") || "";
  el["full-text-filter"].checked = params.get("fullText") === "1";
  el["ashdod-filter"].checked = params.get("ashdod") === "1";
  el["clear-query"].hidden = !el.query.value;
  document.querySelectorAll("[data-lens]").forEach((button) => button.classList.toggle("active", button.dataset.lens === state.lens));
}

async function init() {
  cacheElements(); bindEvents();
  try {
    const response = await fetch("data/catalog.json");
    if (!response.ok) throw new Error("catalog");
    state.catalog = await response.json();
    state.records = state.catalog.records;
    state.recordsById = new Map(state.records.map((record) => [record.id, record]));
    addOptions(el["office-filter"], state.catalog.offices);
    addOptions(el["municipality-filter"], state.catalog.municipalities);
    addOptions(el["adjudicator-filter"], state.catalog.adjudicators);
    addOptions(el["year-filter"], state.catalog.years);
    addOptions(el["category-filter"], state.catalog.categories);
    addOptions(el["type-filter"], state.catalog.decisionTypes);
    addOptions(el["status-filter"], state.catalog.sourceStatuses);
    el["corpus-status"].textContent = `${state.catalog.totalDocuments.toLocaleString("he-IL")} רשומות · ${state.catalog.fullTextDocuments.toLocaleString("he-IL")} עם טקסט מלא`;
    el["updated-at"].textContent = `עודכן ${formatDate(state.catalog.generatedAt.slice(0, 10))}`;
    el["corpus-facts"].innerHTML = `<span><strong>${state.catalog.totalDocuments.toLocaleString("he-IL")}</strong> רשומות</span><span><strong>${state.catalog.officialDocuments.toLocaleString("he-IL")}</strong> רשמיות</span><span><strong>${state.catalog.externalDocuments.toLocaleString("he-IL")}</strong> השלמות</span><span><strong>${state.catalog.fullTextDocuments.toLocaleString("he-IL")}</strong> טקסטים מלאים</span><span><strong>${state.catalog.ashdodOfficial}</strong> רשומות אשדוד רשמיות</span>`;
    restoreSearchFromUrl(); applyFilters(false);
    if (el.query.value) requestFullTextSearch(el.query.value);
    const initialView = location.hash.replace("#", "") || "search";
    switchView(["search", "compare", "workspace", "methodology"].includes(initialView) ? initialView : "search");
  } catch (error) {
    el["loading-state"].hidden = true; el["error-state"].hidden = false; el["corpus-status"].textContent = "המאגר אינו זמין";
  }
}

init();
