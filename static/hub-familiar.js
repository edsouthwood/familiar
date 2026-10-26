/* The familiar panel: which creatures this table has, and who they are.
 *
 * It used to be a link inside a <details> inside the overview -- three
 * disclosures deep, on a page whose own tab was called something else. A
 * familiar's persona is the most personal thing in the app and it was the
 * hardest thing in the app to find.
 *
 * Only what you may open is listed as editable: your own familiars, and -- for
 * the DM -- the table's unowned one. A DM has no route into a familiar somebody
 * else summoned (familiars.py, Familiar.may_edit), so the persona link is not
 * offered for one. See hub-party.js for the shared-scope note.
 */

/* The creatures, drawn rather than typed.
 *
 * The same reasoning theme.js gives for its sun and moon and nav.js for its
 * three stripes, and it applies harder here: 🐱 and 🦉 render at wildly
 * different weights across the platforms this runs on, and on two of them they
 * arrive as full-colour emoji that ignore the palette completely -- a bright
 * orange cartoon head beside a name set in --text-soft. These are strokes in
 * currentColor, so a familiar's mark is the same weight and colour as its name
 * and follows the theme with it.
 *
 * Keyed by shell id, which is what /shells returns and what the picker in the
 * card sets. Anything without a drawing yet falls back to the paw, so adding a
 * shell to the server is never blocked on somebody drawing an icon for it. */
