/* Which table is this page looking at?
 *
 * Every page that shows a table's data lives under /t/<slug>/ -- the familiar's
 * screen at /t/<slug>/, the timeline and cast sheet beside it. The slug in the
 * path is the only thing the browser needs to know: `api()` turns a bare
 * endpoint name into that table's URL, and every fetch in familiar.js,
 * timeline.js and cast.js goes through it.
 *
 * Deriving the slug from the URL rather than baking it into the page keeps the
 * HTML files identical for every table -- there is no template engine here and
 * no build step, and this is what lets it stay that way.
 */

const TABLE_SLUG = (() => {
  const parts = location.pathname.split("/").filter(Boolean);
  return parts[0] === "t" && parts[1] ? decodeURIComponent(parts[1]) : "";
})();

/* Build a URL for one of this table's endpoints. Pass the part after the slug:
 *   api("/config")                      -> /api/t/<slug>/config
 *   api(`/cast/${id}/amend`)            -> /api/t/<slug>/cast/<id>/amend      */
function api(path) {
  return `/api/t/${encodeURIComponent(TABLE_SLUG)}${path}`;
}

/* The body bus for this table. */
function busUrl() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/t/${encodeURIComponent(TABLE_SLUG)}/ws/state`;
}

/* A JSON POST/PUT that carries the session cookie and satisfies the server's
 * CSRF check -- the Content-Type is not decoration, it is the check. Every
 * state-changing call in the app goes through here or names the header itself.
 */
async function apiSend(path, body, method = "POST") {
  const res = await fetch(api(path), {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `server said ${res.status}`);
  return data;
}

/* A signed-out browser gets a redirect to the login page from any HTML route,
 * but an API call gets a 401 -- which arrives in the middle of a page that is
 * already open. Send the reader somewhere useful rather than letting the page
 * quietly stop working. */
function handleUnauthorized(res) {
  if (res.status === 401) {
    location.href = `/login.html?next=${encodeURIComponent(location.pathname)}`;
    return true;
  }
  return false;
}

/* This table's /config, fetched once per page.
 *
 * Four places used to ask for it independently -- the familiar's own boot, the
 * nav strip's back-link, and the hub twice -- so a page that showed the cast
 * paid for two identical round trips before it drew anything. The navigation
 * now needs the same document a third time (it decides which tabs to offer
 * from `me` and `familiars[].owner`), and three copies of one small identity
 * document is where they start disagreeing with each other.
 *
 * The promise is cached, not the result: callers that race get the same
 * in-flight request rather than a second one. */
let configPromise = null;

function tableConfig() {
  if (!configPromise) {
    configPromise = fetch(api("/config")).then((res) => {
      if (handleUnauthorized(res)) throw new Error("signed out — heading to the login page");
      if (!res.ok) throw new Error(`server said ${res.status}`);
      return res.json();
    });
  }
  return configPromise;
}

/* Drop the cache after anything that changes the answer -- binding a different
 * familiar, summoning one, dismissing one. Nothing re-fetches on its own;
 * the next caller does. */
function forgetTableConfig() { configPromise = null; }
