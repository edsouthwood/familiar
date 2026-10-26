/* The navigation for one table, on every page that belongs to it -- including
 * the familiar's own screen, which never had any.
 *
 * It is anchors rather than a tab widget, and that is not an aesthetic choice.
 * cast.js, locations.js, timeline.js and search.js each declare `const
 * statusEl` at top level, and classic <script> tags share one lexical scope --
 * so loading any two of them into one document is a SyntaxError and the second
 * never runs. Real tabs would mean renaming top-level bindings in four files
 * first. This gets the feel of tabs for one element per page.
 *
 * That same rule is why every name in this file is prefixed. On the stage it
 * shares a scope with familiar.js and its forty-odd top-level bindings, and a
 * collision there breaks the creature, not the menu.
 *
 * What it replaces is a strip along the top of five pages -- styled in two
 * different stylesheets, neither of which the timeline loaded -- and, on the
 * stage, four links buried at the bottom of the settings drawer. A cog is for
 * changing how something works, not for getting somewhere else.
 */

/* A tab is `{ label, href, hash?, when? }`. `when(cfg)` decides whether this
 * viewer is offered it at all; the server checks again on every request, so a
 * wrong answer here is a missing door, never an open one. */
const NAV_SECTIONS = [
  {
    group: "The record",
    tabs: [
      { label: "Timeline", href: "timeline.html" },
      { label: "Cast", href: "cast.html" },
      { label: "Places", href: "locations.html" },
      { label: "Search", href: "search.html" },
    ],
  },
  {
    group: "The party",
    tabs: [
      { label: "Characters", href: "hub.html", hash: "#characters" },
      { label: "Stats", href: "hub.html", hash: "#stats" },
    ],
  },
  {
    group: "The table",
    tabs: [
      { label: "Campaign", href: "hub.html", hash: "#campaign" },
      { label: "The familiar", href: "hub.html", hash: "#familiar", when: navMayEditAFamiliar },
      { label: "Running the table", href: "hub.html", hash: "#running", when: navIsDM },
    ],
  },
];

function navIsDM(cfg) {
  return Boolean(cfg && cfg.me && cfg.me.role === "dm");
}

/* Is there a familiar this person may open? Their own, or -- for the DM -- one
 * the table owns rather than a player. A DM has no route into a familiar
 * somebody else summoned, so offering them the tab for it would be offering a
 * door that is locked. */
function navMayEditAFamiliar(cfg) {
  const me = (cfg && cfg.me) || null;
  if (!me) return false;
  return (cfg.familiars || []).some(
    (familiar) => (familiar.owner ? familiar.owner === me.id : me.role === "dm"),
  );
}

/* Is the drawer docked rather than overlaid?
 *
 * Kept deliberately in step with the media query in nav.css, and the duplicated
 * breakpoint is the price of that -- CSS cannot tell JavaScript what it
 * matched. It has to be told, because docking is the one state where `inert`
 * would be actively wrong: an inert sidebar paints perfectly and cannot be
 * clicked or tabbed into, which is a navigation that looks fine and does
 * nothing.
 *
 * Only the document pages dock. The stage keeps the overlay at every width --
 * see the note at the top of nav.css. */
const NAV_DOCK_QUERY = "(min-width: 60rem)";

function navDocked() {
  return document.body.classList.contains("with-rail")
    && Boolean(window.matchMedia && window.matchMedia(NAV_DOCK_QUERY).matches);
}

/* Which page is this? Derived rather than passed in: five call sites each used
 * to name their own file, and a new page that forgot to would silently show no
 * navigation. */
function navCurrentPage() {
  const last = location.pathname.split("/").pop();
  return last || "index.html";
}

function navIsActive(tab, page, hash) {
  if (tab.href !== page) return false;
  if (!tab.hash) return true;
  return tab.hash === (hash || "#characters");
}

function navLink(className, text, href) {
  const a = document.createElement("a");
  a.className = className;
  a.textContent = text;
  if (href) a.href = href;
  return a;
}

/* Paint the drawer. Called once at load and again by the hub when the hash
 * moves, since the hub switches panels without navigating. */
