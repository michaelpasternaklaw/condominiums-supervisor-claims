const state = {
  catalog: null,
  records: [],
  visible: [],
  page: 1,
  pageSize: 18,
  fullTextMatches: null,
  fullTextSnippets: new Map(),
  searchRequestId: 0,
};

const el = {};
const worker = new Worker("search-worker.js");

const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

const normalize = (value) => String(value ?? "")
  .normalize("NFKD")
  .replace(/[\u0591-\u05C7]/g, "")
  .replace(/[\u200e\u200f\u202a-\u202e]/g, "")
  .replace(/[^\p{L}\p{N}]+/gu, " ")
  .trim()
  .toLocaleLowerCase("he");

const formatDate = (value) => {
  if (!value) return "ללא תאריך";
  const [year, month, day] = value.split("-");
  return `${day}.${month}.${year}`;
};

const debounce = (fn, delay = 220) => {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), delay);
  };
};

function cacheElements() {
  [
    "corpus-status", "query", "clear-query", "full-text-toggle", "search-progress",
    "office-filter", "year-filter", "category-filter", "type-filter", "sort-select",
    "result-count", "result-list", "pagination", "loading-state", "empty-state",
    "error-state", "reset-filters", "mobile-filter-button", "filters", "updated-at",
    "case-dialog", "dialog-close", "dialog-content",
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
      record.caseNumber, ...(record.caseAliases || []), record.date, record.type, record.tribunal,
      record.office, record.adjudicator, record.municipality, record.address, record.plaintiffs,
      record.defendants, record.representatives, ...(record.categories || []), ...(record.topics || []),
      record.summary, record.operativeExcerpt,
    ].join(" "));
  }
  return record._haystack;
}

function requestFullTextSearch(query) {
  const trimmed = query.trim();
  if (trimmed.length < 2) {
    state.fullTextMatches = null;
    state.fullTextSnippets.clear();
    el["search-progress"].textContent = "יש להקליד לפחות שני תווים";
    applyFilters();
    return;
  }
  state.searchRequestId += 1;
  state.fullTextMatches = new Set();
  state.fullTextSnippets.clear();
  el["search-progress"].textContent = "טוען אינדקס טקסט מלא…";
  worker.postMessage({ type: "search", query: trimmed, requestId: state.searchRequestId });
}

worker.onmessage = (event) => {
  const message = event.data;
  if (message.type === "progress") {
    el["search-progress"].textContent = `טוען טקסט מלא ${message.loaded}/${message.total}`;
    return;
  }
  if (message.requestId !== state.searchRequestId) return;
  if (message.type === "error") {
    el["search-progress"].textContent = "לא ניתן לטעון את החיפוש המלא";
    return;
  }
  state.fullTextMatches = new Set(message.matches.map((item) => item.id));
  state.fullTextSnippets = new Map(message.matches.map((item) => [item.id, item.snippet]));
  el["search-progress"].textContent = `החיפוש המלא הסתיים · ${message.matches.length} התאמות`;
  state.page = 1;
  applyFilters();
};

function applyFilters() {
  const query = el.query.value.trim();
  const tokens = normalize(query).split(/\s+/).filter(Boolean);
  const office = el["office-filter"].value;
  const year = el["year-filter"].value;
  const category = el["category-filter"].value;
  const type = el["type-filter"].value;
  const useFullText = el["full-text-toggle"].checked && tokens.length > 0;

  state.visible = state.records.filter((record) => {
    if (office && record.office !== office) return false;
    if (year && record.year !== year) return false;
    if (category && !(record.categories || []).includes(category)) return false;
    if (type && record.type !== type) return false;
    if (!tokens.length) return true;
    if (useFullText) return state.fullTextMatches ? state.fullTextMatches.has(record.id) : false;
    const haystack = metadataHaystack(record);
    return tokens.every((token) => haystack.includes(token));
  });

  const sort = el["sort-select"].value;
  state.visible.sort((left, right) => {
    if (sort === "date-asc") return left.date.localeCompare(right.date);
    if (sort === "case") return left.caseNumber.localeCompare(right.caseNumber, "he", { numeric: true });
    return right.date.localeCompare(left.date);
  });

  const maxPage = Math.max(1, Math.ceil(state.visible.length / state.pageSize));
  state.page = Math.min(state.page, maxPage);
  renderResults();
}

