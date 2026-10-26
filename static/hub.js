/* The table's own page: the campaign, the party, the creature, and running the
 * thing.
 *
 * This is the surface that did not exist. Everything here used to live either
 * in the gear panel on the familiar's screen -- a tablet propped at the end of
 * a table, which is the worst possible place to edit prose -- or inside a
 * <details> in the den's list of tables, which is where you go to *choose* a
 * table, not to run one.
 *
 * Five panels now, one per tab in the drawer, switched by the hash so they are
 * linkable and the back button works. Everything is lazy per panel: opening
 * the hub should cost the requests for what you are looking at, not for
 * everything it can show. The panels themselves live in hub-party.js,
 * hub-stats.js, hub-familiar.js and hub-running.js; this file is the shell
 * they share -- apiGet, field, block, and the switching.
 *
 * It loads LAST of the set, because init() runs as it parses.
 */

const statusEl = document.getElementById("status");
const PANELS = ["campaign", "characters", "stats", "familiar", "running"];

/* What each panel is called at the top of the page and in the browser's
 * history. Without it five panels share one title and the back button gives
 * you five identical entries. */
const PANEL_TITLES = {
  campaign: "The campaign",
  characters: "The party",
  stats: "Dice",
  familiar: "The familiar",
  running: "Running the table",
};

/* Where the two old panel names land. #overview and #party are in people's
 * history and in the odd link; they cost two lines to keep working. */
const PANEL_ALIASES = { overview: "campaign", party: "characters" };

let TABLE = null;      // /config's table document
let ME = null;         // { id, name, role }
const loaded = {};     // panel -> true, so switching back does not refetch

/* GET one of this table's endpoints. An expired session must bounce to the
 * login page rather than render as an empty table -- handleUnauthorized
 * (table.js) is what every other table page already does on a 401; the throw
 * after it only stops the caller painting while the redirect happens. */
async function apiGet(path) {
  const res = await fetch(api(path));
  if (handleUnauthorized(res)) throw new Error("signed out — heading to the login page");
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `server said ${res.status}`);
  return data;
}

function panelFromHash() {
  const wanted = (location.hash || "").replace("#", "");
  if (PANELS.includes(wanted)) return wanted;
  return PANEL_ALIASES[wanted] || "campaign";
}

function showPanel(name) {
  for (const panel of PANELS) {
    document.getElementById(panel).hidden = panel !== name;
  }
  const title = PANEL_TITLES[name] || "The table";
  const table = TABLE && TABLE.name ? TABLE.name : "The table";
  document.getElementById("heading").textContent = title;
  document.getElementById("page-title").textContent = `${title} — ${table}`;
  renderTableNav();
  if (!loaded[name]) {
    loaded[name] = true;
    if (name === "campaign") { loadGlance(); loadCampaign(); }
    if (name === "characters") loadParty();
    if (name === "stats") loadStats();
    if (name === "familiar") loadFamiliars();
    if (name === "running") loadRunning();
  }
}

/* A panel that has already been drawn, redrawn. Used where one panel's edit
 * moves another's numbers -- an alias or a retirement changes the dice -- so
 * the stale one is thrown away rather than refreshed behind your back. */
function invalidatePanel(name) { delete loaded[name]; }

/* ------------------------------------------------------------ at a glance -- */

async function loadGlance() {
  const host = document.getElementById("at-a-glance");
  const rows = [];
  // Four small reads rather than one aggregate endpoint. They are all cheap,
  // they already exist, and a new endpoint whose only job is to save three
  // requests on a page nobody opens mid-session is not worth the surface.
  const [calendar, sessions, cast, places] = await Promise.all(
    ["/calendar", "/sessions", "/cast", "/locations"].map((path) =>
      fetch(api(path))
        .then((r) => (handleUnauthorized(r) || !r.ok ? null : r.json()))
        .catch(() => null)));

  const familiar = TABLE && TABLE.familiarName;
  if (familiar) rows.push(["Bound familiar", familiar]);
  if (calendar) rows.push(["In-game day", String(calendar.current_day)]);
  if (sessions && sessions.dates && sessions.dates.length) {
    rows.push(["Sessions recorded", String(sessions.dates.length)]);
    // The endpoint sorts newest-first (timeline.js leans on the same fact).
    rows.push(["Last session", sessions.dates[0]]);
  }
  if (cast) rows.push(["People met", String((cast.npcs || []).length)]);
  if (places) rows.push(["Places", String((places.places || []).length)]);

  host.textContent = "";
  /* Each pair in its own <div> -- valid inside a <dl>, and what lets the list
     become a row of tiles rather than a two-column grid of small grey text
     where "Sessions recorded" and "14" carried the same weight. */
  for (const [label, value] of rows) {
    const stat = document.createElement("div");
    stat.className = "stat";
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value;
    stat.append(dt, dd);
    host.append(stat);
  }
}

/* -------------------------------------------------------------- the notes -- */

let campaignRevision = "";

async function loadCampaign() {
  const host = document.getElementById("campaign-fields");
  const note = document.getElementById("campaign-note");
  const actions = document.getElementById("campaign-actions");
  host.textContent = "";
  try {
    const data = await apiGet("/campaign");
    campaignRevision = data.revision;

    note.textContent = data.mayEdit
      ? "The familiar reads these before every answer."
      : "Written by the DM. The familiar reads these before every answer.";
    actions.hidden = !data.mayEdit;

    // One form, one save button, and the fields never collapse individually.
    // "Where we are" is the thing a DM changes most and it is first, because
    // on a phone the field you want should not be a scroll away.
    const hints = data.hints || {};
    for (const [key, label] of Object.entries(data.labels)) {
      host.append(field(key, label, data.fields[key], data.mayEdit, false, hints[key]));
    }
    if ("dmOnly" in data) {
      host.append(field("dmOnly", "Only you can see this", data.dmOnly, true, true));
    }
    if ("passPhrase" in data) host.append(passPhraseField(data.passPhrase));
  } catch (err) {
    note.textContent = `Couldn't load the notes: ${err.message}`;
  }
}

