"""
my_behavior_analyzer.py — Inferencia de estilo de trading del usuario
======================================================================
Analiza el histórico real on-chain (my_wallet.closed_positions) para:
  - Clasificar el estilo en MOMENTUM / HYBRID / HOLD según ratio de
    ventas anticipadas vs redeem.
  - Segmentar trades por nicho, banda de entry_price, trader copiado.
  - Detectar patrones (edges y agujeros).
  - Generar recomendaciones para Fase 3 (modo SUGGEST → FILTER).

Fuente de verdad única: `my_wallet_tracker.get_my_wallet_data().closed_positions`
con `close_type` clasificado en Pasada B de Fase 1.2.

NOTA: bot_trades reales (paper_trade=0 con resolución) se evaluarán para
inclusión cuando lleguemos a Fase 3 — hoy son ruido (1 trade paper).
La FUENTE de comportamiento real es la wallet on-chain.

Schema (REGLAS):
  - min_sample = 50 → confidence 'high'
  - Snapshot diario (loop async en main.py)
  - Histórico completo (is_milestone marca puntos clave)
"""

import json
import sqlite3
from datetime import datetime, timedelta
from collections import defaultdict
from statistics import mean

import database
import my_wallet_tracker

# ── Umbrales de configuración ────────────────────────────────────────
MIN_SAMPLE_HIGH_CONF   = 50    # confidence='high' si sample >= 50
MIN_SAMPLE_MEDIUM_CONF = 20    # confidence='medium' si 20-49
# Estilos
MOMENTUM_SELL_PCT      = 0.50  # > 50% ventas anticipadas → MOMENTUM
HOLD_SELL_PCT          = 0.30  # < 30% → HOLD; medio → HYBRID
# Recomendaciones (Fase 3)
REC_HIGH_SAMPLE        = 20    # confidence high en recomendación
REC_MEDIUM_SAMPLE      = 10
# Bandas de entry price
ENTRY_BANDS = [(0.0, 0.30), (0.30, 0.50), (0.50, 0.70), (0.70, 1.01)]
NICHE_KEYWORDS = {
    "esports":     ["lol:", "dota", "counter-strike", "valorant", "cs:go", "esports"],
    "tennis":      ["tennis", "atp", "wta", "open", "masters", "grand slam"],
    "soccer":      ["fc ", "soccer", "futbol", "fútbol", "win on 2"],  # "Will X win on YYYY"
    "basketball":  ["nba", "nuggets", "lakers", "knicks", "celtics", "bulls", "spread:", "o/u"],
    "baseball":    ["mlb", "yankees", "red sox", "dodgers"],
    "hockey":      ["nhl", "bruins", "leafs", "rangers"],
    "crypto":      ["bitcoin", "ethereum", "btc", "eth", "crypto", "$"],
    "politics":    ["election", "trump", "biden", "putin", "election", "deal", "ceasefire"],
    "geopolitics": ["hormuz", "ukraine", "russia", "iran", "war"],
}


# ─────────────────────────────────────────────────────────────────────
# 1. Carga de datos
# ─────────────────────────────────────────────────────────────────────

def _load_closed_trades() -> list:
    """
    Carga las posiciones cerradas reales del usuario desde my_wallet_tracker.
    Cada trade tiene close_type clasificado (Pasada B Fase 1.2).
    """
    data = my_wallet_tracker.get_my_wallet_data()
    closed = list(data.get("closed_positions") or [])

    # Recorrer también las que no caben en el truncado a 50 — recomputar
    # internamente sin slicing para tener todo el histórico.
    open_raw  = my_wallet_tracker._fetch_positions(status="OPEN")
    all_open  = [my_wallet_tracker._parse_position(p, is_open=True) for p in open_raw]
    real_open = []
    pending = []
    for p, raw in zip(all_open, open_raw):
        cur_price = float(raw.get("curPrice") or 0)
        pct       = float(raw.get("percentPnl") or 0)
        if cur_price < 0.01 and pct < -90:
            pending.append({
                "condition_id": p["condition_id"], "outcome": p["outcome"],
                "market": p["market"], "size": p["size"], "avg_price": p["avg_price"],
                "cash_pnl": p["cash_pnl"],
                "buy_usdc": p["size"] * p["avg_price"], "sell_usdc": 0.0,
                "redeem_usdc": 0.0, "sold_pct": 0.0,
                "close_type": "redeem_loss", "won": False,
                "opened_at": p["opened_at"], "resolved_at": "",
            })
        else:
            real_open.append(p)
    open_keys = {(p["condition_id"], (p["outcome"] or "").upper().strip()) for p in real_open}
    activity = my_wallet_tracker._fetch_activity(limit_total=2000)
    closed_full = my_wallet_tracker._aggregate_closed(activity, open_keys)
    seen = {(c["condition_id"], c["outcome"]) for c in closed_full}
    closed_full += [p for p in pending if (p["condition_id"], p["outcome"]) not in seen]
    return closed_full