function resultCard(record) {
  const summary = record.summary || "אין תקציר זמין; ניתן לפתוח את פסק הדין המלא.";
  const tags = (record.categories || []).slice(0, 3).map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("");
  const snippet = state.fullTextSnippets.get(record.id);
  return `
    <article class="result-card" data-id="${escapeHtml(record.id)}">
      <div class="result-main">
        <div class="case-line">
          <span class="case-number">${escapeHtml(record.caseNumber)}</span>
          <span class="date-pill">${escapeHtml(formatDate(record.date))}</span>
          <span class="type-pill">${escapeHtml(record.type)}</span>
        </div>
        <h3>${escapeHtml(record.office)} · ${escapeHtml(record.adjudicator)}</h3>
        <p>${escapeHtml(summary)}</p>
        ${snippet ? `<p class="snippet">${escapeHtml(snippet)}</p>` : ""}
        <div class="meta-line">
          ${record.municipality ? `<span>יישוב: ${escapeHtml(record.municipality)}</span>` : ""}
          ${record.pages ? `<span>${escapeHtml(record.pages)} עמודים</span>` : ""}
          ${record.plaintiffs ? `<span>תובעים: ${escapeHtml(record.plaintiffs)}</span>` : ""}
        </div>
        ${tags ? `<div class="tags">${tags}</div>` : ""}
      </div>
      <div class="card-actions">
        <button type="button" data-action="details">פרטים</button>
        <a href="${escapeHtml(record.pdf)}" target="_blank" rel="noopener">פתח PDF</a>
      </div>
    </article>`;
}

function renderResults() {
  const start = (state.page - 1) * state.pageSize;
  const pageRecords = state.visible.slice(start, start + state.pageSize);
  el["result-count"].textContent = `${state.visible.length.toLocaleString("he-IL")} פסקי דין`;
  el["loading-state"].hidden = true;
  el["error-state"].hidden = true;
  el["empty-state"].hidden = state.visible.length !== 0;
  el["result-list"].innerHTML = pageRecords.map(resultCard).join("");
  renderPagination();
}

function renderPagination() {
  const totalPages = Math.ceil(state.visible.length / state.pageSize);
  if (totalPages <= 1) {
    el.pagination.innerHTML = "";
    return;
  }
  const buttons = [];
  buttons.push(`<button type="button" data-page="${state.page - 1}" ${state.page === 1 ? "disabled" : ""} aria-label="העמוד הקודם">‹</button>`);
  const from = Math.max(1, state.page - 2);
  const to = Math.min(totalPages, state.page + 2);
  if (from > 1) buttons.push(`<button type="button" data-page="1">1</button><span>…</span>`);
  for (let page = from; page <= to; page += 1) {
    buttons.push(`<button type="button" data-page="${page}" ${page === state.page ? 'aria-current="page"' : ""}>${page}</button>`);
  }
  if (to < totalPages) buttons.push(`<span>…</span><button type="button" data-page="${totalPages}">${totalPages}</button>`);
  buttons.push(`<button type="button" data-page="${state.page + 1}" ${state.page === totalPages ? "disabled" : ""} aria-label="העמוד הבא">›</button>`);
  el.pagination.innerHTML = buttons.join("");
}

function detailItem(label, value) {
  if (!value) return "";
  return `<div class="detail-item"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`;
}

function formatOperativeExcerpt(value) {
  const sections = String(value ?? "")
    .replace(/\r\n?/g, "\n")
    .replace(/\s*\n+\s*/g, "\n")
    .replace(/(^|[\s:;])(?:\d{1,2}\s+)?([א-ת])\s*\.\s*(?=[א-ת])/g, "$1\n$2. ")
    .replace(/\s+\.(\d{1,3})\s*(?=[א-ת])/g, "\n$1. ")
    .replace(/\s+(\d{1,3})\s*\.\s*(?=[א-ת])/g, "\n$1. ")
    .replace(/\s+\(([א-ת]|\d{1,3})\)\s*(?=[א-ת])/g, "\n($1) ")
    .split(/\n+/)
    .map((section) => section.trim())
    .filter(Boolean);

  return `<div class="operative-excerpt">${sections
    .map((section) => `<p>${escapeHtml(section)}</p>`)
    .join("")}</div>`;
}

function openDetails(record) {
  const categories = (record.categories || []).map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("");
  el["dialog-content"].innerHTML = `
    <div class="dialog-kicker">${escapeHtml(record.type)} · ${escapeHtml(formatDate(record.date))}</div>
    <h2 class="dialog-title" id="dialog-title">${escapeHtml(record.caseNumber)}</h2>
    <div class="dialog-subtitle">${escapeHtml(record.tribunal)}</div>
    <div class="dialog-summary">${escapeHtml(record.summary || "אין תקציר זמין")}</div>
    <div class="detail-grid">
      ${detailItem("לשכה", record.office)}
      ${detailItem("מפקח/ת", record.adjudicator)}
      ${detailItem("תובעים", record.plaintiffs)}
      ${detailItem("נתבעים", record.defendants)}
      ${detailItem("יישוב", record.municipality)}
      ${detailItem("כתובת", record.address)}
      ${detailItem("מספר עמודים", record.pages)}
      ${detailItem("ייצוג", record.representatives)}
    </div>
    ${categories ? `<div class="dialog-section"><h3>קטגוריות</h3><div class="tags">${categories}</div></div>` : ""}
    ${record.operativeExcerpt ? `<div class="dialog-section"><h3>קטע אופרטיבי לאיתור</h3>${formatOperativeExcerpt(record.operativeExcerpt)}</div>` : ""}
    <div class="dialog-section"><h3>מעמד המקור</h3><p>${escapeHtml(record.precedentialNote)}</p></div>
    <div class="dialog-actions">
      <a class="button" href="${escapeHtml(record.pdf)}" target="_blank" rel="noopener">פתח PDF</a>
      <a class="button secondary" href="${escapeHtml(record.pdf)}" download>הורד PDF</a>
      <a class="button tertiary" href="${escapeHtml(record.text)}" target="_blank" rel="noopener">פתח טקסט</a>
      ${record.sourceUrl ? `<a class="button tertiary" href="${escapeHtml(record.sourceUrl)}" target="_blank" rel="noopener">מקור רשמי</a>` : ""}
    </div>`;
  el["case-dialog"].showModal();
}

