"""What a table has actually spent.

db.py stores the numbers -- tokens in and out, cache reads and writes, TTS
characters -- and this module decides what they mean: turning recorded tokens
into an estimated cost, and saying how well prompt caching is working. It is
*policy*, so it changes when pricing changes, without any schema migration.

Self-hosting with your own API key, this is how you see "this month cost
$0.40" without opening the provider's billing page.
"""

import os
from datetime import datetime, timezone

# A fixed safety brake on ambient fold-ins. They are the one LLM call any
# player can trigger without asking anything, so a recorder left running in
# somebody's bag gets stopped here rather than on your API bill.
FOLD_INS_PER_HOUR = int(os.environ.get("FOLD_INS_PER_HOUR", "12"))


def configured_model() -> str:
    """The model this install answers with, for pricing the month.

    Claude only when `claude` is in LLM_PROVIDER (mirroring brain's defaults);
    otherwise the Gemini model, which is unpriced, so on the default free brain
    the cost is simply left blank rather than priced at somebody else's rates.
    """
    chain = [p.strip().lower() for p in
             os.environ.get("LLM_PROVIDER", "gemini").split(",") if p.strip()]
    if "claude" in chain:
        return os.environ.get("CLAUDE_MODEL") or "claude-haiku-4-5"
    return os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------
#
# Anthropic list prices, US dollars per million tokens, as of 2026-08-24.
# Cache reads cost about a tenth of a fresh input token and cache writes about
# 1.25x -- which is the entire reason the usage table records them separately.

PRICES = {
    "claude-haiku-4-5": {"in": 1.00, "out": 5.00},
    "claude-sonnet-5":  {"in": 3.00, "out": 15.00},   # $2/$10 intro to 2026-08-31
    "claude-opus-4-8":  {"in": 5.00, "out": 25.00},
    "claude-opus-5":    {"in": 5.00, "out": 25.00},
}
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25

# Providers whose price we do not track. Gemini is deliberately absent from
# PRICES rather than guessed at: a made-up number in a cost model is worse than
# a blank, because it looks like evidence.
UNPRICED = ("gemini", "local", "claude-cli")


def estimate_cost(totals: dict, model: str) -> dict | None:
    """Rough dollar cost of one month's recorded tokens, or None if unpriced.

    "Rough" is honest: the recorded totals mix every model a table's chain fell
    through, and this prices them all at `model`. It is right to the order of
    magnitude, which is what the question ("what is this costing me?") needs.
    """
    price = PRICES.get(model)
    if price is None:
        return None
    token_fields = ("llm_tokens_in", "llm_tokens_out",
                    "cache_read_tokens", "cache_write_tokens")
    if not any(totals.get(f, 0) for f in token_fields):
        # Requests happened but no provider reported a token count -- the Claude
        # CLI never does. Returning $0.00 here would read as "this month was
        # free" when it means "nobody counted". None makes the panel say so.
        return None
    fresh_in = totals.get("llm_tokens_in", 0)
    cost = (
        fresh_in * price["in"]
        + totals.get("cache_read_tokens", 0) * price["in"] * CACHE_READ_MULTIPLIER
        + totals.get("cache_write_tokens", 0) * price["in"] * CACHE_WRITE_MULTIPLIER
        + totals.get("llm_tokens_out", 0) * price["out"]
    ) / 1_000_000
    return {"model": model, "usd": round(cost, 4)}


def cache_hit_rate(totals: dict) -> float | None:
    """Share of input tokens served from cache, or None if nothing was read yet.

    If it stays at 0.0 across a session of repeated questions, caching
    is not engaging -- most likely the prompt is under the ~1024-token minimum
    cacheable prefix, or something volatile has crept in ahead of the
    breakpoint.
    """
    cached = totals.get("cache_read_tokens", 0)
    total = cached + totals.get("llm_tokens_in", 0)
    if total <= 0:
        return None
    return round(cached / total, 4)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def unmetered_providers(rows) -> list[str]:
    """Providers that answered but reported no tokens.

    Only the Claude CLI does this today: it uses the subscription logged in on
    the machine and prints an answer, with no usage counts anywhere. That is
    perfectly fine for one person running their own familiar -- but the usage
    panel has to say "not measured" rather than quietly showing zero.
    """
    names = []
    for row in rows:
        r = dict(row)
        counted = r.get("llm_tokens_in", 0) or r.get("llm_tokens_out", 0)
        if r.get("requests", 0) and not counted and r.get("provider") in UNPRICED:
            names.append(r["provider"])
    return names


def usage_report(db, table_id: str) -> dict:
    """Everything the DM's usage panel shows."""
    totals = db.usage_totals(table_id)
    by_provider = [dict(r) for r in db.usage_for_month(table_id)]
    model = configured_model()
    return {
        "month": datetime.now(timezone.utc).strftime("%Y-%m"),
        "model": model,
        "totals": totals,
        "by_provider": by_provider,
        "unmetered": unmetered_providers(by_provider),
        "cache_hit_rate": cache_hit_rate(totals),
        "estimated_cost": estimate_cost(totals, model),
    }