def _detect_niche(market: str) -> str:
    """Heurística simple por keywords. Mejora pendiente Fase 1.5."""
    m = (market or "").lower()
    for niche, kws in NICHE_KEYWORDS.items():
        for kw in kws:
            if kw in m:
                return niche
    return "other"


def _entry_band(price: float) -> str:
    for lo, hi in ENTRY_BANDS:
        if lo <= price < hi:
            return f"{lo:.2f}-{hi:.2f}"
    return "0.70-1.01"


def _hold_hours(opened_at: str, resolved_at: str) -> float | None:
    """Horas entre apertura y cierre (None si fechas faltan)."""
    if not opened_at or not resolved_at:
        return None
    try:
        a = datetime.strptime(opened_at[:10], "%Y-%m-%d")
        b = datetime.strptime(resolved_at[:10], "%Y-%m-%d")
        return max(0.0, (b - a).total_seconds() / 3600)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────
# 2. Cómputo de buckets y estilo
# ─────────────────────────────────────────────────────────────────────

def _compute_buckets(trades: list) -> dict:
    buckets = {"redeem_win": [], "sell_profit": [], "sell_loss": [], "redeem_loss": []}
    for t in trades:
        ct = t.get("close_type", "redeem_loss")
        if ct in buckets:
            buckets[ct].append(t)
    return buckets


def _compute_style(buckets: dict) -> tuple[str, float]:
    n_sell = len(buckets["sell_profit"]) + len(buckets["sell_loss"])
    n_red  = len(buckets["redeem_win"])  + len(buckets["redeem_loss"])
    total  = n_sell + n_red
    if total == 0:
        return "HOLD", 0.0
    sell_pct = n_sell / total
    if sell_pct > MOMENTUM_SELL_PCT:
        style = "MOMENTUM"
    elif sell_pct < HOLD_SELL_PCT:
        style = "HOLD"
    else:
        style = "HYBRID"
    return style, sell_pct


def _confidence_for_sample(n: int) -> str:
    if n >= MIN_SAMPLE_HIGH_CONF:   return "high"
    if n >= MIN_SAMPLE_MEDIUM_CONF: return "medium"
    return "low"


# ─────────────────────────────────────────────────────────────────────
# 3. Segmentación por features
# ─────────────────────────────────────────────────────────────────────

def _segment_by(trades: list, key_fn) -> dict:
    """Agrupa trades por una clave; devuelve dict con stats por grupo."""
    groups = defaultdict(list)
    for t in trades:
        k = key_fn(t)
        if k is not None:
            groups[k].append(t)
    out = {}
    for k, items in groups.items():
        wins = sum(1 for t in items if t.get("won"))
        pnl  = sum(float(t.get("cash_pnl") or 0) for t in items)
        n    = len(items)
        out[k] = {
            "n":   n,
            "wr":  round(wins / n, 4) if n else 0,
            "pnl": round(pnl, 2),
            "avg_pnl": round(pnl / n, 2) if n else 0,
        }
    return out


def _segment_by_niche(trades: list) -> dict:
    return _segment_by(trades, lambda t: _detect_niche(t.get("market", "")))


def _segment_by_entry_band(trades: list) -> dict:
    return _segment_by(trades, lambda t: _entry_band(float(t.get("avg_price") or 0)))


def _avg_hold_hours(trades: list) -> float | None:
    hs = [_hold_hours(t.get("opened_at"), t.get("resolved_at")) for t in trades]
    hs = [h for h in hs if h is not None]
    return round(mean(hs), 1) if hs else None


