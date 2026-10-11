"""
wallet_rotator.py — Estudio recurrente de traders copiados.

Ejecuta cada 48h (vía thread en main.py o cron). Genera reporte completo
en reports/wallet_rotation_YYYYMMDD_HHMM.md con:
  - Performance por wallet (paper + real)
  - Rolling degradation (primeros 20 vs últimos 20)
  - Discovery scan del leaderboard top 200
  - Recomendaciones de promoción / degradación

NO modifica .env automáticamente — solo emite recomendaciones.
La decisión final la toma el operador.

USO:
  python tools/wallet_rotator.py            # ejecuta y guarda reporte
  python tools/wallet_rotator.py --skip-discovery  # solo análisis interno (rápido)
"""
import argparse
import os
import sys
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import leaderboard_analyzer as la

DB = "polymarket_insider.db"
REPORTS_DIR = Path("reports")
REPORTS_DIR.mkdir(exist_ok=True)

WHITELIST = {a.strip().lower() for a in os.getenv("REAL_WALLET_WHITELIST","").split(",") if a.strip()}
CLOSED_STATES = ("resolved_win","resolved_loss","tp_hit","sl_hit","timeout")


def _parse_dt(s):
    if not s: return None
    try:
        return datetime.fromisoformat(str(s).replace("Z","+00:00"))
    except Exception:
        return None


def section_performance(conn, lines):
    """Sección 1: performance por wallet (paper C + real)."""
    paper = conn.execute("""
        SELECT wallet_copied, status, niche, entry_at, exit_at,
               realized_pnl_net, agent_id
        FROM paper_trades WHERE wallet_copied IS NOT NULL AND agent_id='C'
    """).fetchall()
    real = conn.execute("""
        SELECT bt.id, bt.status, bt.outcome, bt.pnl_usdc, bt.executed_at,
               dt.wallet_address
        FROM bot_trades bt LEFT JOIN detected_trades dt ON bt.detected_trade_id=dt.id
        WHERE bt.paper_trade=0 AND dt.wallet_address IS NOT NULL
    """).fetchall()

    per_wallet = defaultdict(list)
    for r in paper:
        if r["status"] in CLOSED_STATES:
            per_wallet[r["wallet_copied"]].append(r)
    real_per_wallet = defaultdict(list)
    for r in real:
        real_per_wallet[r["wallet_address"]].append(r)

    lines.append("## 1. Performance por wallet")
    lines.append("")
    lines.append("| wallet | en_wl | n_C | wr_C | ppt_C | n_real | wr_real | pnl_real |")
    lines.append("|---|---|---|---|---|---|---|---|")
    all_w = sorted(set(per_wallet) | set(real_per_wallet))
    for w in all_w:
        in_wl = "✓" if w.lower() in WHITELIST else "—"
        rows = per_wallet[w]
        n = len(rows)
        pnl = sum(r["realized_pnl_net"] or 0 for r in rows)
        wins = sum(1 for r in rows if (r["realized_pnl_net"] or 0) > 0)
        wr = (wins/n*100) if n else 0
        ppt = pnl/n if n else 0
        rc = [r for r in real_per_wallet[w] if r["status"]=="closed" and r["outcome"] in ("won","lost")]
        rn = len(rc); rwin = sum(1 for r in rc if r["outcome"]=="won")
        rwr = (rwin/rn*100) if rn else 0
        rpnl = sum(r["pnl_usdc"] or 0 for r in rc)
        lines.append(f"| {w[:10]}…{w[-4:]} | {in_wl} | {n} | {wr:.1f}% | ${ppt:.2f} | "
                     f"{rn} | {rwr:.1f}% | ${rpnl:.2f} |")
    return per_wallet, real_per_wallet


