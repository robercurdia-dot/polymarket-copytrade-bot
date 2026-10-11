"""
manual_trade_tracker.py — Sistema de aprendizaje de trades manuales
====================================================================
Aprende de los trades que haces manualmente en Polymarket para
identificar tus patrones ganadores y eventualmente automatizarlos.

Comandos Telegram:
  /logtrade <market_id> <YES|NO> <price> <size_usdc> [trader] ["reason"] [days_left]
  /trades                        → trades abiertos
  /traderesult <id> <won|lost>   → registrar resultado manualmente
  /tradepatterns                 → análisis de patrones (requiere 10+ resueltos)

Ejemplo:
  /logtrade 0x3bf6b... YES 0.65 100 sovereign2013 "sigue tendencia" 8
"""

from __future__ import annotations

import re
import shlex
import requests
import json
from datetime import datetime, timezone
from typing import Optional

import database
import config

MIN_RESOLVED_FOR_PATTERNS = 10   # mínimo de trades resueltos para mostrar patrones

GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API  = "https://data-api.polymarket.com"


# ── Utilidades ────────────────────────────────────────────────────────────────

def _fetch_market_info(market_id: str) -> dict:
    """
    Intenta obtener la pregunta del mercado y la fecha de resolución.
    Primero por conditionId, luego por slug.
    Devuelve {"question": str, "end_date": str} o vacío si no encuentra.
    """
    for param in [f"conditionId={market_id}", f"slug={market_id}"]:
        try:
            r = requests.get(f"{GAMMA_API}/markets?{param}", timeout=8)
            if r.status_code != 200:
                continue
            data = r.json()
            mkt = data[0] if isinstance(data, list) and data else None
            if mkt:
                return {
                    "question": mkt.get("question", ""),
                    "end_date": mkt.get("endDate", ""),
                    "closed":   mkt.get("closed", False),
                    "outcome_prices": mkt.get("outcomePrices"),
                    "outcomes":       mkt.get("outcomes"),
                }
        except Exception:
            pass
    return {}


def _detect_niche(question: str) -> str:
    """Detecta el nicho del mercado a partir de la pregunta."""
    try:
        from leaderboard_analyzer import NICHE_KEYWORDS
        import re as _re
        q = question.lower()
        for niche, keywords in NICHE_KEYWORDS.items():
            for kw in keywords:
                kw_l = kw.lower()
                if _re.match(r'^\w+$', kw_l):
                    if _re.search(r'\b' + kw_l + r'\b', q):
                        return niche
                elif kw_l in q:
                    return niche
    except Exception:
        pass
    return "other"


def _winning_side_from_market(market_info: dict) -> Optional[str]:
    """
    Si el mercado está cerrado, devuelve el lado ganador ('YES'/'NO').
    """
    if not market_info.get("closed"):
        return None
    raw_prices   = market_info.get("outcome_prices")
    outcomes_raw = market_info.get("outcomes")
    if not raw_prices or not outcomes_raw:
        return None

    if isinstance(raw_prices, str):
        try:
            raw_prices = json.loads(raw_prices.replace("'", '"'))
        except Exception:
            return None
    if isinstance(outcomes_raw, str):
        try:
            outcomes_raw = json.loads(outcomes_raw.replace("'", '"'))
        except Exception:
            return None

    prices   = [float(p) for p in raw_prices]
    outcomes = list(outcomes_raw)
    winner_idx = next((i for i, p in enumerate(prices) if p >= 0.99), None)
    if winner_idx is None:
        return None
    return outcomes[winner_idx].upper() if winner_idx < len(outcomes) else "YES"


