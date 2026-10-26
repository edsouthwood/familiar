/* The den: every table you can sit at, and the door to a new one.
 *
 * This is the page that was missing. Before it, the app had exactly one
 * surface -- the familiar's screen -- and everything that was not "talk to the
 * familiar" had to be squeezed into the gear panel or reached by typing a URL.
 * The den is where a table is chosen, made, and administered, which is what
 * lets the gear panel go back to being about this device.
 */

async function load() {
  const res = await fetch("/api/me");
  if (!res.ok) { location.href = "/login.html"; return; }
  const data = await res.json();
  if (!data.user) { location.href = "/login.html"; return; }

  byId("who").textContent = `Signed in as ${data.user.email}`;
  render(data.tables || []);
}

function render(tables) {
  const list = byId("tables");
  list.textContent = "";
  byId("empty").hidden = tables.length > 0;
  // A DM with no tables yet has nothing to look at, so open the "new table"
  // form rather than making them find the disclosure triangle first.
  byId("new-table").open = tables.length === 0;

  for (const t of tables) {
    const li = document.createElement("li");

    const heading = document.createElement("div");
    const name = document.createElement("span");
    name.className = "table-name";
    name.textContent = t.name;
    heading.append(name);
    if (t.role === "dm") {
      const role = document.createElement("span");
      role.className = "role";
      role.textContent = "DM";
      heading.append(role);
    }
    li.append(heading);

    const links = document.createElement("div");
    links.className = "links";
    const base = `/t/${encodeURIComponent(t.slug)}/`;
    // Two doors, not six. The familiar's screen is what you open on a game
    // night; the hub is everything else, and it carries its own strip across
    // the timeline, cast, places and search. Listing all six here meant the
    // den had to be kept in step with a navigation that already exists.
    for (const [label, href] of [
      ["The familiar", base],
      ["The table", `${base}hub.html`],
    ]) {
      const a = document.createElement("a");
      a.href = href;
      a.textContent = label;
      links.append(a);
    }
    li.append(links);

    li.append(tableTools(t));
    list.append(li);
  }
}

/* Per-table controls. Collapsed by default -- on a game night you want to click
 * through to the familiar, not read a form.
 *
 * Two audiences, not one. The DM gets the invitation link and the bill. Anyone
 * who summoned a familiar gets its persona, whether they run the game or not --
 * the creature is theirs. The server decides both; this only chooses what to
 * ask for, so a wrong guess here is a missing panel, never a leak. */
/* A link, not an editor.
 *
 * There used to be two bare <textarea>s here -- the only place in the whole app
 * a persona or a campaign could be edited, buried inside a <details> inside a
 * list of tables. The den is where you *choose* a table; running one now has a
 * page of its own. See static/hub.js.
 */
function loadHubLink(slug, host) {
  host.textContent = "";
  const p = document.createElement("p");
  const a = document.createElement("a");
  a.href = `/t/${encodeURIComponent(slug)}/hub.html`;
  a.textContent = "The campaign, the party and this table's familiars →";
  p.append(a);
  host.append(p);
}

function tableTools(t) {
  const isDM = t.role === "dm";
  const details = document.createElement("details");
  const summary = document.createElement("summary");
  summary.textContent = isDM ? "Spend, and your data" : "Your data";
  details.append(summary);

  const invite = document.createElement("div");
  const spend = document.createElement("div");
  const persona = document.createElement("div");
  if (isDM) {
    invite.className = "note";
    invite.textContent = "Loading the invitation link…";
    details.append(invite, spend);
  }
  details.append(persona);

  details.append(yourData(t));

  // Fetched lazily: opening the den should cost one request, not one per table.
  details.addEventListener("toggle", () => {
    if (!details.open || details.dataset.loaded) return;
    details.dataset.loaded = "yes";
    if (isDM) {
      loadInvite(t.slug, invite);
      loadUsage(t.slug, spend);
    }
    loadHubLink(t.slug, persona);
  });
  return details;
}

/* Export, delete, leave.
 *
 * Deliberately in the ordinary panel rather than behind a "danger zone" -- a
 * red box at the bottom of a settings page is where people learn not to look.
 * Taking your evenings back out is a normal thing to want, and it should read
 * like one. The one destructive control asks you to type the table's name,
 * which the server checks too; the button is not the guard. */