# ─────────────────────────────────────────────────────────────────────
# 4. Patrones (texto humano)
# ─────────────────────────────────────────────────────────────────────

def _detect_patterns(buckets: dict, niche_seg: dict, entry_seg: dict,
                     style: str, sell_pct: float) -> list[str]:
    patterns = []

    # 1. Bucket dominante
    pnl_by_bucket = {k: round(sum(float(t["cash_pnl"]) for t in v), 2) for k, v in buckets.items()}
    biggest_profit = max(pnl_by_bucket.items(), key=lambda kv: kv[1])
    biggest_loss   = min(pnl_by_bucket.items(), key=lambda kv: kv[1])
    if biggest_profit[1] > 0:
        patterns.append(
            f"Mayor fuente de profit: {biggest_profit[0]} con +${biggest_profit[1]} "
            f"sobre {len(buckets[biggest_profit[0]])} trades"
        )
    if biggest_loss[1] < 0:
        patterns.append(
            f"Mayor agujero: {biggest_loss[0]} con ${biggest_loss[1]} "
            f"sobre {len(buckets[biggest_loss[0]])} trades"
        )

    # 2. Edge por sell_profit
    if buckets["sell_profit"]:
        avg_sell = mean(float(t["cash_pnl"]) for t in buckets["sell_profit"])
        avg_buy  = mean(float(t.get("buy_usdc") or 0) for t in buckets["sell_profit"])
        if avg_buy > 0:
            roi = avg_sell / avg_buy
            patterns.append(
                f"sell_profit promedia ${avg_sell:.2f} ({roi*100:.0f}% ROI sobre coste medio "
                f"${avg_buy:.2f}) — capturas momentum bien"
            )

    # 3. Nicho con mejor / peor expectancy (mínimo 5 trades para que sea informativo)
    rich_niches = {k: v for k, v in niche_seg.items() if v["n"] >= 5}
    if rich_niches:
        best = max(rich_niches.items(), key=lambda kv: kv[1]["pnl"])
        worst = min(rich_niches.items(), key=lambda kv: kv[1]["pnl"])
        if best[1]["pnl"] > 0:
            patterns.append(
                f"Mejor nicho: {best[0]} con +${best[1]['pnl']} en {best[1]['n']} trades "
                f"(WR {best[1]['wr']*100:.0f}%)"
            )
        if worst[1]["pnl"] < 0 and worst[0] != best[0]:
            patterns.append(
                f"Peor nicho: {worst[0]} con ${worst[1]['pnl']} en {worst[1]['n']} trades "
                f"(WR {worst[1]['wr']*100:.0f}%)"
            )

    # 4. Banda de entry con mejor/peor WR (mínimo 5 trades)
    rich_bands = {k: v for k, v in entry_seg.items() if v["n"] >= 5}
    if rich_bands:
        best_b = max(rich_bands.items(), key=lambda kv: kv[1]["wr"])
        worst_b = min(rich_bands.items(), key=lambda kv: kv[1]["wr"])
        if best_b[1]["wr"] > 0.50:
            patterns.append(
                f"Mejor banda de entrada: {best_b[0]} con WR {best_b[1]['wr']*100:.0f}% "
                f"({best_b[1]['n']} trades)"
            )
        if worst_b[1]["wr"] < 0.30 and worst_b[0] != best_b[0]:
            patterns.append(
                f"Peor banda de entrada: {worst_b[0]} con WR {worst_b[1]['wr']*100:.0f}% "
                f"({worst_b[1]['n']} trades)"
            )

    # 5. Estilo confirma o sorprende
    patterns.append(
        f"Estilo {style} ({sell_pct*100:.0f}% ventas anticipadas) — "
        + ("vendes momentum, no aguantas resolución" if style == "MOMENTUM"
           else "aguantas hasta resolución, vendes poco" if style == "HOLD"
           else "mezcla equilibrada de venta y aguante")
    )

    return patterns


# ─────────────────────────────────────────────────────────────────────
# 5. Recomendaciones para Fase 3
# ─────────────────────────────────────────────────────────────────────

def _confidence_rec(n: int) -> str:
    if n >= REC_HIGH_SAMPLE:   return "high"
    if n >= REC_MEDIUM_SAMPLE: return "medium"
    return "low"


