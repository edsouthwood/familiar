/* Session timeline viewer — browses sessions/<date>-timeline.log via the
 * /api/sessions and /api/sessions/<date>/timeline endpoints in server.py, and
 * lets any player amend an event after the game (see review-shared.js for the
 * shared player-name-tag + amend-POST helpers). Amending never rewrites the
 * original capture -- it appends a correction, which the server folds into
 * `current` on the next fetch. "Apply corrections to memory" triggers
 * /api/sessions/<date>/reprocess so amendments also reach game-context.md and
 * the cast list, not just this page. */

const datePickerRow = document.getElementById("date-picker-row");
const datePicker = document.getElementById("date-picker");
const statusEl = document.getElementById("status");
const eventsEl = document.getElementById("events");
const filterRow = document.getElementById("filter-row");
const filterEl = document.getElementById("filter");
const filterCount = document.getElementById("filter-count");
const reprocessRow = document.getElementById("reprocess-row");
const reprocessBtn = document.getElementById("reprocess-btn");
const reprocessStatus = document.getElementById("reprocess-status");

const ROLL_OUTCOMES = [
  ["", "no roll"],
  ["success", "success"],
  ["failure", "failure"],
  ["crit", "crit"],
  ["nat1", "natural 1"],
];

/* A long rest ends an in-game day, so getting one wrong shifts the whole
 * campaign's calendar. "no rest" is first and is what an untagged event shows,
 * which also makes removing a mis-heard rest the easy default. */
const REST_TYPES = [
  ["", "no rest"],
  ["short", "short rest"],
  ["long", "long rest — ends the day"],
];

let currentDate = null;

/* The session as the server sent it, kept whole.
 *
 * The filter draws from this rather than hiding rows in place, because the day
 * dividers are not rows -- they are drawn between them, from the run of days
 * in whatever is being shown. Filtering by `display: none` would leave "Day 12"
 * standing over a stretch with nothing under it. */
let allEvents = [];

async function loadIdentity() {
  try {
    // Shared with the navigation drawer, which wants the same document -- see
    // tableConfig() in table.js. This page used to fetch it a second time.
    const cfg = await tableConfig();
    if (cfg.name) {
      const heading = `${cfg.name}'s session timeline`;
      document.getElementById("page-title").textContent = heading;
      document.getElementById("heading").textContent = heading;
    }
  } catch (_) { /* keep the built-in defaults */ }
}

