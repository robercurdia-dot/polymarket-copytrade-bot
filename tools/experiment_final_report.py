"""
experiment_final_report.py — Reporte final del experimento multi-agente Fase 2.6
=================================================================================
Genera un reporte multi-métrica con datos puros, sin recomendación de configuración
ni lenguaje celebrativo. La decisión de Fase 3a la toma el usuario manualmente.

Output:
  - stdout (imprime el reporte completo)
  - reports/experiment_final_<timestamp>.md (markdown persistido)

Uso:
    python tools/experiment_final_report.py
    python tools/experiment_final_report.py --output reports/custom.md
    python tools/experiment_final_report.py --markdown-only

Métricas calculadas:
  1. Rentabilidad:    avg, mediana, percentiles, top/bottom 10, concentración
  2. Riesgo:          σ, Sharpe, Sortino, max drawdown, tiempo en DD, racha L
  3. Operativas:      tiempos por bucket, capital bloqueado, rotación
  4. Robustez:        sin top5/bottom5, cuartiles temporales
  5. Posiciones abiertas: mark-to-market estimado
  Bonus: histograma textual por agente
"""
import argparse
import os
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "polymarket_insider.db"
REPORTS_DIR = ROOT / "reports"
REPORTS_DIR.mkdir(exist_ok=True)

AGENTS = ["A", "B", "C"]
WIN_STATUSES  = {"tp_hit", "resolved_win"}
LOSS_STATUSES = {"sl_hit", "resolved_loss", "timeout", "disputed"}
CLOSED_STATUSES = WIN_STATUSES | LOSS_STATUSES


# ── Helpers ─────────────────────────────────────────────────────────

def fetch_closed_trades(conn, agent_id: str) -> list[dict]:
    c = conn.cursor()
    c.execute("""
        SELECT id, status, entry_at, exit_at, entry_price, exit_price,
               entry_size_usdc, realized_pnl_net, realized_pnl_gross,
               fees_estimated, market_question, niche, is_hold_to_resolve
        FROM paper_trades
        WHERE agent_id = ? AND status IN ('tp_hit','sl_hit','resolved_win','resolved_loss','disputed','timeout')
        ORDER BY entry_at
    """, (agent_id,))
    cols = ["id","status","entry_at","exit_at","entry_price","exit_price",
            "entry_size_usdc","pnl_net","pnl_gross","fees","market","niche","is_h2r"]
    return [dict(zip(cols, r)) for r in c.fetchall()]


def fetch_open_trades(conn, agent_id: str) -> list[dict]:
    c = conn.cursor()
    c.execute("""
        SELECT id, entry_at, entry_price, last_check_price, entry_size_usdc,
               entry_shares, fees_estimated, market_question, is_hold_to_resolve
        FROM paper_trades
        WHERE agent_id = ? AND status = 'open'
    """, (agent_id,))
    cols = ["id","entry_at","entry_price","last_price","entry_size_usdc",
            "shares","fees","market","is_h2r"]
    return [dict(zip(cols, r)) for r in c.fetchall()]


def fmt_money(x, decimals=2):
    if x is None:
        return "—"
    s = f"${abs(x):,.{decimals}f}"
    return f"-{s}" if x < 0 else f"+{s}" if x > 0 else f"${0:.{decimals}f}"


def percentile(sorted_xs: list[float], p: float) -> float:
    if not sorted_xs:
        return float("nan")
    n = len(sorted_xs)
    if n == 1:
        return sorted_xs[0]
    k = (n - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, n - 1)
    if f == c:
        return sorted_xs[f]
    return sorted_xs[f] + (sorted_xs[c] - sorted_xs[f]) * (k - f)


def hours_between(a_iso: str, b_iso: str) -> float | None:
    if not a_iso or not b_iso:
        return None
    try:
        a = datetime.fromisoformat(a_iso)
        b = datetime.fromisoformat(b_iso)
        return (b - a).total_seconds() / 3600.0
    except Exception:
        return None


# ── 1. Rentabilidad ─────────────────────────────────────────────────

