"""
search_asymmetric.py — Encuentra traders asimétricos de alta calidad
====================================================================
Lee el checkpoint de trader_search.py (883 traders, ya cacheados)
y aplica filtros centrados en profit factor, no solo win rate.

Criterios:
  - PnL total > 0 USDC
  - Profit Factor ≥ 3.0  (avg_win / avg_loss ≥ 3×)
  - Win Rate ≥ 60%
  - Cerradas ≥ 25
  - Total posiciones ≥ 50

Ordena por PnL total descendente y muestra top 20.
"""

import json
import os
from datetime import datetime

CHECKPOINT_PATH = "/tmp/trader_search_checkpoint.json"
REPORTS_DIR     = os.path.join(os.path.dirname(__file__), "reports")

# ── Criterios ─────────────────────────────────────────────────────────────────
MIN_PNL         = 0        # USDC
MIN_PF          = 3.0      # profit factor
MIN_WR          = 0.60     # win rate
MIN_CLOSED      = 25       # posiciones cerradas
MIN_POSITIONS   = 50       # total posiciones (open + closed)
TOP_N           = 20


def load_checkpoint(path: str) -> list:
    with open(path, "r") as f:
        raw = json.load(f)
    # El checkpoint es {wallet: {wallet, name, stats, ...}}
    if isinstance(raw, dict):
        return list(raw.values())
    return raw


def filter_traders(traders: list) -> list:
    result = []
    for t in traders:
        s = t.get("stats", {})
        if not s:
            continue
        pnl     = s.get("pnl", 0) or 0
        pf      = s.get("profit_factor", 0) or 0
        wr      = s.get("win_rate", 0) or 0
        closed  = s.get("closed", 0) or 0
        total   = s.get("total_positions", 0) or 0
        if (pnl   > MIN_PNL and
            pf    >= MIN_PF  and
            wr    >= MIN_WR  and
            closed >= MIN_CLOSED and
            total  >= MIN_POSITIONS):
            result.append(t)
    return sorted(result, key=lambda x: x["stats"]["pnl"], reverse=True)


def _tier(pf: float) -> str:
    if pf >= 5.0: return "🔥 EXCEPCIONAL"
    if pf >= 3.5: return "⭐ EXCELENTE"
    return "✅ BUENO"


def build_report(traders: list, total_scanned: int) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "=" * 60,
        f"  BÚSQUEDA ASIMÉTRICA — {now}",
        f"  Checkpoint: {total_scanned} traders analizados",
        "=" * 60,
        "",
        f"Criterios: PnL>0 · PF≥{MIN_PF} · WR≥{int(MIN_WR*100)}% · "
        f"≥{MIN_CLOSED} cerradas · ≥{MIN_POSITIONS} posiciones",
        "",
        f"Encontrados: {len(traders)} traders  →  mostrando top {min(TOP_N, len(traders))}",
        "",
        "─" * 60,
    ]

    for rank, t in enumerate(traders[:TOP_N], 1):
        s   = t["stats"]
        pnl = s.get("pnl", 0)
        pf  = s.get("profit_factor", 0)
        wr  = s.get("win_rate", 0)
        cl  = s.get("closed", 0)
        tot = s.get("total_positions", 0)
        aw  = s.get("avg_win", 0)
        al  = s.get("avg_loss", 0)
        w   = s.get("wins", 0)
        l   = s.get("losses", 0)

        tier = _tier(pf)
        lines += [
            "",
            f"#{rank:02d}  {t.get('name', 'anon')}   {tier}",
            f"     Wallet: {t['wallet']}",
            f"     PnL:    {pnl:+,.0f} USDC",
            f"     PF:     {pf:.2f}×  (avg win {aw:,.0f} USDC / avg loss {al:,.0f} USDC)",
            f"     WR:     {round(wr*100, 1)}%  ({w}W / {l}L — {cl} cerradas / {tot} total)",
        ]

    lines += [
        "",
        "─" * 60,
        "",
        "RESUMEN POR TIER:",
    ]
    exceptional = [t for t in traders if t["stats"]["profit_factor"] >= 5.0]
    excellent   = [t for t in traders if 3.5 <= t["stats"]["profit_factor"] < 5.0]
    good        = [t for t in traders if 3.0 <= t["stats"]["profit_factor"] < 3.5]
    lines += [
        f"  🔥 Excepcional (PF≥5×):   {len(exceptional)} traders",
        f"  ⭐ Excelente (PF 3.5-5×): {len(excellent)} traders",
        f"  ✅ Bueno (PF 3.0-3.5×):   {len(good)} traders",
        "",
        "=" * 60,
    ]
    return "\n".join(lines)


def main():
    if not os.path.exists(CHECKPOINT_PATH):
        print(f"❌  Checkpoint no encontrado: {CHECKPOINT_PATH}")
        return

    traders = load_checkpoint(CHECKPOINT_PATH)
    total   = len(traders)
    print(f"Cargados {total} traders del checkpoint...")

    filtered = filter_traders(traders)
    report   = build_report(filtered, total)

    print(report)

    os.makedirs(REPORTS_DIR, exist_ok=True)
    filename = f"asymmetric_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    path     = os.path.join(REPORTS_DIR, filename)
    with open(path, "w") as f:
        f.write(report)
    print(f"\nReporte guardado en: {path}")


if __name__ == "__main__":
    main()