function formatTime(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

function rollLabel(roll) {
  if (!roll) return "";
  const names = { success: "success", failure: "failure", crit: "crit", nat1: "nat 1" };
  return `${roll.character} — ${names[roll.outcome] || roll.outcome}`;
}

/* One <datalist> for the page, filled once. Rebuilding it per amend form would
 * mean one fetch per event, and the party does not change while you are
 * reading a timeline. */
const PARTY_LIST_ID = "party-names";

async function loadPartyNames() {
  const list = document.createElement("datalist");
  list.id = PARTY_LIST_ID;
  document.body.appendChild(list);
  try {
    const res = await fetch(api("/party"));
    if (!res.ok) return;           // suggestions are a convenience, not a feature
    for (const character of (await res.json()).party || []) {
      if (!character.retired) list.appendChild(new Option(character.name));
    }
  } catch (_) { /* the field still works as free text */ }
}


function restLabel(rest) {
  return rest === "long" ? "long rest" : rest === "short" ? "short rest" : "";
}

function buildAmendForm(li, ev) {
  const form = document.createElement("div");
  form.className = "amend-form";

  const textarea = document.createElement("textarea");
  textarea.placeholder = "What actually happened?";
  textarea.value = ev.current;

  const rollRow = document.createElement("div");
  rollRow.className = "amend-roll-row";
  const charInput = document.createElement("input");
  charInput.type = "text";
  charInput.placeholder = "Character (optional)";
  charInput.value = ev.roll ? ev.roll.character : "";
  // Suggestions, not a closed list. Stats matches roll names against the party
  // exactly, so offering the real spellings here stops most mismatches before
  // they are written -- but it stays free text, because a guest at one session
  // is a real thing and losing their roll to a dropdown would be worse than an
  // unplaced row somebody can fix later.
  charInput.setAttribute("list", PARTY_LIST_ID);
  const outcomeSelect = document.createElement("select");
  for (const [value, label] of ROLL_OUTCOMES) {
    const opt = document.createElement("option");
    opt.value = value;
    opt.textContent = label;
    outcomeSelect.appendChild(opt);
  }
  outcomeSelect.value = ev.roll ? ev.roll.outcome : "";
  rollRow.append(charInput, outcomeSelect);

  const tagRow = document.createElement("div");
  tagRow.className = "amend-tag-row";
  const restSelect = document.createElement("select");
  for (const [value, label] of REST_TYPES) {
    const opt = document.createElement("option");
    opt.value = value;
    opt.textContent = label;
    restSelect.appendChild(opt);
  }
  restSelect.value = ev.rest || "";
  const weatherInput = document.createElement("input");
  weatherInput.type = "text";
  weatherInput.maxLength = 60;
  weatherInput.placeholder = "Weather (optional)";
  weatherInput.value = ev.weather || "";
  tagRow.append(restSelect, weatherInput);

  const actions = document.createElement("div");
  actions.className = "amend-actions";
  const saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.textContent = "Save correction";
  const cancelBtn = document.createElement("button");
  cancelBtn.type = "button";
  cancelBtn.className = "secondary";
  cancelBtn.textContent = "Cancel";
  const errEl = document.createElement("span");
  errEl.className = "amend-error";
  actions.append(saveBtn, cancelBtn, errEl);

  cancelBtn.addEventListener("click", () => form.remove());

  saveBtn.addEventListener("click", async () => {
    const text = textarea.value.trim();
    if (!text) { errEl.textContent = "Enter a correction first."; return; }
    let roll = null;
    if (outcomeSelect.value && charInput.value.trim()) {
      roll = { character: charInput.value.trim(), outcome: outcomeSelect.value };
    }
    /* Every tag is sent on every save, including as null. The server reads an
     * absent key as "no opinion" and an explicit null as "remove it" -- and
     * this form always has an opinion, because the reader can see all three
     * controls. Sending only the non-empty ones would make clearing a rest
     * impossible from the one place you would look for it. */
    saveBtn.disabled = true;
    errEl.textContent = "";
    try {
      const updated = await postAmend(
        api(`/sessions/${encodeURIComponent(currentDate)}/timeline/${encodeURIComponent(ev.id)}/amend`),
        {
          text,
          roll,
          rest: restSelect.value || null,
          weather: weatherInput.value.trim() || null,
        }
      );
      /* A changed rest moves every day divider below it, so patching this one
       * row would leave the rest of the page quietly wrong. Reload instead. */
      if ((updated.rest || "") !== (ev.rest || "")) {
        loadTimeline(currentDate);
      } else {
        replaceEvent(li, updated);
      }
    } catch (err) {
      errEl.textContent = err.message;
      saveBtn.disabled = false;
    }
  });

  form.append(textarea, rollRow, tagRow, actions);
  return form;
}

/* Put text into an element with every occurrence of `needle` marked.
 *
 * Text nodes and <mark> elements built through the DOM, never innerHTML. Every
 * word on this page was transcribed from a room by a speech-to-text engine and
 * rewritten by a model, and search.js refuses to put that near innerHTML for
 * exactly the same reason -- with the added twist here that the needle is
 * whatever the reader typed. */
function markInto(el, text, needle) {
  if (!needle) { el.textContent = text; return el; }
  const hay = text.toLowerCase();
  let from = 0;
  for (;;) {
    const at = hay.indexOf(needle, from);
    if (at === -1) { el.append(text.slice(from)); break; }
    if (at > from) el.append(text.slice(from, at));
    const mark = document.createElement("mark");
    mark.textContent = text.slice(at, at + needle.length);
    el.append(mark);
    from = at + needle.length;
  }
  return el;
}

function currentFilter() {
  return filterEl.value.trim().toLowerCase();
}

/* What the filter reads. The text of the event, plus the two tags that are
 * words rather than numbers -- who rolled, and the weather. Searching the
 * timestamp would match "12" against half the session. */
function eventMatches(ev, needle) {
  if (!needle) return true;
  return [ev.current, ev.weather, ev.roll && ev.roll.character]
    .filter(Boolean).join(" ").toLowerCase()
    .includes(needle);
}

function renderEvent(ev, needle) {
  const li = document.createElement("li");
  li.dataset.eventId = ev.id;
  if (ev.day) li.dataset.day = ev.day;

  const row = document.createElement("div");
  row.className = "event-row";
  const time = document.createElement("span");
  time.className = "event-time";
  time.textContent = formatTime(ev.t);
  const text = document.createElement("span");
  text.className = "event-text";
  markInto(text, ev.current, needle);
  row.append(time, text);
  if (ev.roll) {
    const badge = document.createElement("span");
    badge.className = `roll-badge roll-${ev.roll.outcome}`;
    badge.textContent = rollLabel(ev.roll);
    row.appendChild(badge);
  }
  if (ev.rest) {
    const badge = document.createElement("span");
    badge.className = `rest-badge rest-${ev.rest}`;
    badge.textContent = restLabel(ev.rest);
    row.appendChild(badge);
  }
  if (ev.weather) {
    const badge = document.createElement("span");
    badge.className = "weather-badge";
    badge.textContent = ev.weather;
    row.appendChild(badge);
  }
  li.appendChild(row);

  const meta = document.createElement("div");
  meta.className = "event-meta";
  const amendBtn = document.createElement("button");
  amendBtn.type = "button";
  amendBtn.className = "amend-toggle";
  amendBtn.textContent = "amend";
  amendBtn.addEventListener("click", () => {
    const existing = li.querySelector(".amend-form");
    if (existing) { existing.remove(); return; }
    li.appendChild(buildAmendForm(li, ev));
  });
  meta.appendChild(amendBtn);

  if (ev.amendments && ev.amendments.length) {
    const details = document.createElement("details");
    details.className = "amend-history";
    const summary = document.createElement("summary");
    summary.textContent = `amended ${ev.amendments.length > 1 ? `${ev.amendments.length} times` : "once"}`;
    details.appendChild(summary);

    const originalP = document.createElement("p");
    originalP.className = "amend-original";
    originalP.textContent = `Originally captured: "${ev.original}"`;
    details.appendChild(originalP);

    for (const a of ev.amendments) {
      const p = document.createElement("p");
      p.className = "amend-entry";
      p.textContent = `${a.text} — ${a.by}, ${formatAmendTime(a.at)}`;
      details.appendChild(p);
    }
    meta.appendChild(details);
  }
  li.appendChild(meta);
  return li;
}

function replaceEvent(li, ev) {
  /* The amend endpoint returns the event without a `day` -- it answers about
   * one event, and the day depends on the whole campaign. Carry the one the
   * row already had so the badge does not flicker away. */
  const fresh = renderEvent({ day: Number(li.dataset.day) || null, ...ev },
                            currentFilter());
  li.replaceWith(fresh);
}

function renderEvents(events) {
  allEvents = events;
  reprocessRow.hidden = !events.length;
  /* No filter box over an empty session: it would be a control whose only
     possible answer is "0 of 0". */
  filterRow.hidden = !events.length;
  if (!events.length) {
    eventsEl.innerHTML = "";
    filterCount.textContent = "";
    statusEl.textContent = "No events recorded for this session yet.";
    eventsEl.hidden = true;
    return;
  }
  paintEvents();
}

/* Draw whatever survives the filter. Called on load and on every keystroke. */
function paintEvents() {
  const needle = currentFilter();
  const shown = needle ? allEvents.filter((ev) => eventMatches(ev, needle)) : allEvents;

  eventsEl.innerHTML = "";
  filterCount.textContent = needle
    ? `${shown.length} of ${allEvents.length} event${allEvents.length === 1 ? "" : "s"}`
    : "";

  if (!shown.length) {
    eventsEl.hidden = true;
    statusEl.textContent = `Nothing in this session matches “${filterEl.value.trim()}”.`;
    return;
  }
  statusEl.textContent = "";
  eventsEl.hidden = false;
  /* Day dividers. `day` is worked out server-side because it depends on every
   * earlier session's rests, not just this one's -- a session that opens on
   * day 12 has no way of knowing that from its own events. Drawn from the
   * filtered run, so a day the filter emptied does not keep its heading. */
  let shownDay = null;
  for (const ev of shown) {
    if (ev.day && ev.day !== shownDay) {
      shownDay = ev.day;
      eventsEl.appendChild(dayDivider(ev.day));
    }
    eventsEl.appendChild(renderEvent(ev, needle));
  }
}

/* No debounce: this filters an array that is already in memory, so there is
   nothing to spare the network. Escape clears, because a filter you cannot get
   out of quickly is a filter you stop using. */
filterEl.addEventListener("input", paintEvents);
filterEl.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && filterEl.value) {
    filterEl.value = "";
    paintEvents();
  }
});

