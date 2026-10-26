/* The small amount of JavaScript behind signing in, signing up and accepting an
 * invitation. Three forms that all do the same thing: POST some JSON, show the
 * error if there is one, go somewhere if there isn't.
 *
 * No framework, same as the rest of the app. */

function byId(id) { return document.getElementById(id); }

/* POST `body` to `url` as JSON. The Content-Type is required, not cosmetic:
 * the server rejects state-changing requests that aren't genuine JSON calls,
 * which is what stops another site from submitting these forms on your behalf. */
async function postJson(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `server said ${res.status}`);
  return data;
}

/* Wire up a form: disable the button while in flight, surface the server's
 * message on failure, hand the response to `onDone` on success. */
function bindForm(url, collect, onDone) {
  const form = byId("form");
  const errEl = byId("error");
  const button = form.querySelector("button[type=submit]");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    errEl.textContent = "";
    button.disabled = true;
    try {
      onDone(await postJson(url, collect()));
    } catch (err) {
      errEl.textContent = err.message;
      button.disabled = false;
    }
  });
}

/* Signing in and signing up both land you wherever you were headed, or at the
 * front door if you arrived directly. `next` is read from the query string so
 * that an expired session on the timeline page returns you to the timeline
 * rather than dumping you at the top. */
function bindAuthForm(url, collect) {
  bindForm(url, collect, () => {
    const next = new URLSearchParams(location.search).get("next");
    // Same-origin paths only. "//evil.example" also starts with "/" and is a
    // protocol-relative URL, so a bare startsWith check is an open redirect.
    const safe = next && next.startsWith("/") && !next.startsWith("//");
    location.href = safe ? next : "/";
  });
}
