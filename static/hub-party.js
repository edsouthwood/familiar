/* The party panel: who is at the table, in the fiction.
 *
 * Split out of hub.js when the hub grew from two panels to five. These files
 * are written to share one lexical scope on purpose -- classic <script> tags
 * have no other kind -- so every name in here is unique across the set, and
 * `apiGet`, `field` and `block` come from hub.js, which loads last because it
 * boots.
 */

/* -------------------------------------------------------------- the party -- */

function refreshParty() { if (loaded.characters) loadParty(); }

async function loadParty() {
  const list = document.getElementById("party-list");
  list.textContent = "";
  try {
    const data = await apiGet("/party");
    document.getElementById("party-add").hidden = !data.mayCreate;
    if (!(data.party || []).length) {
      const empty = document.createElement("p");
      empty.className = "note";
      empty.textContent = data.mayCreate
        ? "Nobody yet. Add the characters and the familiar will know who is at the table — and the dice below will find their owners."
        : "Nobody recorded yet.";
      list.append(empty);
      return;
    }
    for (const character of data.party) list.append(renderCharacter(character));
  } catch (err) {
    statusEl.textContent = `Couldn't load the party: ${err.message}`;
  }
}

/* `notes` is labelled Background because that is what it is. It already goes
 * into the prompt on every answer -- Party.prompt_text appends it to the line
 * the familiar is shown for each character -- so it is the character's
 * background as far as the creature is concerned, and a second prose field
 * beside it would be either a duplicate or dead to the model. */
const CHARACTER_FIELDS = [
  ["name", "Name"], ["class", "Class"], ["pronouns", "Pronouns"],
  ["player", "Played by"], ["aliases", "Also called"], ["notes", "Background"],
];

/* The same sheet, minus the name -- every character has one of those, so it is
 * never a gap. Used to say on the card what the card is not yet being told. */
const CHARACTER_GAPS = CHARACTER_FIELDS.filter(([key]) => key !== "name");

function characterGaps(character) {
  return CHARACTER_GAPS.filter(([key]) => (
    key === "aliases" ? !(character.aliases || []).length : !character[key]
  ));
}

/* One way into the form, for the edit button and for every gap chip.
 *
 * A chip also says which box it wants. Somebody who clicked "Pronouns" should
 * land in the pronouns field, not at the top of a six-field form with the
 * cursor nowhere -- on a phone that is the difference between one tap and one
 * tap plus a scroll and a second tap. */
function openCharacterForm(li, character, focusKey) {
  const open = li.querySelector(".amend-form");
  if (open) {
    const wanted = focusKey && open.querySelector(`[data-field="${focusKey}"]`);
    if (wanted) { wanted.focus(); return; }
    open.remove();                      // the edit button, pressed a second time
    return;
  }
  const form = characterForm(li, character);
  li.append(form);
  const wanted = focusKey && form.querySelector(`[data-field="${focusKey}"]`);
  if (wanted) wanted.focus();
}

/* What this sheet has room for and has not been told.
 *
 * The card only ever drew the fields that had something in them, so a
 * character with nothing but a name rendered as a name and an "edit" link --
 * and the only way to discover that the sheet also holds a class, pronouns, a
 * player, the other names the familiar might hear them called, and a
 * background the creature reads before every answer was to open the form and
 * read it. Those fields are named here instead, on the card, before the click.
 */
function characterGapRow(li, character) {
  const gaps = characterGaps(character);
  if (!gaps.length) return null;

  const row = document.createElement("div");
  row.className = "gaps";
  const label = document.createElement("span");
  label.className = "gaps-label";
  label.textContent = character.mayEdit ? "Not recorded yet — add" : "Not recorded yet";
  row.append(label);

  for (const [key, title] of gaps) {
    if (!character.mayEdit) {
      const span = document.createElement("span");
      span.className = "gap";
      span.textContent = title;
      row.append(span);
      continue;
    }
    const button = document.createElement("button");
    button.type = "button";
    button.className = "gap";
    button.textContent = title;
    button.addEventListener("click", () => openCharacterForm(li, character, key));
    row.append(button);
  }
  return row;
}

function renderCharacter(character) {
  const li = document.createElement("li");
  if (character.retired) li.classList.add("retired");

  const name = document.createElement("div");
  name.className = "entry-name";
  name.textContent = character.name + (character.retired ? " (retired)" : "");
  li.append(name);

  const desc = document.createElement("div");
  desc.className = "entry-desc";
  desc.textContent = [character.class, character.pronouns,
                      character.player && `played by ${character.player}`]
    .filter(Boolean).join(" · ");
  li.append(desc);

  if (character.aliases.length) {
    const also = document.createElement("div");
    also.className = "note";
    also.textContent = `Also called: ${character.aliases.join(", ")}`;
    li.append(also);
  }
  if (character.notes) {
    const notes = document.createElement("div");
    notes.className = "entry-desc";
    notes.textContent = character.notes;
    li.append(notes);
  }
  if (character.member_id && ME && character.member_id === ME.id) {
    const yours = document.createElement("div");
    yours.className = "note";
    yours.textContent = "Yours to edit.";
    li.append(yours);
  }

  const gapRow = characterGapRow(li, character);
  if (gapRow) li.append(gapRow);

  if (character.mayEdit) {
    const meta = document.createElement("div");
    meta.className = "event-meta";
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "amend-toggle";
    edit.textContent = "edit";
    edit.addEventListener("click", () => openCharacterForm(li, character));
    meta.append(edit);
    li.append(meta);
  }
  return li;
}