/* Bring one event into view and mark it, for arrivals from search. */
function highlight(eventId) {
  if (!eventId) return;
  const li = eventsEl.querySelector(`[data-event-id="${CSS.escape(eventId)}"]`);
  if (!li) return;
  li.classList.add("targeted");
  li.scrollIntoView({ block: "center", behavior: "smooth" });
}


function dayDivider(day) {
  const li = document.createElement("li");
  li.className = "day-divider";
  li.textContent = `Day ${day}`;
  return li;
}

async function loadTimeline(date) {
  currentDate = date;
  statusEl.textContent = "Loading…";
  eventsEl.hidden = true;
  reprocessStatus.textContent = "";
  try {
    const res = await fetch(api(`/sessions/${encodeURIComponent(date)}/timeline`));
    if (!res.ok) throw new Error(`server said ${res.status}`);
    const data = await res.json();
    renderEvents(data.events || []);
  } catch (err) {
    statusEl.textContent = `Couldn't load that session: ${err.message}`;
  }
}

reprocessBtn.addEventListener("click", async () => {
  if (!currentDate) return;
  reprocessBtn.disabled = true;
  reprocessStatus.textContent = "Reprocessing… this can take a little while.";
  try {
    const res = await fetch(api(`/sessions/${encodeURIComponent(currentDate)}/reprocess`),
                            { method: "POST", headers: { "Content-Type": "application/json" } });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `server said ${res.status}`);
    reprocessStatus.textContent = `Done — memory updated through ${data.reprocessed_dates[data.reprocessed_dates.length - 1]}.`;
  } catch (err) {
    reprocessStatus.textContent = `Couldn't reprocess: ${err.message}`;
  } finally {
    reprocessBtn.disabled = false;
  }
});