const FAMILIAR_ICONS = {
  cat:
    '<path d="M4.7 11.9C4.7 9 6.3 6.9 8.6 6L6.2 2.3 11.2 5.3'
    + 'C11.7 5.2 12.3 5.2 12.8 5.3L17.8 2.3 15.4 6C17.7 6.9 19.3 9 19.3 11.9'
    + ' 19.3 16.1 16.1 19.5 12 19.5S4.7 16.1 4.7 11.9Z"/>'
    + '<path d="M9.5 11.1v1.4M14.5 11.1v1.4"/>'
    + '<path d="M11.1 14.7h1.8L12 15.9Z"/>',
  owl:
    '<path d="M12 2.9C16.2 2.9 19.4 6.3 19.4 10.7'
    + 'C19.4 16 16.2 20.5 12 20.5 7.8 20.5 4.6 16 4.6 10.7'
    + 'C4.6 6.3 7.8 2.9 12 2.9Z"/>'
    + '<path d="M7 4.9 5.6 2.4 8.9 3.7M17 4.9 18.4 2.4 15.1 3.7"/>'
    + '<circle cx="9.3" cy="10" r="2.3"/><circle cx="14.7" cy="10" r="2.3"/>'
    + '<path d="M9.3 9.9v.2M14.7 9.9v.2"/>'
    + '<path d="M12 11.9 10.9 13.7h2.2Z"/>',
  bat:
    '<path d="M12 8.2c2 0 3.4 1.6 3.4 3.6S14 15.6 12 15.6 8.6 13.8 8.6 11.8 10 8.2 12 8.2Z"/>'
    + '<path d="M10 8.8 9.2 4.4l2.7 2M14 8.8l.8-4.4-2.7 2"/>'
    + '<path d="M9.1 10.9 5 7.9C3.2 6.6 1.2 7.3 1.1 9.2c1.3-.1 1.9.5 1.8 1.7 1.2-.4 2 .2 2.1 1.4 1.2-.6 2.5-1 4.1-1.4Z"/>'
    + '<path d="M14.9 10.9 19 7.9c1.8-1.3 3.8-.6 3.9 1.3-1.3-.1-1.9.5-1.8 1.7-1.2-.4-2 .2-2.1 1.4-1.2-.6-2.5-1-4.1-1.4Z"/>'
    + '<path d="M10.9 11.5v.7M13.1 11.5v.7"/>',
  crab:
    '<path d="M12 9.6c4 0 6.7 2.1 6.7 4.7s-2.7 4.5-6.7 4.5-6.7-1.9-6.7-4.5S8 9.6 12 9.6Z"/>'
    + '<path d="M5.6 12.9 3.9 11.5c-1.3-1.1-3-.5-3.1 1.1 0 1.3 1.3 2.1 2.5 1.6"/>'
    + '<path d="M18.4 12.9l1.7-1.4c1.3-1.1 3-.5 3.1 1.1 0 1.3-1.3 2.1-2.5 1.6"/>'
    + '<path d="M6.4 16.6 4 18.2M7.1 18.2l-1.8 2M17.6 16.6l2.4 1.6M16.9 18.2l1.8 2"/>'
    + '<path d="M9.9 9.8V6.6M14.1 9.8V6.6"/>'
    + '<circle cx="9.9" cy="5.3" r="1.6"/>'
    + '<circle cx="14.1" cy="5.3" r="1.6"/>',
  fish:
    '<path d="M6.4 12 2.1 7.5c-.9 2.9-.9 6.1 0 9L6.4 12Z"/>'
    + '<path d="M6 12c0-3.6 3.5-6.5 8-6.5s7.7 2.9 7.7 6.5-3.2 6.5-7.7 6.5S6 15.6 6 12Z"/>'
    + '<path d="M12.2 6.1c.6-1.9 2.1-3.1 3.8-3.3-.7 1.3-1.1 2.4-1.1 3.3"/>'
    + '<circle cx="17.6" cy="10.4" r="1.3"/>',
  frog:
    '<path d="M12 9.7c5 0 8.6 2.2 8.6 5.1s-3.6 4.7-8.6 4.7-8.6-1.8-8.6-4.7 3.6-5.1 8.6-5.1Z"/>'
    + '<path d="M5.7 13.7c3.5 2.7 9.1 2.7 12.6 0"/>'
    + '<circle cx="7.6" cy="7.5" r="3"/>'
    + '<circle cx="16.4" cy="7.5" r="3"/>'
    + '<path d="M7.6 7.2v.6M16.4 7.2v.6"/>',
  hawk:
    '<path d="M12 4.3c4.4 0 7.8 3.2 7.8 7.2s-3.4 7.1-7.8 7.1-7.8-3.1-7.8-7.1S7.6 4.3 12 4.3Z"/>'
    + '<path d="M6.5 8.3 11.1 10M17.5 8.3 12.9 10"/>'
    + '<path d="M12 12.5 9.9 15.2c1.3 1 2.9 1 4.2 0Z"/>'
    + '<path d="M12 15.4c.7 1.5-.4 2.4-1.5 1.7"/>'
    + '<circle cx="9.3" cy="11.3" r="1.3"/>'
    + '<circle cx="14.7" cy="11.3" r="1.3"/>',
  lizard:
    '<path d="M4.6 14.8c0-2.6 3.1-4.3 6.5-4.3 3.6 0 6.1 1 7.1 2.5.8 1.2.4 3.2-2 4.2-3.5 1.5-9.4.6-10.6-1Z"/>'
    + '<path d="M17.2 13c2.6-.2 5.5.4 5.9 1.6.2 1-1.8 1.9-4.7 1.7-1.6-.1-2.5-1.2-2-2.3Z"/>'
    + '<path d="M5.1 14.5c-2.6.1-4.3-1.4-4.9-3.5"/>'
    + '<path d="M8.3 18v2.6M14 18.2v2.6"/>'
    + '<path d="M7 11.3 8.1 9.4l1 2 1.3-2.2 1.2 2.2 1.4-2 1.2 2"/>'
    + '<circle cx="19.2" cy="14.2" r="1.1"/>',
  octopus:
    '<path d="M12 2.7c4.2 0 7.2 3.4 7.2 7.8 0 4-3 6.7-7.2 6.7s-7.2-2.7-7.2-6.7c0-4.4 3-7.8 7.2-7.8Z"/>'
    + '<path d="M6.6 15.5c-1.2 1.7-1.8 3.5-1.6 5.1M9.3 16.9c-.6 1.7-.8 3.5-.6 4.9"/>'
    + '<path d="M14.7 16.9c.6 1.7.8 3.5.6 4.9M17.4 15.5c1.2 1.7 1.8 3.5 1.6 5.1"/>'
    + '<circle cx="9.4" cy="10.4" r="1.9"/>'
    + '<circle cx="14.6" cy="10.4" r="1.9"/>',
  rat:
    '<circle cx="7" cy="6.7" r="3.2"/>'
    + '<circle cx="17" cy="6.7" r="3.2"/>'
    + '<path d="M12 8c3.8 0 6.7 2.6 6.7 5.6S15.8 19 12 19s-6.7-2.4-6.7-5.4S8.2 8 12 8Z"/>'
    + '<path d="M12 13.9c1.6 0 2.9 1 2.9 2.2s-1.3 2-2.9 2-2.9-.8-2.9-2 1.3-2.2 2.9-2.2Z"/>'
    + '<path d="M9.4 12.4v.6M14.6 12.4v.6"/>'
    + '<circle cx="12" cy="15.6" r=".9"/>',
  raven:
    '<path d="M10.6 5.3c3.8 0 6.7 3 6.7 6.8s-2.9 6.6-6.7 6.6S3.9 15.8 3.9 12s2.9-6.7 6.7-6.7Z"/>'
    + '<path d="M16.5 10.5 23.2 12l-6.7 1.9Z"/>'
    + '<circle cx="12.7" cy="10.4" r="1.4"/>',
  seahorse:
    '<path d="M13.7 5.4c2.7 0 4.1 1.9 3.7 4.3-.4 2.5-2.5 3.5-3.3 5.6-.8 2.1.2 3.5 2 3.5 1.7 0 2.5-1.3 1.8-2.3"/>'
    + '<path d="M16.9 7.6c2.1 0 3.8.6 4.2 1.3"/>'
    + '<path d="M11.7 5.6 11 2.5l1.7 1.7.9-2.5 1 2.5L16.3 2.5l-.5 3.1"/>'
    + '<circle cx="14.4" cy="7.1" r="1.1"/>',
  snake:
    '<path d="M12 20.5c-5.4 0-9-1.9-9-4.1s3.6-4.1 9-4.1 9 1.9 9 4.1-3.6 4.1-9 4.1Z"/>'
    + '<path d="M12 4.4c2.8 0 4.9 1.9 4.9 4.3S14.8 13 12 13s-4.9-1.9-4.9-4.3S9.2 4.4 12 4.4Z"/>'
    + '<path d="M10.2 8v.6M13.8 8v.6"/>'
    + '<path d="M12 13.1v2.4M12 15.5l-1.1 1.3M12 15.5l1.1 1.3"/>',
  spider:
    '<path d="M12 10.9c3 0 5.3 2.4 5.3 5.4s-2.3 5.2-5.3 5.2-5.3-2.2-5.3-5.2 2.3-5.4 5.3-5.4Z"/>'
    + '<path d="M12 5.7c2 0 3.4 1.4 3.4 3.2s-1.4 3.2-3.4 3.2-3.4-1.4-3.4-3.2S10 5.7 12 5.7Z"/>'
    + '<path d="M8.5 8.1 4.2 5.3 1.7 7.2M8.1 11.5 3.1 10.6.9 13.5"/>'
    + '<path d="M15.5 8.1l4.3-2.8 2.5 1.9M15.9 11.5l5-.9 2.2 2.9"/>'
    + '<path d="M10.8 8.2v.5M13.2 8.2v.5"/>',
  weasel:
    '<path d="M3.7 14.7c0-2.8 3.2-4.6 7-4.6 3.9 0 6.5 1.2 7.5 2.8.8 1.4.2 3.4-2.4 4.4-3.6 1.4-10 .6-12.1-1.4Z"/>'
    + '<path d="M17.5 11.4c1.8-1.5 4.1-1.9 5.1-.7.8 1-.6 2.7-2.7 3.1-1.7.4-2.7-.8-2.4-2.4Z"/>'
    + '<path d="M4.6 14.3c-2.2.4-3.7-.8-4.1-2.7"/>'
    + '<circle cx="18.9" cy="9.7" r="1.2"/>'
    + '<circle cx="20.6" cy="11.5" r=".9"/>',
  /* Four toes over a pad, rather than a ring of circles -- the first attempt
     put them in an even arc and the result read as a face, which beside a cat
     and an owl is the one thing it must not do. The outer two are tilted to
     follow the curve of a real print. */
  paw:
    '<ellipse cx="12" cy="17.2" rx="4.9" ry="3.4"/>'
    + '<ellipse cx="6.4" cy="11.3" rx="1.9" ry="2.2"'
    + ' transform="rotate(-20 6.4 11.3)"/>'
    + '<ellipse cx="10" cy="7.9" rx="1.9" ry="2.3"/>'
    + '<ellipse cx="14" cy="7.9" rx="1.9" ry="2.3"/>'
    + '<ellipse cx="17.6" cy="11.3" rx="1.9" ry="2.2"'
    + ' transform="rotate(20 17.6 11.3)"/>',
};

