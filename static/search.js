/* Search page — one box over everything the table has recorded.
 *
 * The server hands back a snippet with the matched span wrapped in two control
 * characters rather than in <mark> tags, and this file turns those into real
 * elements through the DOM. That is not fussiness: every word in the index was
 * transcribed from a room by a speech-to-text engine and rewritten by an LLM,
 * and none of it is ever going near innerHTML.
 *
 * The query lives in the URL (?q=), so a result worth showing somebody is a
 * link you can send them.
 */

const formEl = document.getElementById("search-form");
const inputEl = document.getElementById("q");
const filtersEl = document.getElementById("filters");
const statusEl = document.getElementById("status");
const resultsEl = document.getElementById("results");

const KIND_LABELS = {
  event: "timeline",
  npc: "cast",
  place: "place",
  qa: "asked",
};

let activeKind = "";
let marks = ["", ""];
let pending = null;

function linkFor(result) {
  const [date, ref] = (result.ref || "").split("/");
  switch (result.kind) {
    case "event":
      return { href: `timeline.html?date=${encodeURIComponent(date)}&event=${encodeURIComponent(ref)}`,
               label: "in the timeline" };
    case "npc":
      return { href: "cast.html", label: "in the cast" };
    case "place":
      return { href: "locations.html", label: "in places" };
    default:
      // A question has no page of its own. The snippet is the whole result,
      // so it gets no link rather than one that goes somewhere unhelpful.
      return null;
  }
}

/* Split the snippet on the server's two marker characters into plain text
 * nodes and <mark> elements. */
function renderSnippet(snippet) {
  const [start, end] = marks;
  const el = document.createElement("div");
  el.className = "result-snippet";
  let rest = snippet || "";
  if (!start || !end) { el.textContent = rest; return el; }
  while (rest) {
    const open = rest.indexOf(start);
    if (open === -1) { el.append(rest); break; }
    if (open > 0) el.append(rest.slice(0, open));
    const close = rest.indexOf(end, open + 1);
    if (close === -1) { el.append(rest.slice(open + 1)); break; }
    const mark = document.createElement("mark");
    mark.textContent = rest.slice(open + 1, close);
    el.append(mark);
    rest = rest.slice(close + 1);
  }
  return el;
}

function renderResult(result) {
  const li = document.createElement("li");

  const head = document.createElement("div");
  head.className = "result-head";
  const kind = document.createElement("span");
  kind.className = "kind-badge";
  kind.textContent = KIND_LABELS[result.kind] || result.kind;
  head.append(kind);
  if (result.title) {
    const title = document.createElement("span");
    title.className = "result-title";
    title.textContent = result.title;
    head.append(title);
  }
  if (result.date) {
    const date = document.createElement("span");
    date.className = "result-date";
    date.textContent = result.date;
    head.append(date);
  }
  li.append(head);

  li.append(renderSnippet(result.snippet));

  const link = linkFor(result);
  if (link) {
    const a = document.createElement("a");
    a.className = "result-link";
    a.href = link.href;
    a.textContent = link.label;
    li.append(a);
  }
  return li;
}

function renderFilters(counts) {
  filtersEl.textContent = "";
  const kinds = Object.keys(counts).filter((k) => counts[k] > 0);
  const total = kinds.reduce((n, k) => n + counts[k], 0);
  // One kind of thing matched: a filter that can only ever say "all" is noise.
  filtersEl.hidden = kinds.length < 2;
  if (filtersEl.hidden) return;

  for (const [kind, label, n] of [["", "Everything", total],
                                  ...kinds.map((k) => [k, KIND_LABELS[k] || k, counts[k]])]) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "tab-btn" + (kind === activeKind ? " active" : "");
    btn.textContent = `${label} (${n})`;
    btn.addEventListener("click", () => {
      activeKind = kind;
      run(inputEl.value, { push: false });
    });
    filtersEl.append(btn);
  }
}

async function run(query, { push = true } = {}) {
  query = (query || "").trim();
  if (push) {
    const url = query ? `?q=${encodeURIComponent(query)}` : location.pathname;
    history.replaceState(null, "", url);
  }
  if (!query) {
    resultsEl.hidden = true;
    filtersEl.hidden = true;
    statusEl.textContent = "";
    return;
  }

  statusEl.textContent = "Searching…";
  try {
    const params = new URLSearchParams({ q: query });
    if (activeKind) params.set("kind", activeKind);
    const res = await fetch(api(`/search?${params}`));
    if (!res.ok) throw new Error(`server said ${res.status}`);
    const data = await res.json();
    if (Array.isArray(data.marks) && data.marks.length === 2) marks = data.marks;

    renderFilters(data.counts || {});
    resultsEl.textContent = "";
    const results = data.results || [];
    if (!results.length) {
      resultsEl.hidden = true;
      statusEl.textContent = `Nothing matches “${query}”.`;
      return;
    }
    statusEl.textContent = "";
    resultsEl.hidden = false;
    for (const result of results) resultsEl.append(renderResult(result));
  } catch (err) {
    resultsEl.hidden = true;
    statusEl.textContent = `Couldn't search: ${err.message}`;
  }
}

/* Search as you type, but not on every keystroke: a 250ms settle keeps a fast
 * typist to one request per word rather than one per letter. */
inputEl.addEventListener("input", () => {
  clearTimeout(pending);
  pending = setTimeout(() => run(inputEl.value), 250);
});
formEl.addEventListener("submit", (e) => {
  e.preventDefault();
  clearTimeout(pending);
  run(inputEl.value);
});

async function loadIdentity() {
  try {
    // Shared with the navigation drawer, which wants the same document -- see
    // tableConfig() in table.js. This page used to fetch it a second time.
    const cfg = await tableConfig();
    if (cfg.name) {
      const heading = `Search ${cfg.name}'s memory`;
      document.getElementById("page-title").textContent = heading;
      document.getElementById("heading").textContent = heading;
    }
  } catch (_) { /* keep the built-in defaults */ }
}

loadIdentity();
const initial = new URLSearchParams(location.search).get("q") || "";
if (initial) { inputEl.value = initial; run(initial, { push: false }); }