def _build_recommendations(buckets: dict, niche_seg: dict, entry_seg: dict) -> list[dict]:
    """
    Cada recomendación tiene la estructura:
      {
        "rule_type":         "...",
        "params":            {...},
        "evidence":          {"sample": N, "wr": X, "pnl": Y},
        "confidence":        "high"|"medium"|"low",
        "auto_apply_at_phase": 3
      }
    Reglas con confidence='high' se aplicarán automáticamente al pasar a
    Fase 3 (modo SUGGEST → FILTER).
    """
    recs = []

    # 1. exclude_niche: nichos con WR<35% y pnl negativo
    for niche, stats in niche_seg.items():
        if stats["pnl"] < 0 and stats["wr"] < 0.35 and stats["n"] >= REC_MEDIUM_SAMPLE:
            recs.append({
                "rule_type": "exclude_niche",
                "params":    {"niche": niche},
                "evidence":  {"sample": stats["n"], "wr": stats["wr"], "pnl": stats["pnl"]},
                "confidence": _confidence_rec(stats["n"]),
                "auto_apply_at_phase": 3,
            })

    # 2. exclude_entry_above: bandas con WR<25%
    for band, stats in entry_seg.items():
        if stats["wr"] < 0.25 and stats["n"] >= REC_MEDIUM_SAMPLE:
            lo_str = band.split("-")[0]
            try:
                lo = float(lo_str)
                recs.append({
                    "rule_type": "exclude_entry_above",
                    "params":    {"price": lo},
                    "evidence":  {"sample": stats["n"], "wr": stats["wr"], "pnl": stats["pnl"]},
                    "confidence": _confidence_rec(stats["n"]),
                    "auto_apply_at_phase": 3,
                })
            except Exception:
                pass

    # 3. auto_tp_at: si sell_profit tiene >=10 trades con ROI medio >15%
    if len(buckets["sell_profit"]) >= REC_MEDIUM_SAMPLE:
        avg_sell = mean(float(t["cash_pnl"]) for t in buckets["sell_profit"])
        avg_buy  = mean(float(t.get("buy_usdc") or 0) for t in buckets["sell_profit"])
        if avg_buy > 0:
            roi = avg_sell / avg_buy
            if roi > 0.15:
                tp_multiplier = round(1 + roi, 2)
                recs.append({
                    "rule_type": "auto_tp_at",
                    "params":    {"multiplier": tp_multiplier},
                    "evidence":  {
                        "sample": len(buckets["sell_profit"]),
                        "wr":     1.0,
                        "pnl":    round(sum(float(t["cash_pnl"]) for t in buckets["sell_profit"]), 2),
                    },
                    "confidence": _confidence_rec(len(buckets["sell_profit"])),
                    "auto_apply_at_phase": 3,
                })

    # 4. avoid_hold_to_resolution: si redeem_loss domina sobre redeem_win
    n_rl = len(buckets["redeem_loss"])
    n_rw = len(buckets["redeem_win"])
    if n_rl + n_rw >= REC_MEDIUM_SAMPLE:
        ratio = n_rw / (n_rl + n_rw) if (n_rl + n_rw) else 0
        if ratio < 0.35:
            recs.append({
                "rule_type": "prefer_early_exit",
                "params":    {"redeem_win_ratio": round(ratio, 3)},
                "evidence":  {
                    "sample": n_rl + n_rw,
                    "wr":     round(ratio, 3),
                    "pnl":    round(
                        sum(float(t["cash_pnl"]) for t in buckets["redeem_win"]) +
                        sum(float(t["cash_pnl"]) for t in buckets["redeem_loss"]), 2
                    ),
                },
                "confidence": _confidence_rec(n_rl + n_rw),
                "auto_apply_at_phase": 3,
            })

    return recs


# ─────────────────────────────────────────────────────────────────────
# 6. Función pública: analyze_my_behavior
# ─────────────────────────────────────────────────────────────────────

