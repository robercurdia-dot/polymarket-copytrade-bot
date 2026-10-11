# Polymarket Insider Agent

A multi-agent copy-trading system that monitors on-chain activity of top-performing Polymarket traders and replicates their positions with configurable risk, position-sizing, and exit strategies.

## Overview

The system detects, classifies, and acts on signals from a dynamically updated whitelist of profitable wallets. It runs **paper-trading** and **real-trading** agents in parallel so that any new rule, threshold, or scale factor can be validated against live market data before touching capital.

Current production configuration tracks ~100 wallets with sub-second signal detection and supports three independent exit strategies (take-profit/stop-loss, copy-exit on trader merge, time-based exits) running simultaneously for A/B comparison.

## Key Features

- **Real-time on-chain monitoring** — polls Polymarket's data API every 2 s for whitelisted wallets, with pagination and automatic recovery from network interruptions.
- **Multi-agent paper trading** — three strategies (`DEF_TP`, `DEF_COPY`, `DEF_COPY_05`) replay every signal in parallel so the live P&L of each strategy is observable before promoting one to real capital.
- **Threshold-based entry** — a trader's cumulative volume in a market must cross a configurable threshold before an entry is triggered, filtering out probing and small positions.
- **Scale factor** — real trades are sized as a fraction of the trader's position so the bot can mirror a $5 M portfolio with a $500 account.
- **Resolver and risk gate** — a dedicated risk-manager checks slippage, open-position limits, allowance state, and wallet balance before every real order.
- **Live dashboard** — a lightweight web UI (served from `web/`) exposes real-time equity curves, per-agent P&L, open positions, and signal queue.
- **Pluggable research agents** — leaderboard scraping, trader-reputation scoring, and insider-detection modules continuously propose new wallets for the whitelist.

## Architecture

```
┌──────────────────────┐    signals    ┌──────────────────────┐
│  monitor.py          │ ────────────▶ │  paper_executor.py   │
│  (whitelist polling, │               │  (3 shadow agents)   │
│   2 s interval)      │               └──────────────────────┘
│                      │                           │
│                      │    high-confidence        │
│                      │    signals                ▼
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
                                       │ (exit on MERGE/SELL) │
                                       └──────────────────────┘
```

Supporting modules:

| File | Responsibility |
|------|----------------|
| `monitor.py` | Fast polling loop and signal normalization. |
| `paper_executor.py` | Opens/closes paper trades for all shadow agents. |
| `real_signal_router.py` | Promotes paper signals to real execution with scale, idempotency, and daily-loss limits. |
| `executor.py` | Low-level CLOB client wrapper (sign, place, cancel, poll). |
| `definitive_copy_watcher.py` | Mirrors the trader's exit event (merge, sell, resolve). |
| `risk_manager.py` | Pre-trade sanity checks: balance, slippage, caps. |
| `database.py` | SQLite schema and migrations for signals, trades, positions, equity snapshots. |
| `data_exporter.py` | Builds `web/data.json` consumed by the dashboard. |
| `web/dashboard.html` | Single-page vanilla-JS UI. |
| `main.py` | Supervises all loops, exposes `/health`, serves the dashboard. |

## Quick start

```bash
# 1. Clone and set up Python environment
git clone https://github.com/brodyautomates/polymarket-pipeline.git
cd polymarket-pipeline
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt   # see requirements inside each module

# 2. Configure environment
cp .env.example .env
# Edit .env — see "Configuration" below

# 3. Initialize the database
python3 -c "import database; database.init_db()"

# 4. Run (paper mode is the default)
./start.sh
```

The dashboard is served at `http://localhost:8080`.

## Configuration

All runtime behavior is controlled by environment variables. Key groups:

### Polymarket credentials
```
POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE
POLY_WALLET_ADDRESS, POLY_EOA_ADDRESS, POLY_PRIVATE_KEY
POLY_SIGNATURE_TYPE
```

### Agent sizing (per-strategy)
```
DEFINITIVE_SWIS_SCALE=0.00784   # fraction of trader's position
DEFINITIVE_THRESHOLD=50         # min cumulative USD before entry
DEFINITIVE_MIN_TRADE=1          # floor per trade
DEFINITIVE_MAX_PER_TRADE=20     # cap per trade
```

### Risk gates
```
MAX_OPEN_POSITIONS=50
MAX_SLIPPAGE=0.02
REAL_D_WALLET_WHITELIST=0x...   # comma-separated
REAL_WALLET_BLACKLIST=0x...
```

### Trading mode
```
PAPER_TRADING=false
REAL_TRADING_ENABLED=true
REAL_COPY_ENABLED=false         # master kill-switch for copy-trading
```

See `.env.example` for the full list.

## Experiment-driven development

Every behavioral change — a new threshold, a new exit rule, a scale change — is first run as a paper-only shadow agent next to the production path. The dashboard surfaces each agent's equity curve, win rate, Sharpe-equivalent, and capital-at-risk so that changes can be judged on real signals before any real-capital promotion.

A typical cycle:

1. Add a new shadow agent in `paper_executor.py` (new agent_id).
2. Let it run alongside the current production path for 24–72 h.
3. Compare its equity curve to the production agent's.
4. If better, promote it by swapping `agent_id` in `real_signal_router.py`.

## Known-good trader selection

The whitelist (`REAL_D_WALLET_WHITELIST`) is maintained from:

- Polymarket's public leaderboards (`leaderboard_analyzer.py`).
- A reputation score computed on-chain (`reputation_tracker.py`).
- Manual research pinned in the trader-criteria doc.

Wallets rotate automatically out of the whitelist if their 7-day Sharpe drops below a floor (`tools/wallet_rotator.py`).

## Safety

- Real trading is off by default and gated by `REAL_COPY_ENABLED`.
- Every real order passes through `risk_manager.safety_check` before signing.
- Daily loss limits halt real trading automatically.
- Live orders are reconciled with the CLOB every 60 s (`reconcile` loop).
- Hooks for Telegram alerts on fills, errors, and risk-gate rejections.

## Tech stack

- Python 3.11 (asyncio-free, thread-per-loop model)
- SQLite (WAL mode) for signals, trades, equity snapshots
- Polymarket CLOB and data-API HTTP clients
- Web3 / py-clob-client for signing and on-chain reads
- Vanilla HTML/JS dashboard (no build step)

## Status

Actively running in production. Design notes, incident reports, and strategy iterations are tracked internally.

## License

Private — all rights reserved. Not for redistribution.