def rentabilidad(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0}
    pnls = [t["pnl_net"] or 0.0 for t in trades]
    pnls_sorted = sorted(pnls)
    total = sum(pnls)
    n = len(pnls)
    wins = [t for t in trades if t["status"] in WIN_STATUSES]
    wr = len(wins) / n if n else 0
    # Concentración: top 10% trades por PnL absoluto positivo
    pos_pnls = sorted([p for p in pnls if p > 0], reverse=True)
    top10_n = max(1, int(round(len(pos_pnls) * 0.10)))
    top10_sum = sum(pos_pnls[:top10_n]) if pos_pnls else 0
    pos_total = sum(pos_pnls)
    concentration_pct = (top10_sum / pos_total * 100) if pos_total > 0 else 0

    # Top/bottom 10
    by_pnl = sorted(trades, key=lambda t: t["pnl_net"] or 0, reverse=True)
    top10 = by_pnl[:10]
    bot10 = by_pnl[-10:][::-1]

    return {
        "n": n,
        "wr": wr,
        "total": total,
        "avg": total / n,
        "median": statistics.median(pnls),
        "p5":  percentile(pnls_sorted, 5),
        "p10": percentile(pnls_sorted, 10),
        "p25": percentile(pnls_sorted, 25),
        "p50": percentile(pnls_sorted, 50),
        "p75": percentile(pnls_sorted, 75),
        "p90": percentile(pnls_sorted, 90),
        "p95": percentile(pnls_sorted, 95),
        "top10": top10,
        "bot10": bot10,
        "concentration_top10pct": concentration_pct,
        "top10_count": top10_n,
    }


# ── 2. Riesgo ───────────────────────────────────────────────────────

def riesgo(trades: list[dict]) -> dict:
    if not trades:
        return {}
    pnls = [t["pnl_net"] or 0.0 for t in trades]
    n = len(pnls)
    avg = sum(pnls) / n
    sigma = statistics.stdev(pnls) if n > 1 else 0
    sharpe = (avg / sigma) if sigma > 0 else None

    neg_pnls = [p for p in pnls if p < 0]
    if len(neg_pnls) > 1:
        sigma_neg = statistics.stdev(neg_pnls)
        sortino = (avg / sigma_neg) if sigma_neg > 0 else None
    else:
        sortino = None

    # Drawdown máximo y tiempo en drawdown
    sorted_trades = sorted(trades, key=lambda t: t["exit_at"] or t["entry_at"])
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    in_dd_count = 0
    for t in sorted_trades:
        cum += t["pnl_net"] or 0
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd
        if cum < peak:
            in_dd_count += 1
    time_in_dd_pct = (in_dd_count / len(sorted_trades) * 100) if sorted_trades else 0

    # Racha máxima de losses consecutivas (un trade es loss si pnl_net < 0)
    max_loss_streak = 0
    cur = 0
    for t in sorted_trades:
        if (t["pnl_net"] or 0) < 0:
            cur += 1
            if cur > max_loss_streak:
                max_loss_streak = cur
        else:
            cur = 0

    return {
        "sigma": sigma,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_dd,
        "time_in_dd_pct": time_in_dd_pct,
        "max_loss_streak": max_loss_streak,
        "n_neg": len(neg_pnls),
        "n_pos": n - len(neg_pnls),
    }


# ── 3. Operativas ───────────────────────────────────────────────────