def _compute_days_left(end_date: str) -> int:
    if not end_date:
        return 999
    try:
        dt = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            from datetime import timezone as _tz
            dt = dt.replace(tzinfo=_tz.utc)
        diff = dt - datetime.now(timezone.utc)
        return max(0, int(diff.total_seconds() // 86400))
    except Exception:
        return 999


# ── Parseo del comando /logtrade ──────────────────────────────────────────────

def parse_logtrade_command(text: str) -> dict | None:
    """
    Parsea el comando /logtrade y devuelve un dict con los campos,
    o None con un mensaje de error si el formato es incorrecto.

    Formato:
      /logtrade <market_id> <YES|NO> <price> <size_usdc>
                [trader] ["reason with spaces"] [days_left]

    Ejemplo:
      /logtrade 0x3bf6b YES 0.65 100 sovereign2013 "copying elite" 8
    """
    # Quitar el comando en sí
    text = text.strip()
    if text.lower().startswith("/logtrade"):
        text = text[len("/logtrade"):].strip()

    try:
        parts = shlex.split(text)
    except ValueError as e:
        return {"error": f"Error parseando comillas: {e}"}

    if len(parts) < 4:
        return {
            "error": (
                "Formato: /logtrade <market_id> <YES|NO> <price> <size_usdc> "
                "[trader] [\"razón\"] [días_restantes]\n"
                "Ejemplo: /logtrade 0x3bf6b YES 0.65 100 sovereign2013 \"sigue tendencia\" 8"
            )
        }

    market_id = parts[0]
    side      = parts[1].upper()
    if side not in ("YES", "NO"):
        return {"error": f"Lado inválido '{parts[1]}'. Usa YES o NO."}

    try:
        price = float(parts[2])
        if not 0 < price < 1:
            return {"error": f"Precio '{parts[2]}' fuera de rango (0–1)."}
    except ValueError:
        return {"error": f"Precio inválido: '{parts[2]}'."}

    try:
        size = float(parts[3])
        if size <= 0:
            return {"error": "Tamaño debe ser > 0 USDC."}
    except ValueError:
        return {"error": f"Tamaño inválido: '{parts[3]}'."}

    trader    = parts[4] if len(parts) > 4 else "manual"
    reason    = parts[5] if len(parts) > 5 else ""
    days_left = 999
    if len(parts) > 6:
        try:
            days_left = int(parts[6])
        except ValueError:
            pass  # ignorar si no es entero

    return {
        "market_id": market_id,
        "side":      side,
        "price":     price,
        "size":      size,
        "trader":    trader,
        "reason":    reason,
        "days_left": days_left,
    }


# ── Registro de trade ─────────────────────────────────────────────────────────

def log_trade(market_id: str, side: str, price: float, size: float,
              trader: str = "manual", reason: str = "",
              days_left: int = 999) -> dict:
    """
    Registra un trade manual. Intenta obtener la pregunta del mercado vía API.
    Devuelve dict con id, question, niche y mensaje confirmación.
    """
    info = _fetch_market_info(market_id)
    question = info.get("question", "")
    niche    = _detect_niche(question) if question else "other"

    # Si no se pasó days_left, calcular desde end_date
    if days_left == 999 and info.get("end_date"):
        days_left = _compute_days_left(info["end_date"])

    trade_id = database.save_manual_trade(
        market_id      = market_id,
        side           = side,
        price          = price,
        size           = size,
        copied_from    = trader,
        reason         = reason,
        days_left      = days_left,
        niche          = niche,
        market_question= question,
    )

    retorno = round((1 / price) - 1, 2) if price > 0 else 0
    return {
        "id":       trade_id,
        "question": question or market_id[:20],
        "niche":    niche,
        "retorno":  retorno,
        "days_left": days_left,
    }


# ── Resolución automática ─────────────────────────────────────────────────────

def resolve_open_trades():
    """
    Consulta Gamma API para cada trade manual abierto.
    Si el mercado se resolvió, actualiza el resultado automáticamente.
    Llamar periódicamente desde pnl_check_loop.
    """
    open_trades = database.get_open_manual_trades()
    if not open_trades:
        return

    print(f"[MANUAL] Verificando {len(open_trades)} trade(s) manual(es) abierto(s)...")
    resolved_count = 0

    for t in open_trades:
        try:
            info = _fetch_market_info(t["market_id"])
            if not info.get("closed"):
                continue
            winning_side = _winning_side_from_market(info)
            if not winning_side:
                continue
            won = database.resolve_manual_trade(t["id"], winning_side)
            tag = "✅ WON" if won else "❌ LOST"
            print(f"[MANUAL] #{t['id']} {t['market_question'][:40]} → {tag} ({winning_side})")

            # Actualizar reputación del trader copiado
            copied_from = t.get("copied_from") or ""
            if copied_from and copied_from not in ("manual", "trusted", ""):
                wallet = database.get_wallet_by_username(copied_from)
                if wallet:
                    entry = float(t.get("entry_price") or 0.5)
                    MY_SIZE = 10.0  # tamaño fijo del usuario
                    pnl = round(MY_SIZE / entry - MY_SIZE, 4) if won and entry > 0 else round(-MY_SIZE, 4)
                    tier = database.update_trade_result(wallet, won, pnl)
                    print(f"[MANUAL] Reputación actualizada: {copied_from} → {tag} | PnL {pnl:+.2f} | tier={tier}")
                else:
                    print(f"[MANUAL] Trader '{copied_from}' no encontrado en wallets — reputación no actualizada")

            resolved_count += 1
        except Exception as e:
            print(f"[MANUAL] Error verificando trade #{t['id']}: {e}")

    if resolved_count:
        print(f"[MANUAL] {resolved_count} trade(s) manual(es) resuelto(s)")


# ── Análisis de patrones ──────────────────────────────────────────────────────

def analyze_patterns() -> dict | None:
    """
    Analiza todos los trades manuales resueltos para identificar patrones.
    Devuelve None si hay menos de MIN_RESOLVED_FOR_PATTERNS resueltos.
    """
    all_trades = database.get_all_manual_trades()
    resolved   = [t for t in all_trades if t["status"] in ("WON", "LOST")]
    open_cnt   = [t for t in all_trades if t["status"] == "OPEN"]

    if len(resolved) < MIN_RESOLVED_FOR_PATTERNS:
        return {
            "insufficient_data": True,
            "total":     len(all_trades),
            "resolved":  len(resolved),
            "open":      len(open_cnt),
            "needed":    MIN_RESOLVED_FOR_PATTERNS,
        }

    wins  = [t for t in resolved if t["won"] == 1]
    total = len(resolved)
    wr    = round(len(wins) / total, 3)
    total_pnl = sum((t["pnl"] or 0) for t in resolved)

    def _bucket_stats(trades, key, buckets):
        out = {}
        for label, lo, hi in buckets:
            subset = [t for t in trades if t.get(key) is not None and lo <= t[key] < hi]
            if not subset:
                continue
            w = sum(1 for t in subset if t["won"] == 1)
            out[label] = {"total": len(subset), "wins": w,
                           "win_rate": round(w / len(subset), 3),
                           "pnl": round(sum((t["pnl"] or 0) for t in subset), 2)}
        return out

    # Por trader copiado
    by_trader: dict = {}
    for t in resolved:
        tr = t.get("copied_from") or "manual"
        if tr not in by_trader:
            by_trader[tr] = {"total": 0, "wins": 0, "pnl": 0}
        by_trader[tr]["total"] += 1
        by_trader[tr]["wins"]  += 1 if t["won"] == 1 else 0
        by_trader[tr]["pnl"]   += t.get("pnl") or 0
    for tr, s in by_trader.items():
        s["win_rate"] = round(s["wins"] / s["total"], 3) if s["total"] > 0 else 0
        s["pnl"]      = round(s["pnl"], 2)
    by_trader = dict(sorted(by_trader.items(),
                            key=lambda x: (x[1]["win_rate"], x[1]["total"]),
                            reverse=True))

    # Por nicho
    by_niche: dict = {}
    for t in resolved:
        n = t.get("niche") or "other"
        if n not in by_niche:
            by_niche[n] = {"total": 0, "wins": 0, "pnl": 0}
        by_niche[n]["total"] += 1
        by_niche[n]["wins"]  += 1 if t["won"] == 1 else 0
        by_niche[n]["pnl"]   += t.get("pnl") or 0
    for n, s in by_niche.items():
        s["win_rate"] = round(s["wins"] / s["total"], 3) if s["total"] > 0 else 0
        s["pnl"]      = round(s["pnl"], 2)

    # Por probabilidad de entrada (precio)
    by_price = _bucket_stats(
        resolved, "entry_price",
        [("baja (0.30-0.50)", 0.30, 0.50),
         ("media (0.50-0.70)", 0.50, 0.70),
         ("alta (0.70-0.85)",  0.70, 0.85)]
    )

    # Por días restantes
    by_days = _bucket_stats(
        resolved, "days_left",
        [("0d", 0, 1), ("1-3d", 1, 4), ("4-7d", 4, 8), ("7d+", 8, 9999)]
    )

    # Mejor patrón combinado (trader + niche)
    best_pattern = None
    best_wr_p    = 0.0
    for trader_name, ts in by_trader.items():
        if ts["total"] >= 3 and ts["win_rate"] > best_wr_p:
            best_wr_p    = ts["win_rate"]
            best_pattern = f"trader={trader_name} (WR {round(ts['win_rate']*100)}%, {ts['total']} trades)"

    return {
        "insufficient_data": False,
        "total":        len(all_trades),
        "resolved":     total,
        "open":         len(open_cnt),
        "wins":         len(wins),
        "win_rate":     wr,
        "total_pnl":    round(total_pnl, 2),
        "by_trader":    by_trader,
        "by_niche":     by_niche,
        "by_price":     by_price,
        "by_days":      by_days,
        "best_pattern": best_pattern,
    }


# ── Mensajes Telegram ─────────────────────────────────────────────────────────

def build_open_trades_message() -> str:
    """Mensaje para /trades — lista de trades manuales abiertos."""
    trades = database.get_open_manual_trades()
    if not trades:
        return (
            "📋 SIN TRADES MANUALES ABIERTOS\n\n"
            "Usa /logtrade para registrar tu próximo trade:\n"
            "/logtrade <market_id> <YES|NO> <precio> <USDC> "
            "[trader] [\"razón\"] [días]"
        )

    lines = [f"📋 TRADES MANUALES ABIERTOS ({len(trades)})", "─" * 36, ""]
    for t in trades:
        retorno = round((1 / t["entry_price"]) - 1, 2) if t["entry_price"] > 0 else 0
        days_str = f"{t['days_left']}d" if t["days_left"] < 999 else "sin fecha"
        q = (t["market_question"] or t["market_id"])[:50]
        lines += [
            f"#{t['id']} — {q}",
            f"   {t['side']} @{t['entry_price']} | {t['size_usdc']} USDC | +{retorno}x | {days_str}",
        ]
        if t["copied_from"] and t["copied_from"] != "manual":
            lines.append(f"   📌 {t['copied_from']}" + (f" — {t['reason']}" if t["reason"] else ""))
        lines.append("")

    lines += [
        "Resolver: /traderesult <id> won|lost",
        "Patrones: /tradepatterns",
    ]
    return "\n".join(lines)


def build_patterns_message() -> str:
    """Mensaje para /tradepatterns — análisis completo."""
    p = analyze_patterns()
    if p is None or p.get("insufficient_data"):
        needed   = (p or {}).get("needed", MIN_RESOLVED_FOR_PATTERNS)
        resolved = (p or {}).get("resolved", 0)
        total    = (p or {}).get("total", 0)
        open_cnt = (p or {}).get("open", 0)
        return (
            f"📊 ANÁLISIS DE PATRONES MANUALES\n\n"
            f"⏳ Acumulando datos ({resolved}/{needed} resueltos)\n"
            f"  Total registrados: {total}  |  Abiertos: {open_cnt}\n\n"
            f"Registra más trades con /logtrade y espera a que resuelvan.\n"
            f"El análisis se activa con {needed} trades resueltos."
        )

    wr_pct   = round(p["win_rate"] * 100, 1)
    wr_emoji = "🟢" if wr_pct >= 70 else "🟡" if wr_pct >= 50 else "🔴"
    pnl_str  = f"{p['total_pnl']:+.2f}"

    lines = [
        f"📊 PATRONES — TUS TRADES MANUALES",
        "─" * 36,
        "",
        f"{wr_emoji} Win rate: {wr_pct}%  ({p['wins']}W / {p['resolved'] - p['wins']}L)",
        f"💰 P&L total: {pnl_str} USDC  |  {p['open']} abierto(s)",
        "",
    ]

    if p["by_trader"]:
        lines.append("👤 Por trader copiado:")
        for trader, s in list(p["by_trader"].items())[:5]:
            e = "🟢" if s["win_rate"] >= 0.65 else "🟡" if s["win_rate"] >= 0.50 else "🔴"
            lines.append(
                f"  {e} {trader}: {round(s['win_rate']*100)}%  "
                f"({s['total']} trades · {s['pnl']:+.0f} USDC)"
            )
        lines.append("")

    if p["by_niche"]:
        lines.append("🏷 Por nicho:")
        for niche, s in sorted(p["by_niche"].items(),
                                key=lambda x: x[1]["win_rate"], reverse=True):
            e = "🟢" if s["win_rate"] >= 0.65 else "🟡" if s["win_rate"] >= 0.50 else "🔴"
            lines.append(
                f"  {e} {niche.upper()}: {round(s['win_rate']*100)}%  "
                f"({s['total']} trades · {s['pnl']:+.0f} USDC)"
            )
        lines.append("")

    if p["by_price"]:
        lines.append("💲 Por precio de entrada:")
        for bucket, s in p["by_price"].items():
            e = "🟢" if s["win_rate"] >= 0.65 else "🟡" if s["win_rate"] >= 0.50 else "🔴"
            lines.append(f"  {e} {bucket}: {round(s['win_rate']*100)}% ({s['total']} trades)")
        lines.append("")

    if p["by_days"]:
        lines.append("📅 Por días restantes al entrar:")
        for bucket, s in p["by_days"].items():
            e = "🟢" if s["win_rate"] >= 0.65 else "🟡" if s["win_rate"] >= 0.50 else "🔴"
            lines.append(f"  {e} {bucket}: {round(s['win_rate']*100)}% ({s['total']} trades)")
        lines.append("")

    if p["best_pattern"]:
        lines += [
            "💡 Mejor patrón identificado:",
            f"   {p['best_pattern']}",
            "",
        ]

    if p["resolved"] >= 50:
        lines += [
            "🤖 Con 50+ trades resueltos el sistema puede",
            "   sugerir auto-ejecución de tus patrones ganadores.",
            "",
        ]

    lines.append("/logtrade para registrar · /trades para ver abiertos")
    return "\n".join(lines)
