# Lessons from running this bot with real money

This file is the honest post-mortem: specific errors made, their root cause, the fix that went into the code, and what the next person (including future me) should know before touching the same wire.

Every lesson here is tagged with the file / line where the fix lives, so the code can be read against the story.

---

## 1. Polymarket's CLOB rejects orders below 5 shares — don't learn this at runtime

**What happened.** Early paper runs looked great. When the same signals went to real, the executor started reporting silent fills of 0 size. The order was being submitted, Polymarket accepted it, and then nothing settled. Reason: Polymarket's CLOB has a minimum size of **5 shares**, and at prices above ~$0.20 our scaled-down USDC amount translated to fewer than 5 shares. Those orders die at the matching engine, not at our validator.

**Fix.** In `paper_executor.py#L89` paper now bumps any trade whose share count would fall below **5.5 shares** up to that floor (capped by `MAX_PER_TRADE`). This makes paper realistic *and* protects real.

```python
_ep = float((signal or {}).get('insider_price') or 0) or float(entry_price or 0)
if _ep > 0:
    min_usdc_5sh = round(5.5 * _ep, 2)
    if size_usdc < min_usdc_5sh:
        size_usdc = min(min_usdc_5sh, MAX_T)
```

**Lesson.** Whenever paper and real diverge, the broker is usually telling you a rule you didn't know existed. Lift it into paper immediately.

---

## 2. The COBERTURA filter was discarding the whale's actual strategy

**What happened.** The monitor has a `COBERTURA_PARTIDO` filter that drops a signal when the wallet is betting both sides of the same market — that's normally noise from unsophisticated accounts hedging themselves. Except our whitelist wallets (Swisstony, Soarin, WiggleW, KinderSman) **hedge on purpose**: they buy YES and NO in live sports markets and `MERGE` the shares to lock in free capital when the game resolves. The filter was rejecting ~30% of their volume as "noise" when it was actually their edge.

**Fix.** In `monitor.py#L90` we added an explicit bypass set for the designated copy wallets:

```python
DEF_COPY_BYPASS_HEDGE = {
    '0x204f72f35326db932158cba6adff0b9a1da95e14',  # Swisstony
    '0x84dbb7103982e3617704a2ed7d5b39691952aeeb',  # Soarin22
    '0xc96e5287ab294ac0388c2ddb00180fc464cff1f9',  # wigglew
    '0xa6d9b55a6a3a54a9d50ca94c64f00af69c50fd2f',  # kinderSman
}
if _wallet_lc not in DEF_COPY_BYPASS_HEDGE:
    # filters apply only to non-DEF_COPY wallets
```

The `definitive_copy_watcher` then catches the MERGE event and closes our position on the same side the whale just liquidated.

**Lesson.** A filter is a hypothesis about what's noise. Hypotheses need owners: for whom is this signal noise, and for whom is it the point? The same event is garbage for one trader and the whole strategy for another.

---

## 3. "Missing trades" at night turned out to be DNS, not the API

**What happened.** We noticed that some whole markets (six J2 League games one morning) were never processed, even though the whales had bought them hours before. First hypothesis: the API `limit=30` was too small. We bumped it to 100. Still happened. Second hypothesis: the pagination logic only read the first page. We added pagination. Still happened sometimes.

Reading the logs properly: ~935 `NameResolutionError` entries overnight. The bot was alive but the laptop's Wi-Fi (a university network that captive-portaled every few hours) was silently dropping `data-api.polymarket.com` resolutions. Our monitor was polling, failing, logging, and moving on, quietly leaving a hole.

**Fix.** Two layers:
1. **`monitor.py#L92`** — real pagination with `MAX_PAGES=5`. If the first page doesn't cover the gap since `last_ts`, we keep going back. A burst-plus-hiccup doesn't drop trades silently anymore.
2. **Operational** — move the bot off university Wi-Fi and onto phone tethering. The code can only paper over so much.