def section_degradation(per_wallet, lines, window=20):
    lines.append("")
    lines.append(f"## 2. Degradación · paper C primeros {window} vs últimos {window}")
    lines.append("")
    lines.append("| wallet | first_wr | first_ppt | last_wr | last_ppt | Δppt | señal |")
    lines.append("|---|---|---|---|---|---|---|")
    warnings = []
    for w in sorted(per_wallet):
        rows = sorted([r for r in per_wallet[w] if r["exit_at"]], key=lambda r: r["exit_at"])
        if len(rows) < 2*window: continue
        first = rows[:window]; last = rows[-window:]
        f_pnls = [r["realized_pnl_net"] or 0 for r in first]
        l_pnls = [r["realized_pnl_net"] or 0 for r in last]
        f_wr = sum(1 for p in f_pnls if p>0)/window*100
        l_wr = sum(1 for p in l_pnls if p>0)/window*100
        f_ppt = sum(f_pnls)/window; l_ppt = sum(l_pnls)/window
        delta = l_ppt - f_ppt
        sig = "—"
        in_wl = w.lower() in WHITELIST
        if delta < -3 and l_ppt < 0:
            sig = "⚠ DEGRADANDO"
            warnings.append((w, f_ppt, l_ppt, in_wl))
        elif delta > 3:
            sig = "↑ mejorando"
        lines.append(f"| {w[:10]}…{w[-4:]} | {f_wr:.0f}% | ${f_ppt:.2f} | "
                     f"{l_wr:.0f}% | ${l_ppt:.2f} | ${delta:+.2f} | {sig} |")
    return warnings


def section_real_timewindow(real_per_wallet, lines):
    all_real = []
    for rows in real_per_wallet.values():
        all_real.extend([r for r in rows if r["status"]=="closed" and r["outcome"] in ("won","lost")])
    all_real.sort(key=lambda r: r["executed_at"] or "")
    lines.append("")
    lines.append("## 3. Real trading · ventanas temporales")
    lines.append("")
    if not all_real:
        lines.append("Sin trades reales cerrados aún.")
        return
    for ws in (10, 20, 30, 50):
        if len(all_real) >= ws:
            tail = all_real[-ws:]
            pnls = [r["pnl_usdc"] or 0 for r in tail]
            wins = sum(1 for p in pnls if p>0)
            lines.append(f"- Últimos {ws}: WR {wins/ws*100:.1f}% · ppt ${sum(pnls)/ws:.2f} · "
                         f"pnl_total ${sum(pnls):.2f}")
    # Detección de drawdown reciente
    if len(all_real) >= 10:
        last10 = all_real[-10:]
        p10 = sum(r["pnl_usdc"] or 0 for r in last10)
        if p10 < 0:
            lines.append("")
            lines.append(f"⚠ Últimos 10 trades reales con PnL negativo ${p10:.2f}")


def section_promotion_candidates(per_wallet, lines):
    """Promoción: ≥30 closed C · WR≥55% · ppt≥$2 · niche sports/esports"""
    lines.append("")
    lines.append("## 4. Candidatos a promoción (no en whitelist actual)")
    lines.append("")
    cands = []
    for w in per_wallet:
        rows = per_wallet[w]
        if len(rows) < 30: continue
        if w.lower() in WHITELIST: continue
        pnls = [r["realized_pnl_net"] or 0 for r in rows]
        wr = sum(1 for p in pnls if p>0)/len(rows)*100
        ppt = sum(pnls)/len(rows)
        by_n = defaultdict(int)
        for r in rows: by_n[r["niche"] or "UNCLASSIFIED"] += 1
        primary = max(by_n, key=by_n.get)
        if primary not in ("sports","esports"): continue
        if wr >= 55 and ppt >= 2.0:
            cands.append((w, len(rows), wr, ppt, primary))
        elif ppt >= 4.0 and wr >= 45:
            cands.append((w, len(rows), wr, ppt, primary + " (asym)"))
    if not cands:
        lines.append("Ninguna wallet cumple criterios de promoción.")
        return
    lines.append("| wallet | n_C | wr | ppt | primary |")
    lines.append("|---|---|---|---|---|")
    for w, n, wr, ppt, niche in sorted(cands, key=lambda x: -x[3]):
        lines.append(f"| `{w}` | {n} | {wr:.1f}% | ${ppt:.2f} | {niche} |")


