/* Places page — browses locations.json via /api/locations.
 *
 * A thin wrapper over roster.js, which is the same renderer the cast page
 * uses: server-side these two lists are one class (server.Roster), and the
 * only differences worth having on the client are the endpoint and the words.
 * No tabs here yet — if this page grows a map or a "where are we now", that
 * is when it earns them. */

const statusEl = document.getElementById("status");

const places = createRoster({
  path: "/locations",
  key: "places",
  listEl: document.getElementById("places-list"),
  statusEl,
  noun: "places",
  empty: "No places recorded yet — they'll show up here as ambient capture overhears the party naming them.",
});

async function loadIdentity() {
  try {
    // Shared with the navigation drawer, which wants the same document -- see
    // tableConfig() in table.js. This page used to fetch it a second time.
    const cfg = await tableConfig();
    if (cfg.name) {
      const heading = `Places ${cfg.name} has been`;
      document.getElementById("page-title").textContent = heading;
      document.getElementById("heading").textContent = heading;
    }
  } catch (_) { /* keep the built-in defaults */ }
}

loadIdentity();
places.load();