**Lesson.** When paper and real disagree on *which* events happened (not just outcomes), the first thing to check is whether we were listening at all. Logs are only useful if you grep them for the boring stuff.

---

## 4. The whale's own exit beats our TP/SL — stop trying to be clever

**What happened.** We started with three strategies in parallel: `DEF_TP` (take-profit 1.8× / stop-loss 0.8×), `DEF_HOLD` (hold to resolve), `DEF_COPY` (mirror the whale's own entry/exit). For a long time `DEF_TP` looked best on short windows and we rotated toward it. On longer windows the ranking flipped: whenever `DEF_TP` beat `DEF_COPY` it turned out to be a noisy window, and on the next window the whale's own exit would have been better.

The clearest specific case: on a 3-hour window where Swisstony was down −3.5%, `DEF_COPY` was only down −0.2% because the threshold-based entry filter was quietly throwing away his low-conviction trades. That looked great for drawdown. The red flag: it meant we would also *miss* his high-conviction winners by the same mechanism.

**Fix.** Pivoted the whole project: `DEF_COPY` is the only agent that can be promoted to real capital; `DEF_TP` and `DEF_HOLD` are explicitly kept as paper-only comparison baselines, and we accept they will sometimes "beat" `DEF_COPY` on short windows without changing our mind. Documented in the README under "Primary vs secondary agents".

**Lesson.** The point of a whitelist is that the picked wallets are better than us at *when* to exit, not just *where* to enter. If you can't articulate why your overlay is *structurally* better than the trader you copy, delete the overlay.

---

## 5. We were operating at 1:153 of the whale's size — the math doesn't work at that scale

**What happened.** Over the live run (mid-May → mid-June 2026, 27 trading days, ~6k real trades, ~$84k deployed) we ended at **+$97 PnL**: essentially flat. The paper strategies tracking the same signals ended at a similar net-flat.

The reason wasn't bad strategy, it was size. Our average real trade was **$13.70**; the whale's average trade was **$2,095** (max $182k). We were operating at **1:153** of his size:

- The CLOB minimum (5 shares) absorbed a disproportionate fraction of small positions.
- Fees ate a larger share of each trade than at whale scale.
- The threshold-based reinforcement never got to express conviction properly — we'd hit our `MAX_PER_TRADE` cap long before the whale was done building.

**Fix (partial).** Raised `MAX_PER_TRADE` from $10 → $20 to let higher-conviction trades express themselves. But the real fix is **capital**: at $5–10k of float the arithmetic works; at $500 of float it can't.

**Lesson.** Before deciding a strategy "doesn't work", compute the size ratio against the signal source. A strategy that mirrors a whale at 1:150 is not the same strategy — it's a worse one.

---

## 6. Compare like with like, or don't compare

**What happened.** When reviewing performance I kept producing comparisons that silently mixed scopes — Swisstony's all-day PnL against our 3-hour-since-reset PnL, API-level activity counts against DB-level filtered counts, paper equity including unrealized against real equity realized-only. Each time my user caught me and demanded recalculation. Every conclusion built on a mismatched window was wrong.

**Fix (process, not code).** Every comparison in the dashboard, in the exports, and in ad-hoc analyses now carries:
- An **explicit time window** (ISO UTC, start and end).
- An **explicit source** (BD-monitored vs API-on-chain vs dashboard-aggregated).
- A **unit** (USDC, shares, trades, markets — not interchangeable).

The exporter in `tools/exportar_progreso.py` was rewritten to emit only paper-sim fields tagged with their window, so the public page can't quietly diverge from internal metrics.

**Lesson.** The hardest bug in trading analytics isn't wrong numbers, it's two right numbers compared across different scopes. Tag everything with its window and its source, always.

---

## 7. Realised PnL is the only equity number that matters

**What happened.** Early dashboards summed realized and unrealized PnL into a single "equity" figure. Once a position hit `+300%` unrealized the headline number looked euphoric. Then the market resolved to the other side and the number collapsed. Nothing was wrong with the math; the *semantics* were wrong — treating paper gains as spendable money.

**Fix.** Equity on every dashboard now changes **only** with realised PnL. Unrealized is shown as a separate figure, labelled as such, and never aggregated into the equity line. Enforced in `data_exporter.py` and the public page's reducer.

**Lesson.** On any system where state transitions happen over time (orders → fills → resolution), separate the *committed* number from the *estimated* number in every surface. One drives decisions. The other drives emotions.

---

## 8. Near-resolved CLOB books are not real prices

**What happened.** Our position-valuation loop was reading the top CLOB bid to compute `current_price` for open positions. For markets within hours of resolution, market makers pull liquidity and the top bid falls to `0.001` even on sides that will resolve to `1.00`. Our dashboard screamed "position wiped out" and I almost closed a winning position at the fake price.

**Fix.** For valuation of open positions we now use Polymarket's `/positions` API `currentValue` field — which uses the Polymarket Markets-level price, not the raw CLOB book. CLOB `bids` are only trusted for actually transacting, never for valuing.

**Lesson.** Two different consumers of "price" exist: *what will someone pay me right now* (needs the live book, acceptable for small size) and *what is this position worth* (needs a model-adjusted price). Don't share one field for both.

---

## 9. Idempotency on exit matters as much as on entry

**What happened.** Our `definitive_copy_watcher` loops every few seconds and fires a SELL on our position when the whale sells theirs. During one network glitch, the watcher fired three times before the fill reconciler caught up — it saw our position as still open each time. We ended up selling the same shares twice (fortunately the second order failed at the CLOB).

**Fix.** Every entry and exit now carries a client-order-id that's deterministic on `(agent_id, market_id, side, whale_event_id)`. The second firing is a no-op at Polymarket. The watcher also checks against `in_flight_exits` in memory before issuing anything.

**Lesson.** On any system that acts on streamed events against mutable state, idempotency is a feature of the write path, not of the read path. "I only sell once" is a wish; "the second sell is a no-op" is a design.

---

## 10. Measure the fee drag, don't assume

**What happened.** At one point I was convinced our net flat PnL was "strategy neutral, fees eating the edge". I had no data to back it. When I actually summed the fee column across the 6k live trades, fees were **$X** out of the $84k moved — meaningful at our size but not the whole story. The actual answer was partly size (lesson 5) and partly exit timing (lesson 4). Blaming fees was emotionally satisfying and factually wrong.

**Fix.** The exporter now emits `fees_usdc_cumulative` alongside PnL on every window. Every claim about "fees eating the edge" has to point at that number.

**Lesson.** The most dangerous cause-of-death narrative is the one that fits intuitively but was never measured. Attach a number to every claim about *why* performance was what it was.

---

## Changes that were proposed but **not** shipped

Honesty requires listing the ones that didn't make it, too:

- **MERGE-exit at live CLOB price** (instead of the hard-coded `$0.50` in `definitive_copy_watcher.py`). Proposed and designed, not committed yet. The $0.50 is right on average for a hedged MERGE but wrong on any specific one; the live CLOB bid would be more honest. It wasn't shipped because the live side was paused before this change was tested enough to trust.
- **Lower reinforcement threshold ($50 → $10)**. Would raise our volume-capture ratio against the whale from ~24% to ~70% at scale — but only makes sense with the capital to absorb the extra turnover.

These are documented so the next iteration starts from the right place instead of re-discovering them.

---

## How to read this file as a reviewer

If you're reading this for a hiring loop, the point isn't that the bot made money (it didn't, meaningfully). The point is what the code looks like after it was debugged against a live market: inline `L89…L92` tags that cite the fix, a watcher that's idempotent on exit, a dashboard that refuses to lie about equity, a README that admits which agents are deprioritised and why.

The entries above are the ones I remember because they cost me real money, real data, or real hours. There are plenty I don't remember, which is itself a lesson.