function characterForm(li, character) {
  const form = document.createElement("div");
  form.className = "amend-form";
  const inputs = {};

  for (const [key, label] of CHARACTER_FIELDS) {
    const wrap = document.createElement("label");
    wrap.className = "field";
    const title = document.createElement("span");
    title.className = "field-label";
    title.textContent = label;
    wrap.append(title);
    const input = key === "notes"
      ? document.createElement("textarea")
      : document.createElement("input");
    if (key !== "notes") input.type = "text";
    // How a gap chip finds the box it named -- see openCharacterForm.
    input.dataset.field = key;
    input.value = key === "aliases"
      ? (character.aliases || []).join(", ")
      : (character[key] || "");
    if (key === "notes") {
      // The same long-form treatment the campaign notes and the persona get:
      // this is prose the familiar reads in full before every single answer,
      // and it was being typed into a six-row box that never changed size.
      // `rows` is gone with it -- .prose sets the height it opens at and
      // autoGrowTextarea owns it from there, so a third opinion would only
      // be one more thing to keep in step.
      input.className = "prose";
      autoGrowTextarea(input);
      const why = document.createElement("span");
      why.className = "field-hint";
      why.textContent = "Who they are, where they came from, what they want — "
        + "and how they behave towards the familiar, which is the part that "
        + "changes how it answers them. Written as prose, not notes: the "
        + "creature reads this before every answer and treats it as something "
        + "it already knows about them.";
      wrap.append(why);
    }
    if (key === "aliases") {
      input.placeholder = "El, the princess";
      const why = document.createElement("span");
      why.className = "field-hint";
      // The reason this box exists, said where somebody will read it.
      why.textContent = "Other names the familiar might hear. Dice rolls are "
        + "matched exactly, so this is how a mis-heard name finds its owner.";
      wrap.append(why);
    }
    wrap.append(input);
    inputs[key] = input;
    form.append(wrap);
  }

  // Whose character is this, in the world outside the fiction?
  //
  // `member_id` is what makes a character editable by the player who plays it
  // (server.py, _may_edit_pc), and until now nothing in the browser had ever
  // set it -- so every character on every table was DM-only and "edit your own
  // character" was not a thing anybody could do. It is DM-only to set, because
  // a player who could set it could grant themselves the permission.
  const linkSelect = ME && ME.role === "dm" ? document.createElement("select") : null;
  if (linkSelect) {
    const wrap = document.createElement("label");
    wrap.className = "field";
    const title = document.createElement("span");
    title.className = "field-label";
    title.textContent = "Their account";
    const why = document.createElement("span");
    why.className = "field-hint";
    why.textContent = "Who at this table may edit this character. Yours to set.";
    linkSelect.append(new Option("nobody yet — DM only", ""));
    apiGet("/members")
      .then((data) => {
        for (const member of data.members || []) {
          linkSelect.append(new Option(member.name, member.id));
        }
        linkSelect.value = character.member_id || "";
      })
      .catch(() => { /* the rest of the form still saves */ });
    wrap.append(title, why, linkSelect);
    form.append(wrap);
  }

  const actions = document.createElement("div");
  actions.className = "amend-actions";
  const save = document.createElement("button");
  save.type = "button";
  save.textContent = "Save";
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "secondary";
  cancel.textContent = "Cancel";
  cancel.addEventListener("click", () => form.remove());
  const error = document.createElement("span");
  error.className = "amend-error";
  actions.append(save, cancel);

  // Retiring, not deleting. A character who died is not a mistake, and their
  // dice history is part of the campaign.
  const retire = document.createElement("button");
  retire.type = "button";
  retire.className = "secondary";
  retire.textContent = character.retired ? "Bring back" : "Retire";
  retire.addEventListener("click", async () => {
    await save_(li, character.id, { retired: !character.retired }, error, retire);
  });
  actions.append(retire, error);

  save.addEventListener("click", async () => {
    const body = {};
    for (const [key] of CHARACTER_FIELDS) {
      body[key] = key === "aliases"
        ? inputs[key].value.split(",").map((a) => a.trim()).filter(Boolean)
        : inputs[key].value;
    }
    if (linkSelect) body.member_id = linkSelect.value;
    await save_(li, character.id, body, error, save);
  });

  form.append(actions);
  return form;
}

async function save_(li, id, body, error, button) {
  button.disabled = true;
  error.textContent = "";
  try {
    const updated = await apiSend(`/party/${encodeURIComponent(id)}`, body, "PUT");
    updated.mayEdit = true;   // they just edited it
    li.replaceWith(renderCharacter(updated));
    refreshStats();           // aliases and retirement both move the numbers
  } catch (err) {
    error.textContent = err.message;
    button.disabled = false;
  }
}

document.getElementById("party-add-btn").addEventListener("click", async () => {
  const name = prompt("What is the character called?");
  if (!name || !name.trim()) return;
  try {
    await apiSend("/party", { name: name.trim() });
    loadParty();
    refreshStats();
  } catch (err) {
    statusEl.textContent = err.message;
  }
});