function familiarIcon(shell) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "familiar-icon");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");   // the name beside it already says
  svg.innerHTML = FAMILIAR_ICONS[shell] || FAMILIAR_ICONS.paw;
  return svg;
}

/* The fifteen forms find familiar allows, spelled the way the spell spells
 * them. Only the one that is two words needs saying; the rest are the shell id
 * with a capital on the front, so a shell added later needs no entry here. */
const SHELL_LABELS = { seahorse: "Sea horse" };

function shellLabel(id) {
  return SHELL_LABELS[id] || id.charAt(0).toUpperCase() + id.slice(1);
}

/* Browsing the creatures.
 *
 * A dropdown listing "bat, cat, crab, fish…" asks somebody to choose a shape
 * from a list of words for shapes. Fifteen drawings exist; this shows them.
 *
 * Each preview is an <img> pointing at the shell's own SVG rather than the
 * markup inlined into this page, and that is deliberate on two counts. Fifteen
 * inlined shells means fifteen elements all called #familiar, #head and
 * #eye-left, and duplicate ids are how clip-paths start pointing at the wrong
 * creature. And an <img> is its own document: the shell's colours cannot leak
 * out and the hub's cannot leak in, which is the whole promise a shell makes.
 *
 * It works because each shell.svg carries its own resting style -- see the
 * <style> block at the top of any of them. Without that the previews would all
 * render fast asleep with their mouths open. */
