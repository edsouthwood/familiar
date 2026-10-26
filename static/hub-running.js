/* Running the table: who is here, the invitation, the bodies, the bill.
 *
 * DM-only, and the server says so on every route -- @require_member(role="dm").
 * What is here is administration of the table, which is the DM's; what is in
 * hub-familiar.js is the creature, which is its summoner's. That line is the
 * whole permission model. See hub-party.js for the shared-scope note.
 */

/* The panel. Members, the invitation, the bodies, the bill -- in that order,
 * which is roughly how often a DM touches them. */
async function loadRunning() {
  const host = document.getElementById("running-body");
  host.textContent = "";
  if (!ME || ME.role !== "dm") {
    const note = document.createElement("p");
    note.className = "note";
    note.textContent = "Only the DM runs the table.";
    host.append(note);
    return;
  }
  host.append(await membersBlock());
  host.append(await inviteBlock());
  host.append(await bodiesBlock());
  host.append(usageBlock());

  const link = document.createElement("p");
  link.className = "note";
  const den = document.createElement("a");
  den.href = "/den.html";
  // Export and delete deliberately stay in the den: it is the only page that
  // still works when a table is too broken to open.
  den.textContent = "Exporting or deleting this table? That lives in the den.";
  link.append(den);
  host.append(link);
}

/* What the table has spent this month. It used to be visible only in the den,
 * one table among several -- which is where you go to choose a table, not to
 * run one. Drawn by renderUsage (usage-view.js), the same as the den draws it.
 */
function usageBlock() {
  const section = block("This month");
  const body = document.createElement("div");
  body.textContent = "Loading…";
  section.append(body);
  apiGet("/usage")
    .then((data) => renderUsage(body, data))
    .catch((err) => { body.textContent = `Couldn't load the spend: ${err.message}`; });
  return section;
}

async function membersBlock() {
  const section = block("At the table");
  try {
    const data = await apiGet("/members");
    const list = document.createElement("ul");
    list.className = "card-list";
    for (const member of data.members) {
      const li = document.createElement("li");
      const row = document.createElement("div");
      row.className = "row";

      const name = document.createElement("input");
      name.type = "text";
      name.value = member.name;
      const role = document.createElement("span");
      role.className = member.role === "dm" ? "pill pill-on" : "pill";
      role.textContent = member.role === "dm" ? "DM" : "player";
      row.append(name, role);

      const rename = document.createElement("button");
      rename.type = "button";
      rename.className = "secondary";
      rename.textContent = "Rename";
      rename.addEventListener("click", async () => {
        try { await apiSend(`/members/${member.id}`, { name: name.value }, "PATCH"); }
        catch (err) { alert(err.message); }
      });
      row.append(rename);

      if (member.role !== "dm") {
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "secondary";
        remove.textContent = "Remove";
        remove.addEventListener("click", async () => {
          if (!confirm(`Remove ${member.name}? Corrections they made stay on the record.`)) return;
          try { await apiSend(`/members/${member.id}`, {}, "DELETE"); loadRunning(); }
          catch (err) { alert(err.message); }
        });
        row.append(remove);
      }
      li.append(row);
      list.append(li);
    }
    section.append(list);
  } catch (err) {
    const note = document.createElement("p");
    note.className = "note";
    note.textContent = `Couldn't load the members: ${err.message}`;
    section.append(note);
  }
  return section;
}

/* Bodies: the puppets paired to this table.
 *
 * A body is not a person and cannot be invited: it can't follow a link and
 * type a name. So the DM mints a short code here, reads it out to whoever is
 * at the Pi, and the Pi swaps it for a token of its own. The code is the only
 * secret that passes through a human, which is why it is short, single-use and
 * dead in fifteen minutes.
 */
