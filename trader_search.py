"""
trader_search.py — Búsqueda sistemática de traders con WR ≥ 80% (sin sesgo)
============================================================================
Uso: python3 trader_search.py

Fases:
  1. Pull del leaderboard (all-time PNL) por categorías: overall, sports, crypto, politics
  2. Pre-filtros baratos (volumen, actividad reciente)
  3. Análisis unbiased de posiciones (500 DESC + 500 ASC, dedup)
  4. Filtro elite (WR ≥ 80%, ≥ 50 cerradas, PnL > 0, PF ≥ 1.5)
  5. Reporte en consola y fichero reports/trader_search_YYYYMMDD_HHMMSS.txt
"""

import os
import json
import time
import requests
from datetime import datetime, timezone, timedelta

from leaderboard_analyzer import _fetch_positions_unbiased, _is_closed, _parse_dt

# ── Config ────────────────────────────────────────────────────────────────────

DATA_URL  = "https://data-api.polymarket.com"

# Leaderboard: cuántos traders pull por categoría
LEADERBOARD_PER_CATEGORY = 300

# Pre-filtros
MIN_VOLUME_USDC      = 10_000   # vol mínimo (relativo — filtra cuentas toy)
MIN_RECENT_ACTIVITY  = 14       # días de actividad reciente mínima

# Filtros elite
ELITE_MIN_WR         = 0.80
ELITE_MIN_CLOSED     = 25
ELITE_MIN_PNL        = 0
ELITE_MIN_PF         = 1.5
ELITE_MIN_POSITIONS  = 50

# Filtros strong (backup)
STRONG_MIN_WR        = 0.70
STRONG_MIN_CLOSED    = 25
STRONG_MIN_PNL       = 0
STRONG_MIN_PF        = 1.3
STRONG_MIN_POSITIONS = 50

REQUEST_DELAY        = 0.3      # segundos entre llamadas de posiciones
CHECKPOINT_FILE      = "/tmp/trader_search_checkpoint.json"

CATEGORIES = ["overall", "sports", "crypto", "politics"]


# ── Fase 1: Leaderboard ───────────────────────────────────────────────────────

def fetch_leaderboard_category(category: str, limit: int = 300) -> list:
    """Devuelve hasta `limit` traders del leaderboard (all-time PNL)."""
    traders = []
    offset  = 0
    page    = 50

    while len(traders) < limit:
        try:
            r = requests.get(
                DATA_URL + "/v1/leaderboard"
                + f"?timePeriod=all&orderBy=PNL&limit={page}&offset={offset}&category={category}",
                timeout=15,
            )
            if r.status_code != 200:
                break
            data = r.json()
            if not isinstance(data, list) or not data:
                break
            traders.extend(data)
            offset += page
            if len(data) < page:
                break
        except Exception as e:
            print(f"  [leaderboard] error cat={category} offset={offset}: {e}")
            break

    return traders[:limit]


def pull_all_candidates() -> list:
    """Pull y deduplica traders de todas las categorías."""
    seen    = {}   # wallet → trader dict

    for cat in CATEGORIES:
        print(f"  Categoria '{cat}'...", end="", flush=True)
        raw = fetch_leaderboard_category(cat, LEADERBOARD_PER_CATEGORY)
        new = 0
        for t in raw:
            wallet = t.get("proxyWallet", "")
            if not wallet:
                continue
            if wallet not in seen:
                seen[wallet] = {
                    "wallet":   wallet,
                    "name":     t.get("userName", wallet[:12]),
                    "volume":   float(t.get("vol", 0) or 0),
                    "pnl_lb":   float(t.get("pnl", 0) or 0),
                    "category": cat,
                }
                new += 1
        print(f" {len(raw)} brutos, {new} nuevos (total único: {len(seen)})")

    return list(seen.values())


# ── Fase 2: Pre-filtros baratos ───────────────────────────────────────────────