function shellPicker(current, ids, onPick) {
  const wrap = document.createElement("div");
  wrap.className = "shell-picker";
  wrap.setAttribute("role", "group");
  wrap.setAttribute("aria-label", "What this familiar looks like");

  for (const id of ids) {
    const choice = document.createElement("button");
    choice.type = "button";
    choice.className = "shell-choice";
    choice.setAttribute("aria-pressed", String(id === current));

    const img = document.createElement("img");
    img.src = `/shells/${encodeURIComponent(id)}/shell.svg`;
    img.alt = "";              // the name underneath already says which it is
    img.loading = "lazy";
    img.width = 120;
    img.height = 120;

    choice.append(img, document.createTextNode(shellLabel(id)));
    choice.addEventListener("click", () => onPick(id, choice, wrap));
    wrap.append(choice);
  }
  return wrap;
}

/* The chevron on a disclosure button, rotated by CSS when it opens. */
function familiarCaret() {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "caret");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.innerHTML = '<path d="M9 5l7 7-7 7"/>';
  return svg;
}

async function loadFamiliars() {
  const host = document.getElementById("familiar-body");
  host.textContent = "";
  const isDM = ME && ME.role === "dm";
  const section = block("Familiars");
  try {
    const [cfg, shells] = await Promise.all([
      tableConfig(),
      apiGet("/shells"),
    ]);
    const list = document.createElement("ul");
    list.className = "card-list";
    for (const familiar of cfg.familiars || []) {
      list.append(familiarCard(familiar, cfg, shells, isDM));
    }
    section.append(list);

    if (isDM) section.append(summonControl());
  } catch (err) {
    const note = document.createElement("p");
    note.className = "note";
    note.textContent = `Couldn't load the familiars: ${err.message}`;
    section.append(note);
  }
  host.append(section);
}

