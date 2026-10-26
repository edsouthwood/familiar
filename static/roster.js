/* The browse-and-amend list shared by the cast page and the places page.
 *
 * Both render the same server object -- see server.Roster, which is one class
 * serving cast.json and locations.json -- so they are one renderer here too,
 * parameterised by which endpoint it reads and which key the list arrives
 * under. The rule it exists to keep consistent is the display one: the
 * machine's `auto` text is never shown once a human has amended an entry, but
 * it is never destroyed either, and every correction stays visible with who
 * made it.
 */

function createRoster({ path, key, listEl, statusEl, empty, noun }) {
  function buildAmendForm(li, entry) {
    const form = document.createElement("div");
    form.className = "amend-form";

    const textarea = document.createElement("textarea");
    textarea.placeholder = "What should this say instead?";
    textarea.value = entry.current;

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
      saveBtn.disabled = true;
      errEl.textContent = "";
      try {
        const updated = await postAmend(
          api(`${path}/${encodeURIComponent(entry.id)}/amend`), { text });
        li.replaceWith(render(updated));
      } catch (err) {
        errEl.textContent = err.message;
        saveBtn.disabled = false;
      }
    });

    form.append(textarea, actions);
    return form;
  }

  function render(entry) {
    const li = document.createElement("li");

    const name = document.createElement("div");
    name.className = "entry-name";
    name.textContent = entry.name;
    li.appendChild(name);

    const desc = document.createElement("div");
    desc.className = "entry-desc";
    desc.textContent = entry.current;
    li.appendChild(desc);

    const meta = document.createElement("div");
    meta.className = "event-meta";
    const amendBtn = document.createElement("button");
    amendBtn.type = "button";
    amendBtn.className = "amend-toggle";
    amendBtn.textContent = "amend";
    amendBtn.addEventListener("click", () => {
      const existing = li.querySelector(".amend-form");
      if (existing) { existing.remove(); return; }
      li.appendChild(buildAmendForm(li, entry));
    });
    meta.appendChild(amendBtn);

    if (entry.amendments && entry.amendments.length) {
      const details = document.createElement("details");
      details.className = "amend-history";
      const summary = document.createElement("summary");
      summary.textContent = `amended ${entry.amendments.length > 1 ? `${entry.amendments.length} times` : "once"}`;
      details.appendChild(summary);
      for (const a of entry.amendments) {
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

  async function load() {
    statusEl.textContent = `Loading ${noun}…`;
    listEl.hidden = true;
    try {
      const res = await fetch(api(path));
      if (!res.ok) throw new Error(`server said ${res.status}`);
      const data = await res.json();
      const entries = data[key] || [];
      listEl.innerHTML = "";
      if (!entries.length) {
        statusEl.textContent = empty;
        return;
      }
      statusEl.textContent = "";
      listEl.hidden = false;
      for (const entry of entries) listEl.appendChild(render(entry));
    } catch (err) {
      statusEl.textContent = `Couldn't load the ${noun}: ${err.message}`;
    }
  }

  return { load, render };
}