def has_recent_activity(wallet: str, days: int = MIN_RECENT_ACTIVITY) -> bool:
    """True si el trader tiene al menos 1 trade en los últimos `days` días."""
    try:
        r = requests.get(
            DATA_URL + f"/activity?user={wallet}&limit=5",
            timeout=8,
        )
        if r.status_code != 200:
            return True   # duda favorable
        data = r.json()
        if not isinstance(data, list) or not data:
            return False
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        for item in data:
            ts = item.get("timestamp")
            if not ts:
                continue
            try:
                dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
                if dt >= cutoff:
                    return True
            except Exception:
                pass
        return False
    except Exception:
        return True   # duda favorable si falla la red


def apply_prefilters(candidates: list) -> list:
    passed = []
    for t in candidates:
        if t["volume"] < MIN_VOLUME_USDC:
            continue
        passed.append(t)
    return passed


# ── Fase 3: Análisis unbiased ─────────────────────────────────────────────────

def calculate_stats(positions: list) -> dict:
    now    = datetime.now(timezone.utc)
    closed = []
    for p in positions:
        size = float(p.get("initialValue", p.get("size", 0)) or 0)
        if size <= 0:
            continue
        if _is_closed(p, now):
            closed.append(p)

    if not closed:
        return {
            "total_positions": len(positions), "closed": 0,
            "wins": 0, "losses": 0, "win_rate": 0,
            "pnl": 0, "profit_factor": 0, "avg_win": 0, "avg_loss": 0,
        }

    wins         = [p for p in closed if float(p.get("cashPnl", 0)) > 0]
    losses       = [p for p in closed if float(p.get("cashPnl", 0)) < 0]
    total_win_pnl  = sum(float(p.get("cashPnl", 0)) for p in wins)
    total_loss_pnl = abs(sum(float(p.get("cashPnl", 0)) for p in losses))
    avg_win  = total_win_pnl  / len(wins)   if wins   else 0
    avg_loss = total_loss_pnl / len(losses) if losses else 0
    pf       = avg_win / avg_loss           if avg_loss > 0 else 0

    return {
        "total_positions": len(positions),
        "closed":          len(closed),
        "wins":            len(wins),
        "losses":          len(losses),
        "win_rate":        len(wins) / len(closed),
        "pnl":             total_win_pnl - total_loss_pnl,
        "profit_factor":   round(pf, 3),
        "avg_win":         round(avg_win, 2),
        "avg_loss":        round(avg_loss, 2),
    }


def analyze_candidates(candidates: list) -> list:
    results    = []
    checkpoint = _load_checkpoint()
    total      = len(candidates)

    for i, trader in enumerate(candidates, 1):
        wallet = trader["wallet"]
        name   = trader["name"]

        # Checkpoint — no repetir traders ya analizados
        if wallet in checkpoint:
            results.append(checkpoint[wallet])
            print(f"  [{i:3d}/{total}] {name[:25]:<25} [CACHED]")
            continue

        print(f"  [{i:3d}/{total}] {name[:25]:<25}", end="", flush=True)

        # Actividad reciente (solo si no está en checkpoint)
        if not has_recent_activity(wallet):
            print(f" → sin actividad reciente, skip")
            continue

        positions = _fetch_positions_unbiased(wallet)
        stats     = calculate_stats(positions)

        wr_pct  = round(stats["win_rate"] * 100, 1)
        pnl_k   = round(stats["pnl"] / 1000, 1)
        print(f" → {len(positions):4d} pos | {stats['closed']:3d} cerradas | WR {wr_pct:5.1f}% | PnL {pnl_k:+.1f}k")

        record = {**trader, "stats": stats}
        results.append(record)
        checkpoint[wallet] = record
        _save_checkpoint(checkpoint)

        time.sleep(REQUEST_DELAY)

    return results


# ── Checkpoint ────────────────────────────────────────────────────────────────