/* Redraw after anything that changes who is bound or who exists. */
function refreshFamiliars() {
  forgetTableConfig();
  loadFamiliars();
  renderTableNav();     // the drawer offers this tab based on what you own
}

/* Summoning, with the owner named at the moment of summoning.
 *
 * `POST /familiars` has always accepted an `owner` and nothing has ever sent
 * one, so every familiar on every table was created unowned -- which is to say
 * the DM's, since an unowned familiar falls to them. In the fiction a familiar
 * is summoned by a *player*, and its persona is private to them; that could
 * not be true of any familiar this app had ever made. This select is the
 * missing half.
 */
function summonControl() {
  const wrap = document.createElement("div");
  wrap.className = "row";

  const who = document.createElement("select");
  who.append(new Option("the table's own", ""));
  apiGet("/members")
    .then((data) => {
      for (const member of data.members || []) {
        who.append(new Option(member.name, member.id));
      }
    })
    .catch(() => { /* summoning still works; it just cannot be assigned yet */ });

  const add = document.createElement("button");
  add.type = "button";
  add.className = "secondary";
  add.textContent = "Summon another";
  add.addEventListener("click", async () => {
    const name = prompt("What is it called?");
    if (!name || !name.trim()) return;
    try {
      await apiSend("/familiars", { name: name.trim(), owner: who.value });
      refreshFamiliars();
    } catch (err) { alert(err.message); }
  });

  const label = document.createElement("span");
  label.className = "note";
  label.textContent = "summoned by";

  wrap.append(add, label, who);
  return wrap;
}

