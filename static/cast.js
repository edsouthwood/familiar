/* Cast page — browses cast.json via /api/cast.
 *
 * The dice tallies used to be a second tab here. They live on the hub's party
 * panel now, beside the characters they belong to: a stats table is about the
 * *party*, and sitting it next to the NPCs was an accident of both being lists.
 * That also puts this page back in the same shape as the places page, which is
 * the same renderer over a different endpoint (roster.js).
 *
 * Amending an NPC works the same way as timeline events (see
 * review-shared.js): the auto-generated description is never overwritten, a
 * correction is layered on top and always wins for display. */

const statusEl = document.getElementById("status");
const castListEl = document.getElementById("cast-list");

const cast = createRoster({
  path: "/cast",
  key: "npcs",
  listEl: castListEl,
  statusEl,
  noun: "cast",
  empty: "No NPCs recorded yet — they'll show up here as ambient capture overhears them.",
});

async function loadIdentity() {
  try {
    // Shared with the navigation drawer, which wants the same document -- see
    // tableConfig() in table.js. This page used to fetch it a second time.
    const cfg = await tableConfig();
    if (cfg.name) {
      const heading = `${cfg.name}'s cast`;
      document.getElementById("page-title").textContent = heading;
      document.getElementById("heading").textContent = heading;
    }
  } catch (_) { /* keep the built-in defaults */ }
}

loadIdentity();
cast.load();

