/* Shared helpers for the post-game review pages (timeline.html, cast.html):
 * a POST helper both pages use to submit amendments, and a timestamp formatter.
 *
 * There used to be a `getPlayerName()` here that prompted for a name and kept
 * it in localStorage, because the server had no idea who anyone was. It does
 * now -- an amendment is attributed to the signed-in member, server-side -- so
 * the prompt is gone and the `by` field is no longer something the browser
 * gets to choose. That is the right way round for a permanent record.
 */

async function postAmend(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (typeof handleUnauthorized === "function" && handleUnauthorized(res)) {
    throw new Error("signed out");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `server said ${res.status}`);
  return data;
}

function formatAmendTime(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}