function resetFilters() {
  el.query.value = "";
  el["clear-query"].hidden = true;
  el["office-filter"].value = "";
  el["year-filter"].value = "";
  el["category-filter"].value = "";
  el["type-filter"].value = "";
  el["full-text-toggle"].checked = false;
  el["search-progress"].textContent = "";
  state.fullTextMatches = null;
  state.fullTextSnippets.clear();
  state.page = 1;
  history.replaceState({}, "", location.pathname);
  applyFilters();
}

function bindEvents() {
  const queryChanged = debounce(() => {
    el["clear-query"].hidden = !el.query.value;
    state.page = 1;
    const params = new URLSearchParams(location.search);
    if (el.query.value.trim()) params.set("q", el.query.value.trim()); else params.delete("q");
    history.replaceState({}, "", `${location.pathname}${params.size ? `?${params}` : ""}`);
    if (el["full-text-toggle"].checked) requestFullTextSearch(el.query.value); else applyFilters();
  });
  el.query.addEventListener("input", queryChanged);
  el["clear-query"].addEventListener("click", () => { el.query.value = ""; queryChanged(); el.query.focus(); });
  el["full-text-toggle"].addEventListener("change", () => {
    state.page = 1;
    if (el["full-text-toggle"].checked) requestFullTextSearch(el.query.value); else {
      state.fullTextMatches = null;
      state.fullTextSnippets.clear();
      el["search-progress"].textContent = "";
      applyFilters();
    }
  });
  ["office-filter", "year-filter", "category-filter", "type-filter", "sort-select"].forEach((id) => {
    el[id].addEventListener("change", () => { state.page = 1; applyFilters(); });
  });
  el["reset-filters"].addEventListener("click", resetFilters);
  el["mobile-filter-button"].addEventListener("click", () => {
    const open = el.filters.classList.toggle("open");
    el["mobile-filter-button"].setAttribute("aria-expanded", String(open));
  });
  el["result-list"].addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action='details']");
    if (!button) return;
    const card = button.closest("[data-id]");
    const record = state.records.find((item) => item.id === card.dataset.id);
    if (record) openDetails(record);
  });
  el.pagination.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-page]");
    if (!button || button.disabled) return;
    state.page = Number(button.dataset.page);
    renderResults();
    document.getElementById("results-title").scrollIntoView({ block: "start" });
  });
  el["dialog-close"].addEventListener("click", () => el["case-dialog"].close());
  el["case-dialog"].addEventListener("click", (event) => {
    if (event.target === el["case-dialog"]) el["case-dialog"].close();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "/" && document.activeElement.tagName !== "INPUT") {
      event.preventDefault();
      el.query.focus();
    }
  });
}

async function init() {
  cacheElements();
  bindEvents();
  try {
    const response = await fetch("data/catalog.json");
    if (!response.ok) throw new Error("catalog");
    state.catalog = await response.json();
    state.records = state.catalog.records;
    addOptions(el["office-filter"], state.catalog.offices);
    addOptions(el["year-filter"], state.catalog.years);
    addOptions(el["category-filter"], state.catalog.categories);
    addOptions(el["type-filter"], state.catalog.decisionTypes);
    el["corpus-status"].textContent = `${state.catalog.totalDocuments.toLocaleString("he-IL")} מסמכים ייחודיים · ${state.catalog.totalSourcePdfs.toLocaleString("he-IL")} קובצי מקור`;
    el["updated-at"].textContent = `עודכן ${formatDate(state.catalog.generatedAt.slice(0, 10))}`;
    const params = new URLSearchParams(location.search);
    const initialQuery = params.get("q") || "";
    el.query.value = initialQuery;
    el["clear-query"].hidden = !initialQuery;
    applyFilters();
  } catch (error) {
    el["loading-state"].hidden = true;
    el["error-state"].hidden = false;
    el["corpus-status"].textContent = "המאגר אינו זמין";
  }
}

init();