def operativas(trades: list[dict], opens: list[dict],
               started_at_dt: datetime | None) -> dict:
    by_status = {}
    for s in CLOSED_STATUSES:
        bucket = [t for t in trades if t["status"] == s]
        if bucket:
            hours = [hours_between(t["entry_at"], t["exit_at"]) for t in bucket]
            hours = [h for h in hours if h is not None]
            by_status[s] = {
                "n": len(bucket),
                "avg_hours": (sum(hours) / len(hours)) if hours else None,
            }

    # Distribución de duración para todos los cerrados (útil sobre todo en C)
    all_hours = sorted([
        h for h in (hours_between(t["entry_at"], t["exit_at"]) for t in trades)
        if h is not None
    ])
    dur_pct = {}
    if all_hours:
        for p in (5, 25, 50, 75, 90, 95):
            dur_pct[f"p{p}"] = percentile(all_hours, p)
        dur_pct["min"] = all_hours[0]
        dur_pct["max"] = all_hours[-1]

    # Capital simultáneo bloqueado promedio:
    # Para cada cerrado, la posición existió desde entry_at hasta exit_at con $entry_size_usdc.
    # Para cada abierto, desde entry_at hasta ahora.
    # Construimos eventos (timestamp, +size) y (timestamp, -size), ordenamos, integramos.
    events = []
    for t in trades:
        try:
            ea = datetime.fromisoformat(t["entry_at"]) if t["entry_at"] else None
            xa = datetime.fromisoformat(t["exit_at"]) if t["exit_at"] else None
            if ea: events.append((ea, +float(t["entry_size_usdc"] or 0)))
            if xa: events.append((xa, -float(t["entry_size_usdc"] or 0)))
        except Exception:
            continue
    now = datetime.utcnow()
    for o in opens:
        try:
            ea = datetime.fromisoformat(o["entry_at"]) if o["entry_at"] else None
            if ea:
                events.append((ea, +float(o["entry_size_usdc"] or 0)))
                events.append((now, -float(o["entry_size_usdc"] or 0)))
        except Exception:
            continue

    events.sort(key=lambda e: e[0])
    avg_capital_blocked = None
    if events and started_at_dt:
        total_secs = (now - started_at_dt).total_seconds()
        if total_secs > 0:
            cur_blocked = 0.0
            integral = 0.0
            prev_ts = started_at_dt
            for ts, delta in events:
                if ts < prev_ts:
                    ts = prev_ts
                dt = (ts - prev_ts).total_seconds()
                integral += cur_blocked * dt
                cur_blocked += delta
                prev_ts = ts
            integral += cur_blocked * max(0, (now - prev_ts).total_seconds())
            avg_capital_blocked = integral / total_secs

    # Tasa de rotación: (capital traded total / capital_bloqueado_promedio) por día
    total_traded = sum((t["entry_size_usdc"] or 0) for t in trades)
    days = (now - started_at_dt).total_seconds() / 86400 if started_at_dt else None
    rotation_per_day = None
    if avg_capital_blocked and days and avg_capital_blocked > 0 and days > 0:
        rotation_per_day = total_traded / avg_capital_blocked / days

    return {
        "by_status": by_status,
        "duration_distribution_h": dur_pct,
        "avg_capital_blocked_usdc": avg_capital_blocked,
        "total_capital_traded": total_traded,
        "rotation_per_day": rotation_per_day,
        "experiment_days": days,
    }


# ── 4. Robustez ─────────────────────────────────────────────────────

def robustez(trades: list[dict], started_at_dt: datetime | None) -> dict:
    if not trades:
        return {}
    pnls = [t["pnl_net"] or 0 for t in trades]
    n = len(pnls)
    sorted_desc = sorted(pnls, reverse=True)

    avg_full = sum(pnls) / n
    if n > 5:
        no_top5 = sum(sorted_desc[5:]) / (n - 5)
        no_bot5 = sum(sorted_desc[:-5]) / (n - 5)
    else:
        no_top5 = no_bot5 = None

    # Cuartiles temporales del experimento
    sorted_trades = sorted(trades, key=lambda t: t["exit_at"] or t["entry_at"])
    quartiles = {}
    if started_at_dt and sorted_trades:
        last_ts = max(
            datetime.fromisoformat(t["exit_at"] or t["entry_at"])
            for t in sorted_trades
        )
        total_secs = (last_ts - started_at_dt).total_seconds()
        if total_secs > 0:
            for q_idx, (q_start, q_end, q_label) in enumerate([
                (0.00, 0.25, "Q1"), (0.25, 0.50, "Q2"),
                (0.50, 0.75, "Q3"), (0.75, 1.00, "Q4"),
            ]):
                start_q = started_at_dt + timedelta(seconds=total_secs * q_start)
                end_q   = started_at_dt + timedelta(seconds=total_secs * q_end)
                bucket = [
                    t for t in sorted_trades
                    if start_q <= datetime.fromisoformat(t["exit_at"] or t["entry_at"]) < end_q
                ]
                if not bucket and q_idx == 3:
                    # Incluir el último timestamp en Q4
                    bucket = [
                        t for t in sorted_trades
                        if start_q <= datetime.fromisoformat(t["exit_at"] or t["entry_at"]) <= end_q
                    ]
                pnl_q = sum(t["pnl_net"] or 0 for t in bucket)
                quartiles[q_label] = {
                    "n": len(bucket),
                    "avg_pnl_net": (pnl_q / len(bucket)) if bucket else None,
                    "total_pnl_net": pnl_q,
                }

    return {
        "avg_full":     avg_full,
        "avg_no_top5":  no_top5,
        "avg_no_bot5":  no_bot5,
        "quartiles":    quartiles,
    }