function familiarCard(familiar, cfg, shells, isDM) {
  const li = document.createElement("li");
  const bound = cfg.id === familiar.id;

  const name = document.createElement("div");
  name.className = "entry-name";
  // The creature's own shape, beside its own name. `let` because the picker
  // below can change which animal this is, and the icon has to follow it
  // without a round trip -- see the shell listener.
  let icon = familiarIcon(familiar.shell);
  name.append(icon, document.createTextNode(familiar.name));
  // A state, drawn as one. "(bound)" in the middle of a name reads as part of
  // the name, which for a creature somebody chose the name of is unkind.
  if (bound) {
    const pill = document.createElement("span");
    pill.className = "pill pill-on";
    pill.textContent = "bound";
    name.append(pill);
  }
  li.append(name);

  // The server decides; this only chooses what to offer, so a wrong guess is a
  // missing control rather than a leak. It mirrors Familiar.may_edit exactly:
  // the summoner, or -- for a familiar nobody summoned -- the DM. A DM is
  // deliberately *not* offered somebody else's, because a persona is not
  // theirs to read.
  const mine = familiar.owner ? (ME && familiar.owner === ME.id) : isDM;
  if (!mine) {
    const note = document.createElement("div");
    note.className = "note";
    note.textContent = "Summoned by somebody else — their creature, their business.";
    li.append(note);
    if (isDM) li.append(reassignRow(familiar));
    return li;
  }

  const row = document.createElement("div");
  row.className = "row";

  /* What it looks like. A disclosure rather than a dropdown, because the
     choice is between drawings and a list of words for drawings is not a
     choice anybody can make. */
  const shape = document.createElement("button");
  shape.type = "button";
  shape.className = "secondary disclosure";
  shape.setAttribute("aria-expanded", "false");
  const shapeName = document.createTextNode(shellLabel(familiar.shell));
  shape.append(familiarCaret(), shapeName);

  let picker = null;
  const closePicker = () => {
    if (picker) picker.remove();
    picker = null;
    shape.setAttribute("aria-expanded", "false");
  };
  shape.addEventListener("click", () => {
    if (picker) { closePicker(); return; }
    picker = shellPicker(familiar.shell, shells.shells, async (id, choice, host) => {
      try {
        await apiSend(`/familiars/${familiar.id}`, { shell: id }, "PATCH");
        familiar.shell = id;
        // Swap the mark beside the name. Reassigned rather than left pointing
        // at the node we just removed, or the next pick would replace a
        // detached one.
        const next = familiarIcon(id);
        icon.replaceWith(next);
        icon = next;
        shapeName.nodeValue = shellLabel(id);
        for (const other of host.querySelectorAll(".shell-choice")) {
          other.setAttribute("aria-pressed", "false");
        }
        choice.setAttribute("aria-pressed", "true");
      } catch (err) { alert(err.message); }
    });
    li.append(picker);
    shape.setAttribute("aria-expanded", "true");
  });
  row.append(shape);

  /* A button, because it is one.
   *
   * This was an <a href="#"> that called preventDefault -- a link that goes
   * nowhere, which is announced as a link, offers a meaningless target to
   * "copy link address", and does not answer the space bar. It opens a panel
   * on this page, so it is a disclosure button: aria-expanded says whether the
   * panel is open, and the caret says the same thing to everyone else. */
  const persona = document.createElement("button");
  persona.type = "button";
  persona.className = "secondary disclosure";
  persona.setAttribute("aria-expanded", "false");
  persona.append(familiarCaret(), document.createTextNode("Persona"));
  persona.addEventListener("click", () => {
    const open = li.querySelector(".amend-form");
    if (open) {
      open.remove();
      persona.setAttribute("aria-expanded", "false");
      return;
    }
    li.append(personaForm(li, familiar));
    persona.setAttribute("aria-expanded", "true");
  });
  row.append(persona);

  if (!bound) {
    const bind = document.createElement("button");
    bind.type = "button";
    bind.className = "secondary";
    bind.textContent = "Bind";
    bind.addEventListener("click", async () => {
      try { await apiSend("/familiar", { id: familiar.id }); refreshFamiliars(); }
      catch (err) { alert(err.message); }
    });
    row.append(bind);

    const dismiss = document.createElement("button");
    dismiss.type = "button";
    dismiss.className = "secondary";
    dismiss.textContent = "Dismiss";
    dismiss.addEventListener("click", async () => {
      if (!confirm(`Dismiss ${familiar.name}? Its persona and its bond with the party go too.`)) return;
      try { await apiSend(`/familiars/${familiar.id}`, {}, "DELETE"); refreshFamiliars(); }
      catch (err) { alert(err.message); }
    });
    row.append(dismiss);
  }
  li.append(row);
  return li;
}

/* What a persona is actually made of.
 *
 * The box arrives pre-filled from persona.example.md, so the answers are all
 * already in front of whoever opens it -- but as a finished document, which
 * tells you what it says and not which parts you are meant to change, which
 * parts are load-bearing, or why any of it is there. The odder-looking rules
 * are the load-bearing ones: "never use markdown" is not style advice, it is
 * because every word of this is read out by a speech synthesiser and an
 * asterisk gets pronounced. This says which is which.
 *
 * Four headings, because those are the four the example uses and the ones the
 * prompt is assembled from. Written as what each section *buys you*, since the
 * headings alone already appear in the text below the guidance. */
const PERSONA_GUIDE = [
  ["Who you are",
   "The creature itself — what it is, how it looks, how old, and two or three "
   + "traits with some friction in them: a vice, a loyalty, something that "
   + "delights it. This is the only section that is purely yours to invent."],
  ["How you speak",
   "The section that does the most work, because everything here is spoken "
   + "aloud by a voice. Keep the instruction to answer in one to three "
   + "sentences unless asked for more, and keep the ban on markdown, bullets "
   + "and emoji — a creature that replies in asterisks and hashes gets them "
   + "read out. Spelling things as they are said (\u201carmour class "
   + "fifteen\u201d, not \u201cAC 15\u201d) is the same idea."],
  ["What you know",
   "That the campaign notes are its own memories rather than a document it "
   + "was handed, that it knows fifth-edition rules and lore, and that a house "
   + "rule in the notes beats the printed rule. Take this out and it will "
   + "quote the book at your table instead of your table's version of it."],
  ["What you must not do",
   "The guard rails. Do not hint at DM-only notes (the server only shows "
   + "them to it after the pass-phrase); do not invent campaign facts, because a familiar that "
   + "cheerfully makes up an NPC is worse than one that admits it was asleep; "
   + "do not break character to discuss being a language model."],
];

