/* This month's spend, drawn the same way in both places that show it.
 *
 * The den shows it per table in a list of tables; the hub shows it for the one
 * table you are running. Same figures, same reading of them, so the rendering
 * is shared and only the fetch differs -- the den asks by slug across several
 * tables, the hub asks through apiGet.
 *
 * On a Claude key the cache hit rate is the line worth reading: caching
 * engages silently or not at all -- a prompt under the minimum cacheable length
 * simply never caches, with no error anywhere. If this says 0%, you are paying
 * full price for every question.
 */

function renderUsage(host, data) {
  host.textContent = "";

  const heading = document.createElement("p");
  heading.className = "note";
  heading.textContent = `This month (${data.month}), answered by ${data.model}:`;
  host.append(heading);

  const rows = [
    ["Questions and fold-ins", data.totals.requests.toLocaleString()],
    ["Tokens in / out",
     `${data.totals.llm_tokens_in.toLocaleString()} / `
     + `${data.totals.llm_tokens_out.toLocaleString()}`],
    ["Served from cache", formatCacheRate(data.cache_hit_rate)],
    ["Spoken", `${data.totals.tts_chars.toLocaleString()} characters`],
    ["Estimated cost", formatCost(data)],
  ];

  const list = document.createElement("dl");
  list.className = "usage";
  for (const [label, value] of rows) {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value;
    list.append(dt, dd);
  }
  host.append(list);
}

/* "$0.00" and "nobody counted" are very different statements. The Claude CLI
 * answers from the subscription on the machine and reports no token counts at
 * all, so a month spent on it has no cost to show -- say that, rather than
 * printing a zero that reads as "free". */
function formatCost(data) {
  if (data.estimated_cost) return `$${data.estimated_cost.usd.toFixed(2)}`;
  if (data.unmetered && data.unmetered.length) {
    return `not measured — ${data.unmetered.join(", ")} reports no token counts`;
  }
  return "not tracked for this provider";
}

function formatCacheRate(rate) {
  if (rate === null) return "nothing measured yet";
  if (rate === 0) return "0% — caching is not engaging";
  return `${Math.round(rate * 100)}%`;
}
