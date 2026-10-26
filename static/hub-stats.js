/* The dice panel.
 *
 * Was the bottom half of the party page; it is its own tab now, because what
 * the party has rolled is a different question from who the party is -- and
 * because this is where the rest of a character's numbers will go when there
 * are any. See hub-party.js for the shared-scope note.
 */

/* --------------------------------------------------------------- the dice -- */

/* Redraw only if this panel has been drawn. A character edit moves the dice,
 * but if nobody has opened the dice yet there is nothing to keep in step --
 * showPanel will load them fresh when they are first asked for. */
function refreshStats() { if (loaded.stats) loadStats(); }

async function loadStats() {
  const scroll = document.getElementById("stats-scroll");
  const body = document.getElementById("stats-body");
  const note = document.getElementById("stats-note");
  const unplacedEl = document.getElementById("unplaced");
  try {
    const data = await apiGet("/stats");
    const characters = data.characters || [];
    const unplaced = data.unplaced || [];
    body.textContent = "";
    scroll.hidden = !characters.length;
    note.textContent = characters.length ? "" : "No party recorded yet.";

    for (const character of characters) {
      const tr = document.createElement("tr");
      if (character.retired) tr.className = "retired";
      const t = character.tally;
      for (const value of [character.name, t.success, t.failure, t.crit, t.nat1]) {
        const td = document.createElement("td");
        td.textContent = value;
        tr.append(td);
      }
      body.append(tr);
    }
    renderUnplaced(unplacedEl, unplaced, characters);
  } catch (err) {
    note.textContent = `Couldn't load the dice: ${err.message}`;
  }
}

/* Rolls whose name matches nobody.
 *
 * There will be some the first time a party is written, because the tags carry
 * whatever the familiar heard. That is the moment every alias gets set, so the
 * control that fixes it sits right here next to the problem rather than inside
 * an edit form somebody has to think to open. */
function renderUnplaced(host, unplaced, characters) {
  host.textContent = "";
  host.hidden = !unplaced.length;
  if (!unplaced.length) return;

  const heading = document.createElement("p");
  heading.className = "note";
  heading.textContent = unplaced.length === 1
    ? "One roll we couldn't place. Who was it?"
    : `${unplaced.length} rolls we couldn't place. Who were they?`;
  host.append(heading);

  for (const row of unplaced) {
    const line = document.createElement("div");
    line.className = "row";
    const t = row.tally;
    const n = t.success + t.failure + t.crit + t.nat1;

    const name = document.createElement("span");
    name.className = "unplaced-name";
    name.textContent = `${row.name} (${n} roll${n === 1 ? "" : "s"})`;

    const picker = document.createElement("select");
    picker.append(new Option("this is…", ""));
    for (const character of characters) picker.append(new Option(character.name, character.id));
    const error = document.createElement("span");
    error.className = "amend-error";

    picker.addEventListener("change", async () => {
      if (!picker.value) return;
      picker.disabled = true;
      error.textContent = "";
      try {
        // Read the current aliases first: this adds one, it does not replace
        // the set, and there may be others already taught.
        const party = await apiGet("/party");
        const target = (party.party || []).find((p) => p.id === picker.value);
        const aliases = [...(target ? target.aliases : []), row.name];
        await apiSend(`/party/${encodeURIComponent(picker.value)}`, { aliases }, "PUT");
        loadStats();      // the alias moves a roll onto a character
        refreshParty();
      } catch (err) {
        error.textContent = err.message;
        picker.disabled = false;
      }
    });

    line.append(name, picker, error);
    host.append(line);
  }
}