function renderTableNav() {
  const host = document.getElementById("table-nav");
  if (!host) return;

  const page = navCurrentPage();
  const onStage = page === "index.html" || page === "";
  host.textContent = "";
  host.classList.remove("open");

  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "nav-toggle";
  toggle.setAttribute("aria-expanded", "false");
  toggle.setAttribute("aria-label", "Menu");
  for (let i = 0; i < 3; i += 1) toggle.append(document.createElement("span"));

  const scrim = document.createElement("div");
  scrim.className = "nav-scrim";

  const drawer = document.createElement("div");
  drawer.className = "nav-drawer";
  drawer.inert = !navDocked();

  // The creature's name arrives with /config; until then the drawer still
  // works, which is the point of drawing it before the fetch resolves.
  const home = navLink("nav-home", "The familiar", onStage ? null : "./");
  if (onStage) home.classList.add("active");
  drawer.append(home);

  const out = document.createElement("div");
  out.className = "nav-out";
  out.append(navLink("", "All my tables", "/den.html"));

  host.append(toggle, scrim, drawer);

  const openNav = () => {
    if (navDocked()) return;             // nothing to open; it is already there
    const cog = document.getElementById("settings");
    if (cog) cog.hidden = true;          // two fixed panels at once is the only real collision
    host.classList.add("open");
    toggle.setAttribute("aria-expanded", "true");
    drawer.inert = false;
    const first = drawer.querySelector("a");
    if (first) first.focus();
  };
  const closeNav = () => {
    host.classList.remove("open");
    toggle.setAttribute("aria-expanded", "false");
    drawer.inert = !navDocked();
  };
  closeTableNav = closeNav;

  toggle.addEventListener("click", () => {
    if (host.classList.contains("open")) { closeNav(); toggle.focus(); } else { openNav(); }
  });
  scrim.addEventListener("click", closeNav);
  // No focus trap: this is navigation, not a modal, and the page behind it
  // stays legitimately reachable.
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && host.classList.contains("open")) {
      closeNav();
      toggle.focus();
    }
  });

  tableConfig().then((cfg) => {
    if (cfg && cfg.name) home.textContent = onStage ? cfg.name : `← ${cfg.name}`;
    const hash = location.hash;
    for (const section of NAV_SECTIONS) {
      const tabs = section.tabs.filter((tab) => !tab.when || tab.when(cfg));
      if (!tabs.length) continue;        // a group with nothing in it is not drawn
      const heading = document.createElement("p");
      heading.className = "nav-group";
      heading.textContent = section.group;
      drawer.append(heading);
      for (const tab of tabs) {
        const a = navLink("nav-tab", tab.label, tab.hash ? `${tab.href}${tab.hash}` : tab.href);
        if (navIsActive(tab, page, hash)) {
          a.classList.add("active");
          a.setAttribute("aria-current", "page");
        }
        drawer.append(a);
      }
    }
    drawer.append(out);
  }).catch(() => {
    // A failed /config must still leave a way out of the page.
    drawer.append(out);
  });
}

/* Reassigned by renderTableNav; declared here so anything may call it. */
let closeTableNav = () => {};

renderTableNav();

/* A window dragged across the breakpoint, or a tablet turned on its side.
 *
 * Registered once, at module scope, and it finds the drawer in the DOM rather
 * than closing over one. renderTableNav() runs again every time the hub
 * switches panel and builds a fresh drawer each time, so a listener bound
 * inside it would be one more listener per panel switch, every one of them
 * holding a detached element.
 *
 * Undocking has to put `inert` back or the off-screen drawer becomes tabbable
 * again; docking has to take it off, or the sidebar arrives painted and dead. */
if (window.matchMedia) {
  const navDock = window.matchMedia(NAV_DOCK_QUERY);
  const navOnDockChange = () => {
    const host = document.getElementById("table-nav");
    const drawer = host && host.querySelector(".nav-drawer");
    if (!drawer) return;
    if (navDocked()) {
      closeTableNav();
      drawer.inert = false;
    } else if (!host.classList.contains("open")) {
      drawer.inert = true;
    }
  };
  if (navDock.addEventListener) navDock.addEventListener("change", navOnDockChange);
  else if (navDock.addListener) navDock.addListener(navOnDockChange);  // Safari before 14
}