function formatDateLabel(date) {
  const d = new Date(`${date}T00:00:00`);
  if (isNaN(d)) return date;
  return d.toLocaleDateString(undefined, { weekday: "long", year: "numeric", month: "long", day: "numeric" });
}

async function init() {
  loadIdentity();
  statusEl.textContent = "Loading sessions…";
  let dates = [];
  try {
    const res = await fetch(api("/sessions"));
    const data = await res.json();
    dates = data.dates || [];
  } catch (err) {
    statusEl.textContent = `Couldn't reach the server: ${err.message}`;
    return;
  }

  if (!dates.length) {
    statusEl.textContent = "No sessions recorded yet — turn on \"record table talk\" in settings during a game to start building a timeline.";
    return;
  }

  datePickerRow.hidden = false;
  for (const date of dates) {
    const option = document.createElement("option");
    option.value = date;
    option.textContent = formatDateLabel(date);
    datePicker.appendChild(option);
  }
  datePicker.addEventListener("change", () => loadTimeline(datePicker.value));

  /* A search result links straight at one event: ?date=&event=. Fall back to
   * the newest session when the date is missing or no longer has a timeline,
   * so a stale link still lands somewhere useful rather than on an error. */
  const wanted = new URLSearchParams(location.search);
  const date = dates.includes(wanted.get("date")) ? wanted.get("date") : dates[0];
  datePicker.value = date;
  loadTimeline(date).then(() => highlight(wanted.get("event")));
}

loadPartyNames();
init();