function personaGuide() {
  const details = document.createElement("details");
  details.className = "guide";
  const summary = document.createElement("summary");
  summary.textContent = "What goes in a persona";
  details.append(summary);

  const list = document.createElement("dl");
  for (const [heading, body] of PERSONA_GUIDE) {
    const dt = document.createElement("dt");
    dt.textContent = heading;
    const dd = document.createElement("dd");
    dd.textContent = body;
    list.append(dt, dd);
  }
  details.append(list);

  const footer = document.createElement("p");
  footer.className = "field-hint";
  footer.textContent = "It is a whole document, not a form — headings and "
    + "wording are yours to change. The familiar is shown all of it before "
    + "every single answer.";
  details.append(footer);
  return details;
}

function personaForm(li, familiar) {
  const form = document.createElement("div");
  form.className = "amend-form";
  form.append(personaGuide());
  const area = document.createElement("textarea");
  area.className = "prose";
  area.value = "Loading…";
  area.disabled = true;
  const actions = document.createElement("div");
  actions.className = "amend-actions";
  const save = document.createElement("button");
  save.type = "button";
  save.textContent = "Save";
  save.disabled = true;
  const error = document.createElement("span");
  error.className = "amend-error";
  actions.append(save, error);
  form.append(area, actions);
  const fitPersona = autoGrowTextarea(area);

  let revision = "";
  fetch(api(`/familiars/${familiar.id}/persona`))
    .then((r) => r.json().then((d) => ({ ok: r.ok, d })))
    .then(({ ok, d }) => {
      if (!ok) { area.value = ""; error.textContent = d.error; return; }
      area.value = d.persona;
      revision = d.revision;
      area.disabled = false;
      save.disabled = false;
      // Only now is there anything to size the box against: it was created
      // holding the word "Loading…".
      fitPersona();
    })
    .catch((err) => { error.textContent = err.message; });

  save.addEventListener("click", async () => {
    save.disabled = true;
    error.textContent = "";
    try {
      const d = await apiSend(`/familiars/${familiar.id}/persona`,
                              { persona: area.value, revision }, "PUT");
      revision = d.revision;
      error.textContent = "Saved.";
    } catch (err) { error.textContent = err.message; }
    save.disabled = false;
  });
  return form;
}


/* The DM's two powers over a familiar that is not theirs: hand it to somebody
 * else, or dismiss it. Neither opens the persona. This is what stops a
 * creature whose player has left the group becoming a thing nobody can touch
 * and nobody can clear away. */
function reassignRow(familiar) {
  const row = document.createElement("div");
  row.className = "row";

  const who = document.createElement("select");
  who.append(new Option("hand it to…", ""));
  apiGet("/members")
    .then((data) => {
      for (const member of data.members || []) {
        if (member.id !== familiar.owner) who.append(new Option(member.name, member.id));
      }
    })
    .catch(() => {});
  who.addEventListener("change", async () => {
    if (!who.value) return;
    try {
      await apiSend(`/familiars/${familiar.id}`, { owner: who.value }, "PATCH");
      refreshFamiliars();
    } catch (err) { alert(err.message); }
  });

  const dismiss = document.createElement("button");
  dismiss.type = "button";
  dismiss.className = "secondary";
  dismiss.textContent = "Dismiss";
  dismiss.addEventListener("click", async () => {
    if (!confirm(`Dismiss ${familiar.name}? Its persona and its bond with the party go too.`)) return;
    try { await apiSend(`/familiars/${familiar.id}`, {}, "DELETE"); refreshFamiliars(); }
    catch (err) { alert(err.message); }
  });

  row.append(who, dismiss);
  return row;
}