def analyze_my_behavior(min_sample: int = MIN_SAMPLE_HIGH_CONF) -> dict:
    """
    Analiza la wallet del usuario y devuelve un snapshot completo.
    No persiste — usar `save_snapshot()` aparte para guardar en DB.
    """
    trades = _load_closed_trades()
    n = len(trades)
    confidence = _confidence_for_sample(n)

    buckets = _compute_buckets(trades)
    style, sell_pct = _compute_style(buckets)
    avg_hold = _avg_hold_hours(trades)

    niche_seg = _segment_by_niche(trades)
    entry_seg = _segment_by_entry_band(trades)

    patterns = _detect_patterns(buckets, niche_seg, entry_seg, style, sell_pct)
    recs     = _build_recommendations(buckets, niche_seg, entry_seg)

    # Edges resumidos
    rich_niches = {k: v for k, v in niche_seg.items() if v["n"] >= 5}
    rich_bands  = {k: v for k, v in entry_seg.items()  if v["n"] >= 5}
    edges = {
        "best_niche":   _best_by(rich_niches, "pnl", reverse=True),
        "worst_niche":  _best_by(rich_niches, "pnl", reverse=False),
        "best_entry":   _best_by(rich_bands, "wr", reverse=True),
        "worst_entry":  _best_by(rich_bands, "wr", reverse=False),
        "niche_segments": niche_seg,
        "entry_segments": entry_seg,
    }

    summary = _build_summary_text(buckets, style, sell_pct, n)

    # Métricas de paper trading (Fase 2.3) — sub-bloque informativo,
    # NO contamina la clasificación de comportamiento real on-chain.
    try:
        import paper_executor
        paper_metrics = paper_executor.get_paper_metrics_for_snapshot()
    except Exception as e:
        print(f"[BEHAVIOR] paper_metrics error: {e}")
        paper_metrics = {}

    return {
        "snapshot_at":      datetime.now().isoformat(),
        "sample_size":      n,
        "style":            style,
        "style_confidence": confidence,
        "sell_pct":         round(sell_pct, 4),
        "avg_hold_hours":   avg_hold,
        "summary":          summary,
        "paper_metrics":    paper_metrics,
        "buckets": {
            ct: {
                "n":   len(items),
                "pnl": round(sum(float(t["cash_pnl"]) for t in items), 2),
                "avg": round(mean(float(t["cash_pnl"]) for t in items), 2) if items else 0,
            } for ct, items in buckets.items()
        },
        "edges":          edges,
        "patterns":       patterns,
        "recommendations_for_phase3": recs,
    }


def _best_by(seg: dict, field: str, reverse: bool) -> dict | None:
    if not seg:
        return None
    items = sorted(seg.items(), key=lambda kv: kv[1].get(field, 0), reverse=reverse)
    name, stats = items[0]
    return {"name": name, **stats}


def _build_summary_text(buckets: dict, style: str, sell_pct: float, n: int) -> str:
    pnl = {k: sum(float(t["cash_pnl"]) for t in v) for k, v in buckets.items()}
    profit_buckets = {k: v for k, v in pnl.items() if v > 0}
    loss_buckets   = {k: v for k, v in pnl.items() if v < 0}
    profit_str = ", ".join(f"{k} +${v:.0f}" for k, v in
                           sorted(profit_buckets.items(), key=lambda kv: -kv[1]))
    loss_str   = ", ".join(f"{k} ${v:.0f}" for k, v in
                           sorted(loss_buckets.items(), key=lambda kv: kv[1]))
    return (f"{style} ({sell_pct*100:.0f}% ventas) sobre {n} trades. "
            f"Profit: {profit_str or '—'}. "
            f"Loss: {loss_str or '—'}.")


# ─────────────────────────────────────────────────────────────────────
# 7. Persistencia
# ─────────────────────────────────────────────────────────────────────