function yourData(t) {
  const box = document.createElement("div");
  box.className = "your-data";

  const heading = document.createElement("p");
  heading.className = "note";
  heading.textContent = "Your data";
  box.append(heading);

  const base = `/api/t/${encodeURIComponent(t.slug)}`;
  const status = document.createElement("p");
  status.className = "error";

  if (t.owner || t.role === "dm") {
    const download = document.createElement("a");
    download.href = `${base}/export`;
    download.textContent = "Download everything this table has recorded";
    box.append(download);
  }

  if (t.owner) {
    const form = document.createElement("div");
    form.className = "row";
    const input = document.createElement("input");
    input.type = "text";
    input.placeholder = `Type ${t.slug} to delete`;
    input.setAttribute("aria-label", `Type ${t.slug} to confirm deletion`);
    const button = document.createElement("button");
    button.type = "button";
    button.className = "secondary";
    button.textContent = "Delete this table";
    button.addEventListener("click", async () => {
      if (!confirm(`Delete ${t.name}? Every session, correction and note goes `
                   + "with it, and this cannot be undone.")) return;
      button.disabled = true;
      status.textContent = "";
      try {
        await postJson(`${base}/delete`, { confirm: input.value.trim() });
        location.reload();
      } catch (err) {
        status.textContent = err.message;
        button.disabled = false;
      }
    });
    form.append(input, button);
    box.append(form);
  } else {
    const leave = document.createElement("button");
    leave.type = "button";
    leave.className = "secondary";
    leave.textContent = "Leave this table";
    leave.addEventListener("click", async () => {
      if (!confirm(`Leave ${t.name}? Corrections you made stay on the record.`)) return;
      leave.disabled = true;
      status.textContent = "";
      try {
        await postJson(`${base}/leave`, {});
        location.reload();
      } catch (err) {
        status.textContent = err.message;
        leave.disabled = false;
      }
    });
    box.append(leave);
  }

  const privacy = document.createElement("p");
  privacy.className = "note";
  const link = document.createElement("a");
  link.href = "/privacy.html";
  link.textContent = "What is kept, and where it goes";
  privacy.append(link);
  box.append(privacy, status);
  return box;
}

/* This month's spend. The figures are drawn by renderUsage (usage-view.js),
 * which the hub shows too; what differs here is only that the den asks by
 * slug, across every table you are at. */
async function loadUsage(slug, host) {
  try {
    const res = await fetch(`/api/t/${encodeURIComponent(slug)}/usage`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "could not load usage");
    renderUsage(host, data);
  } catch (err) {
    host.textContent = err.message;
  }
}

async function loadInvite(slug, host) {
  try {
    const res = await fetch(`/api/t/${encodeURIComponent(slug)}/invite`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "could not load the invitation");
    host.textContent = "";

    const label = document.createElement("p");
    label.className = "note";
    label.textContent = "Send this link to your players:";
    const link = document.createElement("p");
    link.className = "invite-link";
    link.textContent = new URL(data.url, location.origin).href;

    const row = document.createElement("div");
    row.className = "row";
    const rotate = document.createElement("button");
    rotate.type = "button";
    rotate.className = "secondary";
    rotate.textContent = "New link";
    rotate.title = "Stops the old link working — use it to un-invite someone";
    rotate.addEventListener("click", async () => {
      rotate.disabled = true;
      try {
        const fresh = await postJson(
          `/api/t/${encodeURIComponent(slug)}/invite/rotate`, {});
        link.textContent = new URL(fresh.url, location.origin).href;
      } finally {
        rotate.disabled = false;
      }
    });
    row.append(rotate);

    const who = document.createElement("p");
    who.className = "note";
    who.textContent = data.members.length
      ? `At the table: ${data.members.map((m) => m.name).join(", ")}`
      : "Nobody has joined yet.";

    host.append(label, link, row, who);
  } catch (err) {
    host.textContent = err.message;
  }
}

bindForm("/api/tables",
         () => ({
           name: byId("name").value.trim(),
           familiar: byId("familiar").value.trim(),
           slug: byId("slug").value.trim(),
         }),
         (table) => { location.href = `/t/${encodeURIComponent(table.slug)}/`; });

byId("sign-out").addEventListener("click", async () => {
  await postJson("/api/logout", {});
  location.href = "/login.html";
});

load();