def section_discovery(lines, skip=False):
    lines.append("")
    lines.append("## 5. Discovery · scan leaderboard top 100")
    lines.append("")
    if skip:
        lines.append("(skip-discovery flag activado)")
        return
    try:
        top = la.get_leaderboard(limit=100)
    except Exception as e:
        lines.append(f"Error leaderboard: {e}")
        return
    now = datetime.now(timezone.utc)
    cutoff_30d = now - timedelta(days=30)

    def scan(trader):
        wallet = (trader.get("proxyWallet") or "").lower()
        if not wallet or wallet in WHITELIST:
            return None
        try:
            raw = la._fetch_positions_unbiased(wallet, max_positions=300)
        except Exception:
            return None
        if not raw: return None
        closed = [p for p in raw if la._is_closed(p, now)
                  and float(p.get("initialValue", p.get("size",0)) or 0) > 0]
        if len(closed) < 30: return None
        closed_30d = [p for p in closed
                       if (_parse_dt(p.get("endDate") or p.get("endDateIso")) or now) >= cutoff_30d]
        if len(closed_30d) < 10: return None
        by_niche = la.categorize_by_niches(closed)
        ncnt = {n: len(lst) for n, lst in by_niche.items() if lst}
        if not ncnt: return None
        primary = max(ncnt, key=ncnt.get)
        share = ncnt[primary]/len(closed)
        if primary not in ("sports","esports") or share < 0.70:
            return None
        pnls = [float(p.get("cashPnl",0) or 0) for p in closed]
        wins = sum(1 for x in pnls if x>0)
        wr = wins/len(closed)
        ppt = sum(pnls)/len(closed)
        if wr < 0.55 or ppt < 2.0: return None
        return {"wallet": trader.get("proxyWallet"), "user": trader.get("name") or "?",
                "n": len(closed), "n30d": len(closed_30d),
                "wr": wr*100, "ppt": ppt, "niche": primary, "share": share*100}

    found = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        for fut in as_completed({ex.submit(scan, t): t for t in top}):
            try: r = fut.result()
            except: r = None
            if r: found.append(r)
    found.sort(key=lambda r: -(r["ppt"] * (r["wr"]/100)))

    lines.append(f"Candidatos encontrados: {len(found)}")
    if found:
        lines.append("")
        lines.append("| wallet | n | 30d | wr | ppt | niche | share |")
        lines.append("|---|---|---|---|---|---|---|")
        for r in found[:15]:
            lines.append(f"| `{r['wallet']}` | {r['n']} | {r['n30d']} | "
                         f"{r['wr']:.1f}% | ${r['ppt']:.2f} | {r['niche']} | {r['share']:.0f}% |")


def section_summary(lines, warnings):
    lines.append("")
    lines.append("## 6. Resumen ejecutivo")
    lines.append("")
    if warnings:
        lines.append(f"⚠ {len(warnings)} wallet(s) con señales de degradación:")
        for w, f_ppt, l_ppt, in_wl in warnings:
            tag = "EN WHITELIST" if in_wl else "no en wl"
            lines.append(f"  - `{w}` ({tag}): ppt cayó de ${f_ppt:.2f} → ${l_ppt:.2f}")
    else:
        lines.append("Sin degradación significativa detectada.")
    lines.append("")
    lines.append("**Recomendación**: revisar candidatos sec.4 (promoción) y sec.5 (discovery).")
    lines.append("Aplicar cambios manualmente en `.env REAL_WALLET_WHITELIST`.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-discovery", action="store_true",
                    help="Skip leaderboard scan (más rápido, solo análisis interno)")
    args = ap.parse_args()

    ts = datetime.now()
    fname = REPORTS_DIR / f"wallet_rotation_{ts.strftime('%Y%m%d_%H%M')}.md"

    L = []
    L.append(f"# Wallet Rotation Report")
    L.append("")
    L.append(f"Generado: {ts.isoformat(timespec='seconds')}")
    L.append(f"Whitelist actual ({len(WHITELIST)}): " + ", ".join(sorted(WHITELIST)))
    L.append("")

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    per_wallet, real_per_wallet = section_performance(conn, L)
    warnings = section_degradation(per_wallet, L)
    section_real_timewindow(real_per_wallet, L)
    section_promotion_candidates(per_wallet, L)
    section_discovery(L, skip=args.skip_discovery)
    section_summary(L, warnings)
    conn.close()

    fname.write_text("\n".join(L) + "\n")
    print(f"[ROTATOR] Reporte: {fname}")
    print(f"[ROTATOR] Warnings: {len(warnings)}")
    return fname


if __name__ == "__main__":
    main()