def save_snapshot(snap: dict, is_milestone: bool = False,
                  milestone_label: str | None = None) -> int:
    """Guarda un snapshot en user_profile_snapshots. Devuelve el id."""
    conn = database.get_connection()
    c = conn.cursor()
    b = snap["buckets"]
    pm = snap.get("paper_metrics") or {}
    c.execute("""
        INSERT INTO user_profile_snapshots (
            snapshot_at, sample_size, is_milestone, milestone_label,
            style, style_confidence, sell_pct, avg_hold_hours,
            n_redeem_win, pnl_redeem_win,
            n_sell_profit, pnl_sell_profit,
            n_sell_loss, pnl_sell_loss,
            n_redeem_loss, pnl_redeem_loss,
            edges_json, patterns_json, recommendations_json,
            paper_n_trades, paper_n_closed, paper_wr, paper_pnl_net,
            paper_tp_hit_pct, paper_sl_hit_pct
        ) VALUES (?,?,?,?, ?,?,?,?, ?,?, ?,?, ?,?, ?,?, ?,?,?, ?,?,?,?, ?,?)
    """, (
        snap["snapshot_at"], snap["sample_size"],
        1 if is_milestone else 0, milestone_label,
        snap["style"], snap["style_confidence"], snap["sell_pct"], snap.get("avg_hold_hours"),
        b["redeem_win"]["n"], b["redeem_win"]["pnl"],
        b["sell_profit"]["n"], b["sell_profit"]["pnl"],
        b["sell_loss"]["n"], b["sell_loss"]["pnl"],
        b["redeem_loss"]["n"], b["redeem_loss"]["pnl"],
        json.dumps(snap["edges"], ensure_ascii=False),
        json.dumps(snap["patterns"], ensure_ascii=False),
        json.dumps(snap["recommendations_for_phase3"], ensure_ascii=False),
        pm.get("n_trades"), pm.get("n_closed"), pm.get("wr"), pm.get("pnl_net"),
        pm.get("tp_hit_pct"), pm.get("sl_hit_pct"),
    ))
    snap_id = c.lastrowid
    conn.commit()
    conn.close()
    return snap_id


def get_latest_snapshot() -> dict | None:
    """Devuelve el último snapshot persistido en formato dict listo para JSON."""
    conn = database.get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT id, snapshot_at, sample_size, is_milestone,
               style, style_confidence, sell_pct, avg_hold_hours,
               n_redeem_win, pnl_redeem_win,
               n_sell_profit, pnl_sell_profit,
               n_sell_loss, pnl_sell_loss,
               n_redeem_loss, pnl_redeem_loss,
               edges_json, patterns_json, recommendations_json,
               paper_n_trades, paper_n_closed, paper_wr, paper_pnl_net,
               paper_tp_hit_pct, paper_sl_hit_pct, milestone_label
        FROM user_profile_snapshots
        ORDER BY snapshot_at DESC LIMIT 1
    """)
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id":               row[0],
        "snapshot_at":      row[1],
        "sample_size":      row[2],
        "is_milestone":     bool(row[3]),
        "style":            row[4],
        "style_confidence": row[5],
        "sell_pct":         row[6],
        "avg_hold_hours":   row[7],
        "buckets": {
            "redeem_win":  {"n": row[8],  "pnl": row[9]},
            "sell_profit": {"n": row[10], "pnl": row[11]},
            "sell_loss":   {"n": row[12], "pnl": row[13]},
            "redeem_loss": {"n": row[14], "pnl": row[15]},
        },
        "edges":           json.loads(row[16] or "{}"),
        "patterns":        json.loads(row[17] or "[]"),
        "recommendations_for_phase3": json.loads(row[18] or "[]"),
        "paper_metrics": {
            "n_trades":     row[19],
            "n_closed":     row[20],
            "wr":           row[21],
            "pnl_net":      row[22],
            "tp_hit_pct":   row[23],
            "sl_hit_pct":   row[24],
        },
        "milestone_label": row[25],
    }


# ─────────────────────────────────────────────────────────────────────
# 8. Loop diario (consumido por main.py)
# ─────────────────────────────────────────────────────────────────────

async def behavior_snapshot_loop():
    """
    Genera un snapshot diario (24h entre runs). Ejecuta inmediatamente
    al arrancar y luego cada día.
    """
    import asyncio
    while True:
        try:
            snap = analyze_my_behavior()
            sid  = save_snapshot(snap)
            print(f"[BEHAVIOR] 📊 Snapshot #{sid} → {snap['style']} "
                  f"({snap['sample_size']} trades, conf={snap['style_confidence']})")
        except Exception as e:
            print(f"[BEHAVIOR] Error en snapshot loop: {e}")
        await asyncio.sleep(24 * 3600)  # diario


# ─────────────────────────────────────────────────────────────────────
# Test standalone
# ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    snap = analyze_my_behavior()
    print(json.dumps(snap, indent=2, ensure_ascii=False, default=str))
    sid = save_snapshot(snap)
    print(f"\nGuardado en DB id={sid}")
