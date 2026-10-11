"""
behavior_analyzer.py — Aprendizaje del comportamiento del usuario
=================================================================
Analiza user_feedback para aprender qué trades funcionan para este
usuario y ajusta los filtros automáticamente.

Flujo:
  1. analyze_user_patterns()     → estadísticas por nicho, confianza, días, hora
  2. get_personalized_thresholds()→ umbrales ajustados para monitor.py
  3. generate_weekly_report()    → async; llama a Haiku y manda por Telegram
  4. build_stats_message()       → responde al comando /stats
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

import config
import database

# ── Constantes ────────────────────────────────────────────────────────────────

MIN_RESOLVED_FOR_ANALYSIS    = 20   # feedback resueltos mínimos para analyze
MIN_RESOLVED_FOR_THRESHOLDS  = 20   # ídem para ajustar thresholds en monitor
MIN_SAMPLES_PER_NICHE        = 5    # mínimo por nicho para ser fiable

NICHES = ["esports", "sports", "crypto", "geopolitics", "politics", "other"]

DEFAULTS = {
    "min_confidence":    65.0,
    "min_retorno":       0.20,
    "max_days_left":     30.0,
    "min_insider_score": 0.75,
}

# Cache en memoria — evita consultas a BD en cada trade
_patterns_cache: Optional[dict]  = None
_patterns_cached_at: Optional[datetime] = None
_thresholds_cache: Optional[dict] = None
_thresholds_cached_at: Optional[datetime] = None
_CACHE_TTL_SECS = 3600  # refrescar cada hora


def _invalidate_cache():
    global _patterns_cache, _patterns_cached_at, _thresholds_cache, _thresholds_cached_at
    _patterns_cache = _thresholds_cache = None
    _patterns_cached_at = _thresholds_cached_at = None


# ── Análisis de patrones ──────────────────────────────────────────────────────

def analyze_user_patterns(force_refresh: bool = False) -> Optional[dict]:
    """
    Calcula estadísticas de aprendizaje a partir del historial de feedback.

    Devuelve dict con:
      insufficient_data bool
      total_feedback / resolved_count / copied_resolved
      overall_win_rate / total_pnl
      by_niche / by_confidence / by_days / by_hour
      missed_count / good_ignores_count / missed_opportunities
      top_traders
      generated_at

    Devuelve {"insufficient_data": True, ...} si < MIN_RESOLVED_FOR_ANALYSIS resueltos.
    """
    global _patterns_cache, _patterns_cached_at

    now = datetime.now()
    if (not force_refresh
            and _patterns_cache is not None
            and _patterns_cached_at is not None
            and (now - _patterns_cached_at).total_seconds() < _CACHE_TTL_SECS):
        return _patterns_cache

    all_fb   = database.get_all_feedback_enriched()
    resolved = [f for f in all_fb if f.get("outcome") is not None]
    total    = len(all_fb)
    n_res    = len(resolved)

    if n_res < MIN_RESOLVED_FOR_ANALYSIS:
        result = {
            "insufficient_data": True,
            "total_feedback":    total,
            "resolved_count":    n_res,
            "needed":            MIN_RESOLVED_FOR_ANALYSIS,
        }
        _patterns_cache, _patterns_cached_at = result, now
        return result

    copied_res  = [f for f in resolved if f["action"] == "copied"]
    ignored_res = [f for f in resolved if f["action"] == "ignored"]

    # — Win rate por nicho ─────────────────────────────────────────────────────
    by_niche: dict = {}
    for niche in NICHES:
        trades = [f for f in copied_res if (f.get("niche") or "other") == niche]
        if len(trades) >= MIN_SAMPLES_PER_NICHE:
            wins = sum(1 for f in trades if f["outcome"] == "copied_won")
            by_niche[niche] = {
                "total":    len(trades),
                "wins":     wins,
                "losses":   len(trades) - wins,
                "win_rate": round(wins / len(trades), 3),
            }

    # — Win rate por rango de confianza ────────────────────────────────────────
    by_confidence: dict = {}
    for label, lo, hi in [("60-70", 60, 70), ("70-80", 70, 80), ("80+", 80, 101)]:
        trades = [f for f in copied_res
                  if f.get("confidence") is not None and lo <= f["confidence"] < hi]
        if trades:
            wins = sum(1 for f in trades if f["outcome"] == "copied_won")
            by_confidence[label] = {
                "total":    len(trades),
                "wins":     wins,
                "win_rate": round(wins / len(trades), 3),
            }

    # — Win rate por días restantes ────────────────────────────────────────────
    by_days: dict = {}
    for label, lo, hi in [("0d", 0, 1), ("1-3d", 1, 4), ("4-7d", 4, 8), ("7d+", 8, 9999)]:
        trades = [f for f in copied_res
                  if f.get("days_left") is not None
                  and f["days_left"] < 900
                  and lo <= f["days_left"] < hi]
        if trades:
            wins = sum(1 for f in trades if f["outcome"] == "copied_won")
            by_days[label] = {
                "total":    len(trades),
                "wins":     wins,
                "win_rate": round(wins / len(trades), 3),
            }

    # — Win rate por hora del día ──────────────────────────────────────────────
    by_hour: dict = {}
    _periods = [
        ("mañana (6-12)",  set(range(6, 12))),
        ("tarde (12-20)",  set(range(12, 20))),
        ("noche (20-6)",   set(range(20, 24)) | set(range(0, 6))),
    ]
    for period, hours in _periods:
        trades = [f for f in copied_res if f.get("hour_of_day") in hours]
        if trades:
            wins = sum(1 for f in trades if f["outcome"] == "copied_won")
            by_hour[period] = {
                "total":    len(trades),
                "wins":     wins,
                "win_rate": round(wins / len(trades), 3),
            }

    # — Oportunidades perdidas y buenos ignores ────────────────────────────────
    missed       = [f for f in ignored_res if f["outcome"] == "ignored_would_have_won"]
    good_ignores = [f for f in ignored_res if f["outcome"] == "ignored_good_decision"]

    # — Mejores traders por win rate personal ──────────────────────────────────
    by_wallet: dict = {}
    for f in copied_res:
        w = f.get("wallet") or ""
        if not w:
            continue
        if w not in by_wallet:
            by_wallet[w] = {"total": 0, "wins": 0}
        by_wallet[w]["total"] += 1
        if f["outcome"] == "copied_won":
            by_wallet[w]["wins"] += 1
    for w, s in by_wallet.items():
        s["win_rate"] = round(s["wins"] / s["total"], 3) if s["total"] > 0 else 0
    top_traders = dict(
        sorted(by_wallet.items(),
               key=lambda x: (x[1]["win_rate"], x[1]["total"]),
               reverse=True)[:5]
    )

    # — P&L total real ─────────────────────────────────────────────────────────
    total_pnl = 0.0
    try:
        conn = database.get_connection()
        c = conn.cursor()
        c.execute("SELECT COALESCE(SUM(pnl), 0) FROM live_trades WHERE status='RESOLVED'")
        row = c.fetchone()
        conn.close()
        total_pnl = float(row[0]) if row and row[0] else 0.0
    except Exception:
        pass

    total_copied_res = len(copied_res)
    total_won        = sum(1 for f in copied_res if f["outcome"] == "copied_won")
    overall_wr       = round(total_won / total_copied_res, 3) if total_copied_res > 0 else 0

    result = {
        "insufficient_data":  False,
        "total_feedback":     total,
        "resolved_count":     n_res,
        "copied_resolved":    total_copied_res,
        "overall_win_rate":   overall_wr,
        "total_pnl":          round(total_pnl, 2),
        "by_niche":           by_niche,
        "by_confidence":      by_confidence,
        "by_days":            by_days,
        "by_hour":            by_hour,
        "missed_count":       len(missed),
        "good_ignores_count": len(good_ignores),
        "missed_opportunities": missed[-5:],   # últimas 5
        "top_traders":        top_traders,
        "generated_at":       now.isoformat(),
    }
    _patterns_cache, _patterns_cached_at = result, now
    return result


# ── Umbrales personalizados ───────────────────────────────────────────────────

def get_personalized_thresholds(force_refresh: bool = False) -> Optional[dict]:
    """
    Calcula umbrales óptimos para monitor.py basándose en el historial.

    Devuelve None si no hay datos suficientes (< MIN_RESOLVED_FOR_THRESHOLDS).
    Con datos suficientes devuelve:
      min_confidence   float
      min_retorno      float
      max_days_left    float
      avoid_niches     [{"niche", "win_rate", "trades", "reason"}, ...]
      priority_traders [{"wallet", "win_rate", "trades"}, ...]
      by_niche         {niche: {"boost": bool, "win_rate": float}, ...}
      based_on_samples int
      overall_win_rate float
    """
    global _thresholds_cache, _thresholds_cached_at

    now = datetime.now()
    if (not force_refresh
            and _thresholds_cache is not None
            and _thresholds_cached_at is not None
            and (now - _thresholds_cached_at).total_seconds() < _CACHE_TTL_SECS):
        return _thresholds_cache

    patterns = analyze_user_patterns(force_refresh=force_refresh)
    if patterns is None or patterns.get("insufficient_data"):
        _thresholds_cache, _thresholds_cached_at = None, now
        return None

    thresholds: dict = {
        "min_confidence":    DEFAULTS["min_confidence"],
        "min_retorno":       DEFAULTS["min_retorno"],
        "max_days_left":     DEFAULTS["max_days_left"],
        "avoid_niches":      [],
        "priority_traders":  [],
        "by_niche":          {},
        "based_on_samples":  patterns["resolved_count"],
        "overall_win_rate":  patterns["overall_win_rate"],
    }

    # — Min confidence desde historial ─────────────────────────────────────────
    # El bucket más bajo con WR ≥ 55% y al menos 5 trades
    for label, lo in [("60-70", 60), ("70-80", 70), ("80+", 80)]:
        stats = patterns["by_confidence"].get(label)
        if not stats or stats["total"] < 5:
            continue
        if stats["win_rate"] >= 0.55:
            thresholds["min_confidence"] = float(lo)
            break
        elif stats["win_rate"] < 0.40:
            # Bucket malo: subir el mínimo al siguiente nivel
            thresholds["min_confidence"] = float(min(lo + 10, 80))

    # — Nichos a evitar vs potenciar ───────────────────────────────────────────
    for niche, stats in patterns["by_niche"].items():
        if stats["total"] < MIN_SAMPLES_PER_NICHE:
            continue
        if stats["win_rate"] < 0.45:
            thresholds["avoid_niches"].append({
                "niche":    niche,
                "win_rate": stats["win_rate"],
                "trades":   stats["total"],
                "reason":   f"WR {round(stats['win_rate']*100)}% en {stats['total']} trades",
            })
        elif stats["win_rate"] >= 0.70:
            thresholds["by_niche"][niche] = {
                "boost":          True,
                "win_rate":       stats["win_rate"],
                "min_confidence": max(DEFAULTS["min_confidence"] - 5.0, 55.0),
            }

    # — Max days: ajustar si un bucket corto tiene WR claramente superior ──────
    best_wr, best_max = 0.0, DEFAULTS["max_days_left"]
    for label, max_d in [("0d", 1), ("1-3d", 3), ("4-7d", 7), ("7d+", 30)]:
        s = patterns["by_days"].get(label)
        if s and s["total"] >= 3 and s["win_rate"] > best_wr:
            best_wr, best_max = s["win_rate"], float(max_d)
    if best_wr >= 0.70 and best_max < 30:
        thresholds["max_days_left"] = best_max

    # — Traders prioritarios para este usuario ─────────────────────────────────
    for wallet, stats in patterns["top_traders"].items():
        if stats["total"] >= 3 and stats["win_rate"] >= 0.70:
            thresholds["priority_traders"].append({
                "wallet":   wallet,
                "win_rate": stats["win_rate"],
                "trades":   stats["total"],
            })

    _thresholds_cache, _thresholds_cached_at = thresholds, now
    return thresholds


# ── Reporte semanal con Claude ────────────────────────────────────────────────

async def generate_weekly_report():
    """
    Genera el reporte semanal usando Claude Haiku y lo manda por Telegram.
    Llamar desde weekly_review() en main.py cada lunes.
    """
    import anthropic
    import telegram_bot

    patterns = analyze_user_patterns(force_refresh=True)
    _invalidate_cache()  # forzar recálculo la próxima vez

    if patterns is None or patterns.get("insufficient_data"):
        needed   = (patterns or {}).get("needed", MIN_RESOLVED_FOR_ANALYSIS)
        resolved = (patterns or {}).get("resolved_count", 0)
        total    = (patterns or {}).get("total_feedback", 0)
        msg = (
            "📊 REPORTE SEMANAL\n\n"
            f"⏳ Acumulando datos ({resolved}/{needed} trades resueltos de {total} total)\n\n"
            "Sigue usando el bot — cuando haya suficiente historial recibirás\n"
            "análisis personalizados de Claude cada lunes."
        )
        await telegram_bot.send_with_retry(
            telegram_bot.bot.send_message(chat_id=config.TELEGRAM_CHAT_ID, text=msg)
        )
        return

    # Resumen compacto para Claude ─────────────────────────────────────────────
    lines = [
        f"Win rate global: {round(patterns['overall_win_rate']*100,1)}%",
        f"P&L total: {patterns['total_pnl']:+.2f} USDC",
        f"Trades resueltos: {patterns['resolved_count']} (copiados: {patterns['copied_resolved']})",
        f"Oportunidades perdidas: {patterns['missed_count']}",
        f"Buenos ignores: {patterns['good_ignores_count']}",
        "",
        "WR por nicho:",
    ]
    for niche, s in patterns["by_niche"].items():
        lines.append(f"  {niche}: {round(s['win_rate']*100)}% ({s['total']} trades)")
    lines.append("\nWR por rango de confianza:")
    for band, s in patterns["by_confidence"].items():
        lines.append(f"  {band}%: {round(s['win_rate']*100)}% ({s['total']} trades)")
    lines.append("\nWR por tiempo restante al copiar:")
    for band, s in patterns["by_days"].items():
        lines.append(f"  {band}: {round(s['win_rate']*100)}% ({s['total']} trades)")
    lines.append("\nWR por hora del día:")
    for period, s in patterns["by_hour"].items():
        lines.append(f"  {period}: {round(s['win_rate']*100)}% ({s['total']} trades)")

    prompt = (
        "Eres el asistente de un trader que copia trades de insiders en Polymarket.\n"
        "Aquí están sus estadísticas de esta semana:\n\n"
        + "\n".join(lines)
        + "\n\nResponde en español, de forma concisa (máx 180 palabras total). Incluye:\n"
        "1. Qué está funcionando bien (máx 2 puntos clave)\n"
        "2. Qué cambios recomiendas en los filtros (máx 2 sugerencias concretas)\n"
        "3. Qué tipo de trades debería ignorar más\n"
        "4. Qué oportunidades está perdiendo\n"
        "Usa bullet points. Sin introducciones largas."
    )

    ai_text = ""
    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        for block in resp.content:
            if hasattr(block, "text"):
                ai_text += block.text
    except Exception as e:
        print(f"[LEARN] Error Claude weekly report: {e}")
        ai_text = "⚠️ No disponible (error contactando Claude)."

    wr_pct   = round(patterns["overall_win_rate"] * 100, 1)
    wr_emoji = "🟢" if wr_pct >= 70 else "🟡" if wr_pct >= 50 else "🔴"

    msg = "\n".join([
        f"📊 REPORTE SEMANAL — {datetime.now().strftime('%d/%m/%Y')}",
        "",
        f"  {wr_emoji} Win rate: {wr_pct}%  ({patterns['copied_resolved']} resueltos)",
        f"  💰 P&L real: {patterns['total_pnl']:+.2f} USDC",
        f"  🎯 Copiados: {patterns['copied_resolved']}  |  ❌ Perdidas: {patterns['missed_count']}",
        "",
        "🤖 Análisis de Claude:",
        ai_text,
    ])

    await telegram_bot.send_with_retry(
        telegram_bot.bot.send_message(chat_id=config.TELEGRAM_CHAT_ID, text=msg)
    )
    print("[LEARN] ✅ Reporte semanal enviado")


# ── Dashboard /stats ──────────────────────────────────────────────────────────

def build_stats_message() -> str:
    """
    Construye el mensaje completo para el comando /stats de Telegram.
    Incluye win rate, mejor/peor nicho, P&L real y recomendación del sistema.
    """
    patterns = analyze_user_patterns()

    if patterns is None or patterns.get("insufficient_data"):
        needed   = (patterns or {}).get("needed", MIN_RESOLVED_FOR_ANALYSIS)
        resolved = (patterns or {}).get("resolved_count", 0)
        total    = (patterns or {}).get("total_feedback", 0)
        return (
            "📊 ESTADÍSTICAS PERSONALIZADAS\n\n"
            f"⏳ Acumulando historial...\n"
            f"  Feedback total: {total}\n"
            f"  Resueltos: {resolved}/{needed}\n\n"
            f"Cuando haya {needed} trades resueltos obtendrás estadísticas\n"
            "completas y filtros ajustados a tu comportamiento.\n\n"
            "Sigue copiando e ignorando para entrenar el sistema."
        )

    wr_pct   = round(patterns["overall_win_rate"] * 100, 1)
    wr_emoji = "🟢" if wr_pct >= 70 else "🟡" if wr_pct >= 50 else "🔴"
    pnl      = patterns["total_pnl"]
    pnl_str  = f"{pnl:+.2f}"

    lines = [
        f"📊 TUS ESTADÍSTICAS — {datetime.now().strftime('%d/%m/%Y %H:%M')}",
        "─" * 38,
        "",
        f"{wr_emoji} Win rate real: {wr_pct}%  ({patterns['copied_resolved']} trades resueltos)",
        f"💰 P&L total:    {pnl_str} USDC",
        (f"📋 Feedback:     {patterns['total_feedback']} total  |  "
         f"{patterns['resolved_count']} resueltos"),
        (f"✅ Buenos ignores: {patterns['good_ignores_count']}  |  "
         f"❌ Perdidas: {patterns['missed_count']}"),
        "",
    ]

    # Mejor y peor nicho ────────────────────────────────────────────────────────
    niches_sorted = sorted(
        patterns["by_niche"].items(), key=lambda x: x[1]["win_rate"], reverse=True
    )
    if niches_sorted:
        best_n, best_s = niches_sorted[0]
        lines.append(
            f"🏆 Mejor nicho:  {best_n.upper()} — "
            f"{round(best_s['win_rate']*100)}% ({best_s['total']} trades)"
        )
        if len(niches_sorted) > 1:
            worst_n, worst_s = niches_sorted[-1]
            lines.append(
                f"⚠️  Peor nicho:  {worst_n.upper()} — "
                f"{round(worst_s['win_rate']*100)}% ({worst_s['total']} trades)"
            )
        lines.append("")

    # Win rate por confianza ────────────────────────────────────────────────────
    if patterns["by_confidence"]:
        lines.append("📊 WR por confianza del modelo:")
        for band, s in patterns["by_confidence"].items():
            e = "🟢" if s["win_rate"] >= 0.65 else "🟡" if s["win_rate"] >= 0.50 else "🔴"
            lines.append(f"  {e} {band}%: {round(s['win_rate']*100)}% ({s['total']} trades)")
        lines.append("")

    # Win rate por tiempo restante ──────────────────────────────────────────────
    if patterns["by_days"]:
        lines.append("📅 WR por tiempo restante al copiar:")
        for band, s in patterns["by_days"].items():
            e = "🟢" if s["win_rate"] >= 0.65 else "🟡" if s["win_rate"] >= 0.50 else "🔴"
            lines.append(f"  {e} {band}: {round(s['win_rate']*100)}% ({s['total']} trades)")
        lines.append("")

    # Recomendación del sistema ─────────────────────────────────────────────────
    thresholds = get_personalized_thresholds()
    if thresholds:
        rec_lines = []
        if thresholds.get("avoid_niches"):
            for an in thresholds["avoid_niches"][:2]:
                rec_lines.append(f"  ⛔ Evitar {an['niche'].upper()} — {an['reason']}")
        if thresholds.get("priority_traders"):
            rec_lines.append(
                f"  ⭐ {len(thresholds['priority_traders'])} trader(s) con mejor "
                f"hit rate personal identificado(s)"
            )
        if patterns["missed_count"] >= 3:
            rec_lines.append(
                f"  📌 Has perdido {patterns['missed_count']} oportunidades ganadoras"
            )
        if thresholds["min_confidence"] != DEFAULTS["min_confidence"]:
            rec_lines.append(
                f"  🎯 Confianza mínima aprendida: {thresholds['min_confidence']}%"
            )
        if rec_lines:
            lines.append("💡 Recomendación del sistema:")
            lines.extend(rec_lines)
            lines.append("")

    lines.append("📆 Reporte detallado con Claude: cada lunes automáticamente")
    return "\n".join(lines)