async function bodiesBlock() {
  const section = block("Bodies");

  const note = document.createElement("p");
  note.className = "note";
  note.textContent = "A puppet — the cat with the servos — holds the same "
    + "state the screen does. Pair one here; forget it here if it goes.";
  section.append(note);

  const list = document.createElement("ul");
  list.className = "card-list";
  section.append(list);

  const pair = document.createElement("button");
  pair.type = "button";
  pair.className = "secondary";
  pair.textContent = "Pair a puppet";
  pair.addEventListener("click", async () => {
    pair.disabled = true;
    try { await apiSend("/devices", {}); await refresh(); }
    catch (err) { alert(err.message); }
    finally { pair.disabled = false; }
  });
  section.append(pair);

  async function refresh() {
    list.textContent = "";
    let devices;
    try {
      devices = (await apiGet("/devices")).devices;
    } catch (err) {
      const failed = document.createElement("li");
      failed.className = "note";
      failed.textContent = `Couldn't load the bodies: ${err.message}`;
      list.append(failed);
      return;
    }
    if (!devices.length) {
      const empty = document.createElement("li");
      empty.className = "note";
      empty.textContent = "No bodies paired. The screen is a body too — this is "
        + "only for the ones with servos in them.";
      list.append(empty);
      return;
    }
    for (const device of devices) list.append(bodyCard(device, refresh));
  }

  await refresh();
  return section;
}

function bodyCard(device, refresh) {
  const li = document.createElement("li");
  const row = document.createElement("div");
  row.className = "row";

  const name = document.createElement("strong");
  name.textContent = device.name;
  const state = document.createElement("span");
  state.className = "note";
  state.textContent = device.paired
    ? (device.lastSeenAt ? `last seen ${new Date(device.lastSeenAt).toLocaleString()}`
                         : "paired, never connected")
    : "waiting to be paired";
  row.append(name, state);

  const forget = document.createElement("button");
  forget.type = "button";
  forget.className = "secondary";
  forget.textContent = "Forget";
  forget.addEventListener("click", async () => {
    if (!confirm(`Forget ${device.name}? It stops working the next time it `
                 + "connects, and pairing it again means a new code.")) return;
    try { await apiSend(`/devices/${device.id}`, {}, "DELETE"); refresh(); }
    catch (err) { alert(err.message); }
  });
  row.append(forget);
  li.append(row);

  // The code, and the exact line to type at the Pi. Spelling out the command
  // is the difference between a feature and an evening with the documentation.
  if (!device.paired && device.code) {
    const code = document.createElement("p");
    code.className = "pair-code";
    code.textContent = device.code;
    li.append(code);

    const how = document.createElement("p");
    how.className = "note";
    how.textContent = "On the Pi, run:";
    li.append(how);

    const command = document.createElement("pre");
    command.className = "pair-command";
    command.textContent =
      `python body_client.py --pair ${device.code} --host ${location.origin}`;
    li.append(command);

    const expires = document.createElement("p");
    expires.className = "note";
    expires.textContent = device.expiresAt
      ? `This code works once, and only until ${new Date(device.expiresAt).toLocaleTimeString()}.`
      : "This code works once.";
    li.append(expires);
  }
  return li;
}

async function inviteBlock() {
  const section = block("Invitation");
  // Behind its own disclosure: the member list above is something you check
  // with people watching, and the link is the secret that lets anyone in.
  const details = document.createElement("details");
  const summary = document.createElement("summary");
  summary.textContent = "Show the invitation link";
  details.append(summary);

  const line = document.createElement("p");
  line.className = "note";
  line.textContent = "Loading…";
  details.append(line);

  details.addEventListener("toggle", async () => {
    if (!details.open || details.dataset.loaded) return;
    details.dataset.loaded = "yes";
    try {
      const data = await apiGet("/invite");
      line.textContent = location.origin + data.url;
      const rotate = document.createElement("button");
      rotate.type = "button";
      rotate.className = "secondary";
      rotate.textContent = "New link";
      rotate.addEventListener("click", async () => {
        if (!confirm("Make a new link? The old one stops working immediately.")) return;
        try {
          const fresh = await apiSend("/invite/rotate", {});
          line.textContent = location.origin + fresh.url;
        } catch (err) { alert(err.message); }
      });
      details.append(rotate);
    } catch (err) {
      line.textContent = `Couldn't load the link: ${err.message}`;
    }
  });
  section.append(details);
  return section;
}