# ── 5. Posiciones abiertas (mark-to-market) ─────────────────────────

def open_marked_to_market(opens: list[dict]) -> dict:
    if not opens:
        return {"n": 0, "mark_pnl": 0, "exposure": 0}
    mark = 0.0
    exposure = 0.0
    for o in opens:
        size  = float(o["entry_size_usdc"] or 0)
        ent   = float(o["entry_price"] or 0)
        last  = float(o["last_price"] or 0) if o["last_price"] else ent
        fees  = float(o["fees"] or 0)
        if ent > 0:
            shares = size / ent
            mark_pnl = shares * (last - ent) - fees
            mark += mark_pnl
        exposure += size
    return {"n": len(opens), "mark_pnl": mark, "exposure": exposure}


# ── Histograma textual ──────────────────────────────────────────────

def histogram(pnls: list[float]) -> str:
    if not pnls:
        return "(sin datos)"
    bins = [
        (-float("inf"), -100, "≤ -$100"),
        (-100, -50, "-$100..-$50"),
        (-50, -25, "-$50..-$25"),
        (-25, -10, "-$25..-$10"),
        (-10, 0,   "-$10..  $0"),
        (0,   10,  "  $0..+$10"),
        (10,  25,  "+$10..+$25"),
        (25,  50,  "+$25..+$50"),
        (50,  100, "+$50..+$100"),
        (100, 250, "+$100..+$250"),
        (250, float("inf"), "  ≥ +$250"),
    ]
    counts = []
    max_count = 0
    for lo, hi, label in bins:
        n = sum(1 for p in pnls if lo <= p < hi or (lo == -float("inf") and p < hi))
        counts.append((label, n))
        if n > max_count:
            max_count = n
    lines = []
    BAR_WIDTH = 40
    for label, n in counts:
        bar = "#" * (int(n * BAR_WIDTH / max_count) if max_count else 0)
        lines.append(f"  {label:>14s}  {n:>4d}  {bar}")
    return "\n".join(lines)


# ── Reporte ─────────────────────────────────────────────────────────