function field(key, label, value, editable, secret = false, hint = "") {
  const wrap = document.createElement("label");
  wrap.className = secret ? "field secret" : "field";
  const title = document.createElement("span");
  title.className = "field-label";
  title.textContent = secret ? `🔒 ${label}` : label;
  wrap.append(title);

  if (secret) {
    const why = document.createElement("span");
    why.className = "field-hint";
    // Said plainly because it is not the same promise as the other boxes.
    why.textContent = "Never sent to the session-notes summariser. The familiar "
      + "only sees these when a question includes your pass-phrase — without "
      + "one set below, it never sees them at all. Anything it says aloud "
      + "afterwards is in the session log like every other answer.";
    wrap.append(why);
  } else if (hint) {
    // What this section is for, from campaign.py -- above the box rather than
    // under it, because it is meant to be read before you start typing.
    const why = document.createElement("span");
    why.className = "field-hint";
    why.textContent = hint;
    wrap.append(why);
  }

  if (!editable) {
    const shown = document.createElement("div");
    shown.className = "field-readonly";
    shown.textContent = value || "—";
    wrap.append(shown);
    return wrap;
  }
  const area = document.createElement("textarea");
  area.className = "prose";
  area.value = value || "";
  area.dataset.field = key;
  autoGrowTextarea(area);
  wrap.append(area);
  return wrap;
}

/* The phrase that lets the secrets above into a question's prompt. A line, not
 * a box: it is something you say, and the server compares it as words, so
 * capitals and punctuation do not matter. */
function passPhraseField(value) {
  const wrap = document.createElement("label");
  wrap.className = "field secret";
  const title = document.createElement("span");
  title.className = "field-label";
  title.textContent = "🔒 Pass-phrase";
  const why = document.createElement("span");
  why.className = "field-hint";
  why.textContent = "Say it in a question to let the familiar use your secrets "
    + "for that one answer. At least six letters; leave empty to keep them "
    + "from it entirely.";
  const input = document.createElement("input");
  input.type = "text";
  input.autocomplete = "off";
  input.value = value || "";
  input.dataset.field = "passPhrase";
  wrap.append(title, why, input);
  return wrap;
}

document.getElementById("campaign-save").addEventListener("click", async () => {
  const button = document.getElementById("campaign-save");
  const status = document.getElementById("campaign-status");
  const areas = [...document.querySelectorAll("#campaign-fields textarea")];
  const fields = {};
  let dmOnly;
  for (const area of areas) {
    if (area.dataset.field === "dmOnly") dmOnly = area.value;
    else fields[area.dataset.field] = area.value;
  }
  button.disabled = true;
  status.textContent = "";
  try {
    const body = { fields, revision: campaignRevision };
    if (dmOnly !== undefined) body.dmOnly = dmOnly;
    const phrase = document.querySelector('#campaign-fields input[data-field="passPhrase"]');
    if (phrase) body.passPhrase = phrase.value;
    const data = await apiSend("/campaign", body, "PUT");
    campaignRevision = data.revision;
    // Show what the file now says, canonical headings and all -- a save is
    // also the reload.
    for (const area of areas) {
      if (area.dataset.field in data.fields) area.value = data.fields[area.dataset.field];
    }
    status.textContent = "Saved. The familiar will know from its next answer.";
  } catch (err) {
    status.textContent = err.message;
  }
  button.disabled = false;
});

/* A textarea that is as tall as what is in it.
 *
 * Shared by the campaign notes and the persona, which are the two places on
 * this page where somebody writes paragraphs rather than a line. The measure
 * has to happen while the element is in the document -- scrollHeight on a
 * detached node is 0, and both callers build their form before appending it --
 * so the first sizing is deferred a frame rather than run here.
 *
 * The `height = auto` before each read is not redundant: scrollHeight cannot
 * report a content height smaller than the height already set, so without it
 * the box would grow and never shrink again when text is deleted.
 *
 * The +2 is the border. box-sizing is border-box everywhere here, so the
 * height we set has to include the two hairlines that scrollHeight leaves out;
 * without it every box creeps a scrollbar. */
function autoGrowTextarea(area) {
  const fit = () => {
    area.style.height = "auto";
    area.style.height = `${area.scrollHeight + 2}px`;
  };
  area.addEventListener("input", fit);
  requestAnimationFrame(fit);
  return fit;
}

function block(title) {
  const section = document.createElement("section");
  section.className = "block";
  const h = document.createElement("h3");
  h.textContent = title;
  section.append(h);
  return section;
}


/* ------------------------------------------------------------------ boot -- */

async function init() {
  try {
    // tableConfig() (table.js) rather than apiGet: the navigation drawer needs
    // the same document to decide which tabs to offer, and this page used to
    // fetch it twice on its own account.
    const cfg = await tableConfig();
    TABLE = { ...cfg.table, familiarName: cfg.name };
    ME = cfg.me;
  } catch (_) { /* the panels still work; only the name is missing */ }

  showPanel(panelFromHash());
  window.addEventListener("hashchange", () => showPanel(panelFromHash()));
}

init();