def _load_checkpoint() -> dict:
    try:
        with open(CHECKPOINT_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_checkpoint(data: dict):
    try:
        with open(CHECKPOINT_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


# ── Fase 4: Filtros ───────────────────────────────────────────────────────────

def filter_elite(results: list) -> list:
    out = []
    for r in results:
        s = r.get("stats", {})
        if (s.get("win_rate", 0)       >= ELITE_MIN_WR
                and s.get("closed", 0)         >= ELITE_MIN_CLOSED
                and s.get("pnl", 0)            >= ELITE_MIN_PNL
                and s.get("profit_factor", 0)  >= ELITE_MIN_PF
                and s.get("total_positions", 0)>= ELITE_MIN_POSITIONS):
            out.append(r)
    return sorted(out, key=lambda x: x["stats"]["win_rate"], reverse=True)


def filter_strong(results: list) -> list:
    out = []
    for r in results:
        s = r.get("stats", {})
        if (STRONG_MIN_WR <= s.get("win_rate", 0) < ELITE_MIN_WR
                and s.get("closed", 0)         >= STRONG_MIN_CLOSED
                and s.get("pnl", 0)            >= STRONG_MIN_PNL
                and s.get("profit_factor", 0)  >= STRONG_MIN_PF
                and s.get("total_positions", 0)>= STRONG_MIN_POSITIONS):
            out.append(r)
    return sorted(out, key=lambda x: x["stats"]["win_rate"], reverse=True)


# ── Fase 5: Reporte ───────────────────────────────────────────────────────────

def generate_report(all_results: list, elite: list, strong: list) -> str:
    os.makedirs("reports", exist_ok=True)
    path = f"reports/trader_search_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"

    lines = [
        "=" * 80,
        "BÚSQUEDA SISTEMÁTICA DE TRADERS — ANÁLISIS SIN SESGO",
        f"Generado: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        "=" * 80,
        "",
        f"Traders analizados:          {len(all_results)}",
        f"Elite (WR≥80%, ≥25 cerradas, PnL>0, PF≥1.5):  {len(elite)}",
        f"Strong (WR 70-80%, ≥25 cerradas, PnL>0, PF≥1.3): {len(strong)}",
        "",
    ]

    def trader_block(t, rank):
        s  = t["stats"]
        return [
            f"{rank}. {t['name']}",
            f"   Wallet:         {t['wallet']}",
            f"   Win Rate:       {s['win_rate']*100:.1f}%  ({s['wins']}W / {s['losses']}L / {s['closed']} cerradas)",
            f"   Net PnL:        {s['pnl']:+,.0f} USDC",
            f"   Profit Factor:  {s['profit_factor']:.2f}",
            f"   Avg win:        +{s['avg_win']:.2f} USDC",
            f"   Avg loss:       -{s['avg_loss']:.2f} USDC",
            f"   Posiciones API: {s['total_positions']}",
            f"   Volumen LB:     {t['volume']:,.0f} USDC",
            f"   Categoría LB:   {t['category']}",
            "",
        ]

    if elite:
        lines += ["=" * 80, "ELITE (WR ≥ 80%)", "=" * 80, ""]
        for i, t in enumerate(elite, 1):
            lines += trader_block(t, i)
    else:
        lines += ["SIN TRADERS ELITE (WR ≥ 80% con ≥ 25 cerradas y PnL > 0)", ""]

    if strong:
        lines += ["=" * 80, "STRONG (WR 70–80%)", "=" * 80, ""]
        for i, t in enumerate(strong, 1):
            lines += trader_block(t, i)
    else:
        lines += ["SIN TRADERS STRONG (WR 70-80%)", ""]

    # Distribución WR
    buckets = {"90-100%": [], "80-90%": [], "70-80%": [], "60-70%": [],
               "50-60%": [], "40-50%": [], "30-40%": [], "<30%": []}
    for r in all_results:
        s  = r.get("stats", {})
        wr = s.get("win_rate", 0)
        cl = s.get("closed", 0)
        if cl < 5:
            continue   # muestra insuficiente para distribución
        if   wr >= 0.90: buckets["90-100%"].append(r)
        elif wr >= 0.80: buckets["80-90%"].append(r)
        elif wr >= 0.70: buckets["70-80%"].append(r)
        elif wr >= 0.60: buckets["60-70%"].append(r)
        elif wr >= 0.50: buckets["50-60%"].append(r)
        elif wr >= 0.40: buckets["40-50%"].append(r)
        elif wr >= 0.30: buckets["30-40%"].append(r)
        else:            buckets["<30%"].append(r)

    lines += ["=" * 80, "DISTRIBUCIÓN WIN RATE (traders con ≥ 5 cerradas)", "=" * 80, ""]
    for bucket, members in buckets.items():
        bar = "█" * len(members)
        names = ", ".join(m["name"][:15] for m in members[:5])
        extra = f" +{len(members)-5}más" if len(members) > 5 else ""
        lines.append(f"  {bucket:10s} {len(members):3d}  {bar[:40]}")
        if members:
            lines.append(f"             {names}{extra}")
    lines.append("")

    # Conclusión
    lines += ["=" * 80, "CONCLUSIÓN", "=" * 80, ""]
    if len(elite) >= 5:
        lines += [
            f"✓ ÉXITO: {len(elite)} traders elite encontrados.",
            "  El modelo de copy-trading es viable con estos traders.",
            "  Siguiente paso: añadir a TRUSTED_TRADERS y activar P&L tracking.",
        ]
    elif elite:
        lines += [
            f"⚠ MARGINAL: Solo {len(elite)} traders elite.",
            f"  Con {len(strong)} strong (70-80%) puede ser viable con cautela.",
            "  Siguiente paso: evaluar si la muestra es suficiente.",
        ]
    else:
        lines += [
            "✗ SIN TRADERS ELITE con los criterios actuales.",
            f"  Traders strong (70-80%): {len(strong)}",
            "  Opciones: (A) bajar umbral a 70%, (B) ampliar leaderboard,",
            "  (C) reconsidering el modelo de copy-trading.",
        ]

    text = "\n".join(lines)
    with open(path, "w") as f:
        f.write(text)
    return path, text


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 80)
    print("BÚSQUEDA SISTEMÁTICA DE TRADERS — POLYMARKET (SIN SESGO)")
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 80)

    print("\nFase 1: Pull leaderboard por categorías...")
    candidates = pull_all_candidates()
    print(f"→ {len(candidates)} traders únicos antes de pre-filtros")

    print(f"\nFase 2: Pre-filtros (volumen ≥ {MIN_VOLUME_USDC:,} USDC)...")
    filtered = apply_prefilters(candidates)
    print(f"→ {len(filtered)} candidatos pasan pre-filtros")

    print(f"\nFase 3: Análisis unbiased ({len(filtered)} traders)...")
    print("  (checkpoint guardado en /tmp — reiniciable sin perder progreso)")
    results = analyze_candidates(filtered)
    results_with_stats = [r for r in results if "stats" in r]
    print(f"\n→ {len(results_with_stats)} traders analizados con éxito")

    print("\nFase 4: Filtros elite/strong...")
    elite  = filter_elite(results_with_stats)
    strong = filter_strong(results_with_stats)
    print(f"  Elite  (WR≥80%, ≥25 cerradas, PnL>0, PF≥1.5): {len(elite)}")
    print(f"  Strong (WR≥70%, ≥25 cerradas, PnL>0, PF≥1.3): {len(strong)}")

    print("\nFase 5: Generando reporte...")
    path, text = generate_report(results_with_stats, elite, strong)
    print(text)
    print(f"\nReporte guardado: {path}")

    return elite, strong, results_with_stats


if __name__ == "__main__":
    main()
