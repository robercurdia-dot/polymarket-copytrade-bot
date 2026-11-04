# Polymarket Insider Agent

A copy-trading bot for [Polymarket](https://polymarket.com) built around a single rule: **mirror the entry and exit of a handpicked whitelist of top wallets with the smallest possible drift, and don't try to be smarter than them.**

The project started with classic take-profit / stop-loss strategies on top of the signals. It has since **pivoted to pure mirror-copy**: our own entry/exit logic sits as a secondary shadow strategy, used only for A/B comparison. Everything the live capital does today is a scaled replica of what the whitelisted trader just did on-chain.

## What "copy at the millimeter" means here

| Layer | Behaviour |
|------|----------|
| **Entry** | A trader on the whitelist buys → we buy the same market, same side, same price, **sized as `swis_trade × SCALE`** (e.g. `×0.0078`). No prediction, no "better entry", no waiting for a dip. |
| **Reinforcement** | Trader keeps buying the same market → we accumulate at the same pace, respecting the cumulative threshold that filters probing. |
| **Exit** | Trader sells → we sell. Trader merges YES+NO shares (= they're done) → we close. Cutoff for lagged exits is enforced per-wallet in `definitive_copy_watcher.py`. |
| **Sizing** | `MIN_TRADE=1` / `MAX_PER_TRADE=20` USDC. A 5.5-share floor avoids CLOB minimums (see `paper_executor.py#L89`). |
| **Guardrails** | Pre-trade risk gate (balance, slippage, allowance, open-position caps). Daily loss circuit-breaker. Idempotency on both entry and exit. |

The system ran live for **~5 weeks (mid-May → mid-June 2026)** doing exactly this against ~$84k of real traded volume, break-even net of fees on ~6k trades. It is now paused on live capital and runs as a continuous paper simulation (see below).

> 📓 **[LESSONS.md](LESSONS.md)** is the honest post-mortem — the ten specific errors that shaped the code, each linked to the file and line of its fix. Read that if you care about how the bot actually got built.

## Primary vs secondary agents

Only one agent decides real trades today: **`DEF_COPY`**, the mirror-copy agent. The others are kept as shadows for comparison and are **not** promoted to real capital unless they beat `DEF_COPY` over a long window on the same signals.

| Agent | Role | Behaviour | Status |
|-------|------|-----------|--------|
| **`DEF_COPY`** | **Primary · Swisstony mirror** | Buys and sells one-to-one with the trader, scaled. | Live-ready |
| `DEF_COPY_05` | Shadow · higher threshold | Same mirror logic, threshold = $500. | Paper only |
| `DEF_WIGGLEW` | Shadow · WiggleW mirror | Same, different wallet (`×0.02`). | Paper only |
| `DEF_KINDER` | Shadow · KinderSman mirror | Same, different wallet (`×0.01`). | Paper only |
| `DEF_TP` | Comparison baseline | Our own TP/SL (1.8× / 0.8×) on the trader's entries, ignoring their exits. | Paper only — explicitly deprioritised |
| `DEF_HOLD` | Comparison baseline | Hold until market resolves. | Paper only |

The shift was deliberate: whenever `DEF_TP` outperformed on a single window it was because of noise; on long enough windows the trader's own exit beat our rules. The whole point of a whitelist is that the picked wallets are better than us at *when* to exit, not just *where* to enter. So we stopped trying.

## Architecture

```
┌──────────────────────┐    signals    ┌──────────────────────┐
│  monitor.py          │ ────────────▶ │  paper_executor.py   │
│  (whitelist polling, │               │  (all shadow agents) │
│   2 s interval,      │               └──────────────────────┘
│   paginated)         │                           │
│                      │   mirror signal           │
│                      │   (DEF_COPY only)         ▼
│                      │ ────────────▶ ┌──────────────────────┐
└──────────────────────┘               │ real_signal_router.py│
                                       │  → executor.py       │
                                       │  → Polymarket CLOB   │
                                       └──────────────────────┘
                                                   │
                                                   ▼
                                       ┌──────────────────────┐
                                       │ definitive_copy_     │
                                       │ watcher.py           │
                                       │  — watches trader    │
                                       │    SELL + MERGE      │
                                       │  — closes our pos    │
                                       │    with the same     │
                                       │    size fraction     │
                                       └──────────────────────┘
```

Supporting modules:

| File | Responsibility |
|------|----------------|
| `monitor.py` | Fast polling loop, pagination (MAX_PAGES=5), signal normalisation, COBERTURA filter bypass for designated copy-targets. |
| `data_api.py` | Thin adapter over Polymarket Data API v2 (v1 was retired Nov 2026) returning v1-shaped responses so the rest of the code is unchanged. |
| `paper_executor.py` | Opens/closes paper trades for every shadow agent. Shares-floor bump, per-agent scale/threshold/cap. |
| `real_signal_router.py` | Promotes `DEF_COPY` signals to real execution. Idempotency + daily-loss limit. |
| `executor.py` | Low-level CLOB client wrapper (sign, place, cancel, poll). |
| `definitive_copy_watcher.py` | Watches each whitelisted wallet for SELL / MERGE events on markets where we are open, closes our position with the matching size fraction. |
| `risk_manager.py` | Pre-trade sanity checks: balance, slippage, caps. |
| `database.py` | SQLite schema (WAL) for signals, paper/real trades, equity snapshots. |
| `data_exporter.py` | Builds `web/data.json` consumed by the local dashboard. |
| `tools/construir_whitelist.py` | Rebuilds the wallet whitelist from Polymarket's monthly leaderboard. |
| `tools/exportar_progreso.py` + `publicar_web.sh` | Exports paper-sim progress and pushes to `gh-pages` every 5 min. |
| `main.py` | Supervises all loops, exposes `/health`, serves the local dashboard. |

## Quick start

```bash
# 1. Clone and set up Python environment
git clone https://github.com/robercurdia-dot/polymarket-copytrade-bot.git
cd polymarket-copytrade-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Edit .env — see "Configuration" below

# 3. Initialise the database
python3 -c "import database; database.init_db()"

# 4. Run (paper mode by default)
./start.sh
```

The local dashboard is served at `http://localhost:8080`.

## Paper simulation and public progress page

The bot can run as a pure paper simulation (no keys, no real money) and publish its progress as a static page on GitHub Pages — useful for showing the strategy's behaviour live without exposing the trading wallet.

```bash
cp .env.example .env
# Leave POLY_* keys empty; PAPER_TRADING=true, REAL_TRADING_ENABLED=false

python3 tools/construir_whitelist.py   # rebuilds whitelist from monthly leaderboard (~8 min)
./start.sh                              # paper mode
./tools/publicar_web.sh                 # in another terminal — exports + pushes to gh-pages every 5 min
```

Then enable Pages once: *Settings → Pages → Branch: `gh-pages`, folder `/ (root)`*. The page reads
`progreso.json` and `historial.json`, built by `tools/exportar_progreso.py` from the simulation database only. It never reads real-trade tables.

## Configuration

All runtime behaviour is controlled by environment variables. See `.env.example` for the full list.

### Polymarket credentials (real trading only)
```
POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE
POLY_WALLET_ADDRESS, POLY_EOA_ADDRESS, POLY_PRIVATE_KEY
POLY_SIGNATURE_TYPE
```

### Wallets to mirror
```
COPY_WALLETS=0xADDR:Name:0.0107,0xADDR:Name:0.0015   # address:name:scale — DEF_COPY mirrors exactly these
PAPER_AGENTES=DEF_COPY,DEF_TP,DEF_HOLD                # paper agents that run (others are deactivated)
DEF_MAX_SIGNAL_AGE=120                                # seconds; older trader buys are not copied at today's price
```
`tools/seleccionar_wallets.py --escribir-env` fills `COPY_WALLETS` and the whitelists from the three tests
below. Without `COPY_WALLETS`, `DEF_COPY` falls back to Swisstony and the legacy per-wallet scales:

### Per-wallet scale (legacy wallets)
```
DEFINITIVE_SWIS_SCALE=0.0078       # we mirror Swisstony at 0.78 % of his size
DEFINITIVE_SOARIN_SCALE=0.005
DEFINITIVE_WIGGLEW_SCALE=0.02
DEFINITIVE_KINDER_SCALE=0.01
```

### Common sizing gates
```
DEFINITIVE_THRESHOLD=50            # min cumulative USD before an entry triggers
DEFINITIVE_MIN_TRADE=1             # USDC floor per trade
DEFINITIVE_MAX_PER_TRADE=20        # USDC ceiling per trade
DEFINITIVE_MAX_SLIPPAGE=0.15       # slippage cap for DEF_COPY orders
DEF_MAX_SIGNAL_AGE=120             # discard whale signals older than this (seconds)
                                   # protects against buying "now" at today's price
                                   # when the whale entered hours ago
```

### Risk gates
```
REAL_COPY_MAX_OPEN=40
MAX_SLIPPAGE=0.02
REAL_D_WALLET_WHITELIST=0x...      # comma-separated — the wallets we mirror
REAL_WALLET_BLACKLIST=0x...
```

### Trading mode
```
PAPER_TRADING=false
REAL_TRADING_ENABLED=true
REAL_COPY_ENABLED=false            # master kill-switch for copy-trading
REAL_COPY_TRADE_SIZE=5             # USDC floor for the real path
```

## Known-good trader selection

`tools/seleccionar_wallets.py` screens Polymarket's public leaderboard (monthly and all-time) and keeps the wallets that pass **four** concrete tests, ranked by Sharpe. The thresholds below are the current defaults; each is a command-line option on the selector. Each surviving wallet's scale is then set so its median position per market is copied at ~10 USDC, and written to `COPY_WALLETS` (see `copy_wallets.py`).

### Criterion 1 — Sharpe-equivalent > 1 on 30 days

Per-day realised P&L series built from positions *closed* in the window (not opened). `sharpe = mean(daily_pnl) / stdev(daily_pnl) * sqrt(30)`. Requires **at least 15 closes** in the 30 days; otherwise the variance estimate is noise and we reject regardless of the mean.

### Criterion 2 — ≥ 1 year of *consistent* activity, not a single-market whale

- Active in **at least 10 of the last 12 calendar months** (no multi-month blackouts).
- **At least 15 distinct markets** in the last 30 days.
- **No single market > 25 % of their 30-day traded volume** — a one-shot YOLO wallet isn't repeatable.

### Criterion 3 — Exit discipline

The `definitive_copy_watcher` only helps us if the whale actually *has* exits to mirror.

- **At least 10 SELL or MERGE events** in the last 30 days.
- Those exits must equal **at least 10 %** of their BUY count — a trader who only buys and resolves is useless to us.

### Criterion 4 — Size-ratio ≤ 1:150 at our current cap

Direct application of **[Lesson 5](LESSONS.md#5-we-were-operating-at-1153-of-the-whales-size--the-math-doesnt-work-at-that-scale)**. For each candidate wallet we compute:

```
ratio = median_position_size_per_market(whale) / DEFINITIVE_MAX_PER_TRADE
```

If `ratio > 150` we drop the wallet, no matter how good their Sharpe is. The 1:153 ratio we ran live was flat precisely because of this; we're not going to pick traders that are structurally worse than the case we already know doesn't work.

The implication, for the record: this criterion is a **capital-bound filter**, not a strategy filter. Raising `DEFINITIVE_MAX_PER_TRADE` above $20 (which requires more float than we have today) would re-open some of the whales currently rejected — but doing it without the capital just scales up our per-trade risk without improving our edge, which is also what lesson 5 warns against.

### Continuous rotation

`tools/wallet_rotator.py` re-runs the four tests on a rolling basis. Wallets that drift below a 7-day Sharpe floor, miss the exit-discipline threshold two months in a row, or cross back above the size-ratio limit are flagged for removal from the live whitelist.

## Safety

- Real trading is off by default and gated by `REAL_COPY_ENABLED`.
- Every real order passes through `risk_manager.safety_check` before signing.
- Daily loss limits halt real trading automatically.
- Live orders are reconciled with the CLOB every 60 s (`reconcile` loop).
- Entry and exit are both idempotent — a repeated trader signal never results in a double order on our side.
- Alerts go to Telegram if configured, otherwise to the log.

## Tech stack

- Python 3.11, thread-per-loop model (no asyncio).
- SQLite in WAL mode for signals, paper/real trades, and equity snapshots.
- Polymarket CLOB + Data API v2 HTTP clients.
- `py-clob-client` / `py-builder-signing-sdk` for signing.
- Static dashboard in `web/` (local) and `publico/` (GitHub Pages) — vanilla HTML/JS, no build step.

## Status

Live capital paused since mid-June 2026 (scaling required more float than I had at the time). Paper simulation keeps running against live on-chain data and publishes its own progress page. All the engineering — mirror logic, risk gate, exit watcher, dashboards, paper/real parallel infra — is in this repo and still works as published.

## License

Private — all rights reserved. Not for redistribution.