def build_report() -> str:
    conn = sqlite3.connect(DB_PATH)
    out = []
    P = out.append

    # Started_at del experimento (primer paper no-legacy)
    c = conn.cursor()
    c.execute("SELECT MIN(entry_at) FROM paper_trades WHERE agent_id != 'A_legacy'")
    started_at_iso = c.fetchone()[0]
    started_at_dt = datetime.fromisoformat(started_at_iso) if started_at_iso else None
    now = datetime.utcnow()
    duration_h = (now - started_at_dt).total_seconds() / 3600 if started_at_dt else 0

    P(f"# Reporte final del experimento multi-agente (Fase 2.6)")
    P("")
    P(f"- Generado: {now.isoformat()} UTC")
    P(f"- Inicio del experimento: {started_at_iso}")
    P(f"- Duración: {duration_h:.1f}h ({duration_h/24:.2f} días)")
    P("")

    # Snapshot por agente
    data_by_agent = {}
    for ag in AGENTS:
        closed = fetch_closed_trades(conn, ag)
        opens  = fetch_open_trades(conn, ag)
        data_by_agent[ag] = {"closed": closed, "open": opens}

    # ── 1. RENTABILIDAD ─────────────────────────────────────────────
    P("## 1. Métricas de rentabilidad")
    P("")
    P("| Agente | n cerrados | WR | PnL total net | PnL/trade avg | Mediana |")
    P("|---|---:|---:|---:|---:|---:|")
    for ag in AGENTS:
        r = rentabilidad(data_by_agent[ag]["closed"])
        if r["n"] == 0:
            P(f"| {ag} | 0 | — | — | — | — |")
            continue
        P(f"| {ag} | {r['n']} | {r['wr']*100:.1f}% | "
          f"{fmt_money(r['total'])} | {fmt_money(r['avg'])} | {fmt_money(r['median'])} |")
    P("")

    P("### Distribución por percentiles (PnL/trade net)")
    P("")
    P("| Agente | p5 | p10 | p25 | p50 | p75 | p90 | p95 |")
    P("|---|---:|---:|---:|---:|---:|---:|---:|")
    for ag in AGENTS:
        r = rentabilidad(data_by_agent[ag]["closed"])
        if r["n"] == 0:
            P(f"| {ag} | — | — | — | — | — | — | — |")
            continue
        P(f"| {ag} | {fmt_money(r['p5'])} | {fmt_money(r['p10'])} | "
          f"{fmt_money(r['p25'])} | {fmt_money(r['p50'])} | "
          f"{fmt_money(r['p75'])} | {fmt_money(r['p90'])} | {fmt_money(r['p95'])} |")
    P("")

    for ag in AGENTS:
        r = rentabilidad(data_by_agent[ag]["closed"])
        if r["n"] == 0:
            continue
        P(f"### Top 10 ganadores · Agente {ag}")
        P("")
        P("| # | PnL net | Status | Mercado |")
        P("|---:|---:|---|---|")
        for i, t in enumerate(r["top10"], 1):
            mkt = (t["market"] or "")[:60]
            P(f"| {i} | {fmt_money(t['pnl_net'])} | {t['status']} | {mkt} |")
        P("")
        P(f"### Top 10 perdedores · Agente {ag}")
        P("")
        P("| # | PnL net | Status | Mercado |")
        P("|---:|---:|---|---|")
        for i, t in enumerate(r["bot10"], 1):
            mkt = (t["market"] or "")[:60]
            P(f"| {i} | {fmt_money(t['pnl_net'])} | {t['status']} | {mkt} |")
        P("")
        P(f"**Concentración Agente {ag}**: el top {r['top10_count']} trades (≈10% de los ganadores) "
          f"aporta el {r['concentration_top10pct']:.1f}% del PnL bruto positivo total.")
        P("")

    # ── 2. RIESGO ───────────────────────────────────────────────────
    P("## 2. Métricas de riesgo y volatilidad")
    P("")
    P("| Agente | σ PnL/trade | Sharpe | Sortino | Max drawdown | Tiempo en DD | Racha L máx |")
    P("|---|---:|---:|---:|---:|---:|---:|")
    for ag in AGENTS:
        trades = data_by_agent[ag]["closed"]
        if not trades:
            P(f"| {ag} | — | — | — | — | — | — |")
            continue
        rsk = riesgo(trades)
        sharpe_s  = f"{rsk['sharpe']:.3f}" if rsk['sharpe']  is not None else "—"
        sortino_s = f"{rsk['sortino']:.3f}" if rsk['sortino'] is not None else "—"
        P(f"| {ag} | {fmt_money(rsk['sigma'])} | {sharpe_s} | {sortino_s} | "
          f"{fmt_money(rsk['max_drawdown'])} | {rsk['time_in_dd_pct']:.1f}% | "
          f"{rsk['max_loss_streak']} |")
    P("")
    P("Notas:")
    P("- σ es la desviación estándar muestral del PnL/trade neto.")
    P("- Sharpe = avg_pnl / σ (sin restar tasa libre de riesgo; medida puramente intra-experimento).")
    P("- Sortino = avg_pnl / σ_neg (σ calculado solo sobre trades con PnL<0).")
    P("- Max drawdown = peor caída desde el peak intra-experimento del PnL acumulado.")
    P("- Tiempo en DD = % de cierres con PnL acumulado por debajo del peak histórico.")
    P("")

    # ── 3. OPERATIVAS ───────────────────────────────────────────────
    P("## 3. Métricas operativas")
    P("")
    for ag in AGENTS:
        trades = data_by_agent[ag]["closed"]
        opens  = data_by_agent[ag]["open"]
        if not trades:
            continue
        op = operativas(trades, opens, started_at_dt)
        P(f"### Agente {ag}")
        P("")
        P("Tiempo medio por bucket (h):")
        P("")
        P("| status | n | avg_hours |")
        P("|---|---:|---:|")
        for s in ("tp_hit","sl_hit","resolved_win","resolved_loss","timeout","disputed"):
            v = op["by_status"].get(s)
            if v:
                ah = f"{v['avg_hours']:.1f}" if v['avg_hours'] is not None else "—"
                P(f"| {s} | {v['n']} | {ah} |")
        P("")
        if ag == "C" and op["duration_distribution_h"]:
            d = op["duration_distribution_h"]
            P("**Distribución duración (Agente C, hold-to-resolve)**:")
            P("")
            P("| min | p5 | p25 | p50 | p75 | p90 | p95 | max |")
            P("|---:|---:|---:|---:|---:|---:|---:|---:|")
            P(f"| {d.get('min',0):.1f}h | {d.get('p5',0):.1f}h | "
              f"{d.get('p25',0):.1f}h | {d.get('p50',0):.1f}h | "
              f"{d.get('p75',0):.1f}h | {d.get('p90',0):.1f}h | "
              f"{d.get('p95',0):.1f}h | {d.get('max',0):.1f}h |")
            P("")
        cap = op.get("avg_capital_blocked_usdc")
        rot = op.get("rotation_per_day")
        P(f"- Capital simultáneo bloqueado promedio: "
          f"{fmt_money(cap) if cap is not None else '—'}")
        P(f"- Capital total movido (suma de tamaños de entrada): "
          f"{fmt_money(op['total_capital_traded'])}")
        P(f"- Tasa de rotación de capital: "
          f"{rot:.2f} ×/día" if rot is not None else "- Tasa de rotación: —")
        P("")

    # ── 4. ROBUSTEZ ─────────────────────────────────────────────────
    P("## 4. Análisis de robustez")
    P("")
    P("### PnL/trade neto eliminando extremos")
    P("")
    P("| Agente | PnL/trade (full) | sin top 5 ganadores | sin top 5 perdedores |")
    P("|---|---:|---:|---:|")
    for ag in AGENTS:
        trades = data_by_agent[ag]["closed"]
        if not trades:
            P(f"| {ag} | — | — | — |")
            continue
        rb = robustez(trades, started_at_dt)
        P(f"| {ag} | {fmt_money(rb['avg_full'])} | "
          f"{fmt_money(rb['avg_no_top5']) if rb['avg_no_top5'] is not None else '—'} | "
          f"{fmt_money(rb['avg_no_bot5']) if rb['avg_no_bot5'] is not None else '—'} |")
    P("")

    P("### Cuartiles temporales del experimento (PnL/trade net por cuartil)")
    P("")
    P("| Agente | Q1 (n / avg) | Q2 (n / avg) | Q3 (n / avg) | Q4 (n / avg) |")
    P("|---|---|---|---|---|")
    for ag in AGENTS:
        trades = data_by_agent[ag]["closed"]
        if not trades:
            P(f"| {ag} | — | — | — | — |")
            continue
        rb = robustez(trades, started_at_dt)
        cells = [ag]
        for q in ("Q1","Q2","Q3","Q4"):
            qd = rb["quartiles"].get(q, {})
            if qd.get("n", 0) == 0:
                cells.append("0 / —")
            else:
                cells.append(f"{qd['n']} / {fmt_money(qd['avg_pnl_net'])}")
        P("| " + " | ".join(cells) + " |")
    P("")

    # ── 5. POSICIONES ABIERTAS ──────────────────────────────────────
    P("## 5. Posiciones abiertas al cierre del experimento (mark-to-market)")
    P("")
    P("| Agente | Abiertas | Exposure total | Mark-to-market PnL latente |")
    P("|---|---:|---:|---:|")
    for ag in AGENTS:
        opens = data_by_agent[ag]["open"]
        m = open_marked_to_market(opens)
        P(f"| {ag} | {m['n']} | {fmt_money(m['exposure'])} | {fmt_money(m['mark_pnl'])} |")
    P("")
    P("**Estimación PnL final si todas las abiertas resolvieran al precio actual** (cerradas + mark-to-market):")
    P("")
    P("| Agente | PnL net cerrado | + Mark-to-market | = Estimado total |")
    P("|---|---:|---:|---:|")
    for ag in AGENTS:
        trades = data_by_agent[ag]["closed"]
        opens  = data_by_agent[ag]["open"]
        cl_pnl = sum(t["pnl_net"] or 0 for t in trades)
        m = open_marked_to_market(opens)
        total = cl_pnl + (m["mark_pnl"] or 0)
        P(f"| {ag} | {fmt_money(cl_pnl)} | {fmt_money(m['mark_pnl'])} | {fmt_money(total)} |")
    P("")

    # ── BONUS: Histograma textual ──────────────────────────────────
    P("## Bonus: Histograma textual de PnL/trade neto por agente")
    P("")
    for ag in AGENTS:
        trades = data_by_agent[ag]["closed"]
        if not trades:
            continue
        pnls = [t["pnl_net"] or 0 for t in trades]
        P(f"### Agente {ag} (n = {len(pnls)})")
        P("")
        P("```")
        P(histogram(pnls))
        P("```")
        P("")

    # ── Anexo: cumplimiento de criterios ───────────────────────────
    P("## Anexo: cumplimiento de criterios de éxito")
    P("")
    P("| Criterio | Threshold | A | B | C | Estado |")
    P("|---|---:|---:|---:|---:|---|")
    crit_a = len(data_by_agent["A"]["closed"])
    crit_b = len(data_by_agent["B"]["closed"])
    crit_c = len(data_by_agent["C"]["closed"])
    crit_dur = duration_h / 24
    P(f"| Trades cerrados A | ≥150 | {crit_a} | — | — | "
      f"{'OK' if crit_a >= 150 else 'PARCIAL'} |")
    P(f"| Trades cerrados B | ≥150 | — | {crit_b} | — | "
      f"{'OK' if crit_b >= 150 else 'PARCIAL'} |")
    P(f"| Trades cerrados C | ≥50  | — | — | {crit_c} | "
      f"{'OK' if crit_c >= 50  else 'PARCIAL'} |")
    P(f"| Duración mínima | ≥3 días | — | — | — | "
      f"{crit_dur:.2f}d ({'OK' if crit_dur >= 3 else 'PARCIAL'}) |")
    P("")
    P("---")
    P("")
    P("Reporte sin recomendación de configuración. Decisión de Fase 3a a cargo del usuario.")
    P("")

    conn.close()
    return "\n".join(out)


# ── Main ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", "-o", default=None,
                        help="Path al archivo .md de salida (default: reports/experiment_final_<ts>.md)")
    parser.add_argument("--markdown-only", action="store_true",
                        help="Solo escribe archivo, no imprime a stdout")
    args = parser.parse_args()

    report = build_report()

    if args.output:
        path = Path(args.output)
    else:
        ts = datetime.utcnow().strftime("%Y%m%d-%H%M")
        path = REPORTS_DIR / f"experiment_final_{ts}.md"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report, encoding="utf-8")

    if not args.markdown_only:
        print(report)
    print(f"\n[INFO] Reporte guardado en: {path}", file=sys.stderr)


if __name__ == "__main__":
    main()
