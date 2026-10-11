"""
hot_streak_scanner.py — Detección de traders en racha ganadora
===============================================================
Flujo cada 6 horas (+ al arrancar):
  1. Obtiene top 100 traders por PnL total del leaderboard
  2. Pre-filtra: PnL > $5k · PF ≥ 2× · WR ≥ 55% · 25+ cerradas
  3. Para cada candidato: analiza últimos 5, 10, 20 trades
  4. HOT = last_10_wr ≥ 70% OR last_5_wr ≥ 80%
  5. Nuevos hot streakers → DB + alerta Telegram
  6. DB watch activo 48 h; trusted_scanner los monitorea en modo WATCH

Filtros de entrada (pre-fetch pesado):
  MIN_PNL_USD    > $5,000  (leaderboard lo da directamente)
  MIN_WR         ≥ 55%     (estimado desde leaderboard si disponible)
  MIN_PF         ≥ 2.0
  MIN_CLOSED     ≥ 25
"""

import asyncio
import requests
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import config
import database
import telegram_bot

DATA_URL  = "https://data-api.polymarket.com"

SCAN_INTERVAL_H = 6        # horas entre escaneos
WATCH_TTL_H     = 48       # horas que dura un hot streak en la watch list

# ── Pre-filtros (aplicados sobre datos del leaderboard + historial) ────────────
MIN_PNL_USD     = 5_000    # PnL total > $5k
MIN_WR_HIST     = 0.55     # WR histórico ≥ 55%
MIN_PF          = 2.0      # profit factor ≥ 2×
MIN_CLOSED      = 25       # al menos 25 posiciones cerradas

# ── Umbrales de HOT STREAK ────────────────────────────────────────────────────
HOT_LAST_10_WR  = 0.70     # ≥ 70% de los últimos 10 = racha
HOT_LAST_5_WR   = 0.80     # ≥ 80% de los últimos 5  = racha fuerte

LEADERBOARD_LIMIT = 100    # traders a analizar del top por PnL


# ── Helpers de fecha ──────────────────────────────────────────────────────────

def _parse_dt(raw):
    if not raw:
        return None
    try:
        s = str(raw).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None

def _is_closed(p, now):
    if bool(p.get("redeemable", False)):
        return True
    end_dt = _parse_dt(p.get("endDate") or p.get("endDateIso") or "")
    return end_dt is not None and end_dt < now


# ── Leaderboard ───────────────────────────────────────────────────────────────

def fetch_top_pnl_traders(limit: int = LEADERBOARD_LIMIT) -> list:
    """
    Obtiene los top traders por PnL total del leaderboard.
    Usa timePeriod=all para all-time; cae back a month si falla.
    Devuelve lista de {wallet, name, leaderboard_pnl}.
    """
    result = []
    for period in ("all", "year", "month"):
        seen = set()
        fetched = []
        for offset in range(0, limit, 50):
            try:
                url = (DATA_URL + "/v1/leaderboard"
                       + f"?timePeriod={period}&orderBy=PNL"
                       + f"&limit=50&offset={offset}&category=overall")
                r = requests.get(url, timeout=15)
                if r.status_code != 200:
                    break
                data = r.json()
                if not data:
                    break
                for t in data:
                    wallet = t.get("proxyWallet", "")
                    if wallet and wallet not in seen:
                        seen.add(wallet)
                        pnl = float(t.get("pnl", 0) or 0)
                        fetched.append({
                            "wallet": wallet,
                            "name":   (t.get("userName") or t.get("name") or "anon")[:30],
                            "leaderboard_pnl": pnl,
                        })
                if len(fetched) >= limit:
                    break
            except Exception as e:
                print(f"[HOT] Error leaderboard offset={offset} period={period}: {e}")
                break
        if fetched:
            result = fetched[:limit]
            print(f"[HOT] Leaderboard ({period}): {len(result)} traders")
            return result
    return result


# ── Posiciones recientes ──────────────────────────────────────────────────────

def _fetch_positions_unbiased(wallet: str) -> list:
    """Fetch posiciones sin sesgo (mismo método que leaderboard_analyzer)."""
    seen, result = set(), []
    for direction in ("desc", "asc"):
        try:
            r = requests.get(
                DATA_URL + "/positions?user=" + wallet
                + "&limit=500&sortBy=CASHPNL&sortDirection=" + direction,
                timeout=20,
            )
            if r.status_code != 200:
                continue
            raw = r.json()
            if not isinstance(raw, list):
                continue
        except Exception as e:
            print(f"[HOT] Error positions {wallet[:10]} {direction}: {e}")
            continue
        for p in raw:
            key = (p.get("conditionId", ""), p.get("outcome", ""), p.get("asset", ""))
            if key not in seen:
                seen.add(key)
                result.append(p)
    return result


def get_recent_closed(wallet: str) -> tuple:
    """
    Devuelve (closed_sorted, stats_hist) donde:
      closed_sorted — posiciones cerradas ordenadas por fecha de cierre desc (más reciente primero)
      stats_hist    — dict con win_rate, profit_factor, closed total
    Devuelve (None, None) si no se puede calcular.
    """
    raw = _fetch_positions_unbiased(wallet)
    if not raw:
        return None, None

    now = datetime.now(timezone.utc)
    closed = []
    for p in raw:
        size = float(p.get("initialValue", p.get("size", 0)) or 0)
        if size <= 0:
            continue
        if _is_closed(p, now):
            closed.append(p)

    if not closed:
        return None, None

    # Ordenar por endDate desc para tener las más recientes primero
    def _end_sort_key(p):
        dt = _parse_dt(p.get("endDate") or p.get("endDateIso") or "")
        return dt if dt else datetime(1970, 1, 1, tzinfo=timezone.utc)

    closed.sort(key=_end_sort_key, reverse=True)

    # Stats históricas
    wins   = sum(1 for p in closed if float(p.get("cashPnl", 0) or 0) > 0)
    losses = sum(1 for p in closed if float(p.get("cashPnl", 0) or 0) < 0)
    total  = len(closed)
    avg_win  = (sum(float(p.get("cashPnl", 0)) for p in closed if float(p.get("cashPnl", 0)) > 0)
                / wins) if wins > 0 else 0
    avg_loss = abs(sum(float(p.get("cashPnl", 0)) for p in closed if float(p.get("cashPnl", 0)) < 0)
                   / losses) if losses > 0 else 1
    stats_hist = {
        "total":         total,
        "wins":          wins,
        "losses":        losses,
        "win_rate":      round(wins / total, 4) if total > 0 else 0,
        "profit_factor": round(avg_win / avg_loss, 2) if avg_loss > 0 else 0,
        "avg_win":       round(avg_win, 2),
        "avg_loss":      round(avg_loss, 2),
    }
    return closed, stats_hist


def _window_wr(closed: list, n: int) -> float | None:
    """Win rate de los últimos N trades cerrados. None si hay menos de N."""
    if len(closed) < n:
        return None
    window = closed[:n]
    wins   = sum(1 for p in window if float(p.get("cashPnl", 0) or 0) > 0)
    return round(wins / n, 4)


# ── Análisis de un trader ─────────────────────────────────────────────────────

def analyze_streak(trader: dict) -> dict | None:
    """
    Devuelve dict con métricas de racha o None si no pasa filtros.
    trader: {wallet, name, leaderboard_pnl}
    """
    wallet = trader["wallet"]
    name   = trader["name"]
    pnl    = trader["leaderboard_pnl"]

    # Pre-filtro rápido por PnL
    if pnl < MIN_PNL_USD:
        return None

    closed, hist = get_recent_closed(wallet)
    if closed is None or hist is None:
        return None

    # Pre-filtros sobre historial
    if hist["total"] < MIN_CLOSED:
        return None
    if hist["win_rate"] < MIN_WR_HIST:
        return None
    if hist["profit_factor"] < MIN_PF:
        return None

    # Métricas de racha
    wr5  = _window_wr(closed, 5)
    wr10 = _window_wr(closed, 10)
    wr20 = _window_wr(closed, 20)

    # ¿Está hot?
    hot_10 = wr10 is not None and wr10 >= HOT_LAST_10_WR
    hot_5  = wr5  is not None and wr5  >= HOT_LAST_5_WR
    is_hot = hot_10 or hot_5

    if not is_hot:
        return None

    return {
        "wallet":        wallet,
        "name":          name,
        "total_pnl":     pnl,
        "hist_wr":       hist["win_rate"],
        "hist_closed":   hist["total"],
        "profit_factor": hist["profit_factor"],
        "avg_win":       hist["avg_win"],
        "avg_loss":      hist["avg_loss"],
        "wr5":           wr5,
        "wr10":          wr10,
        "wr20":          wr20,
        "hot_10":        hot_10,
        "hot_5":         hot_5,
    }


# ── Telegram ──────────────────────────────────────────────────────────────────

def _streak_bar(wr: float | None) -> str:
    if wr is None:
        return "—"
    blocks = round(wr * 5)
    return "🟩" * blocks + "⬜" * (5 - blocks) + f"  {round(wr*100)}%"

async def send_hot_streak_alert(hot_traders: list, newly_found: list):
    """
    Envía alerta de hot streaks detectados.
    hot_traders — todos los activos en DB (incluyendo previos)
    newly_found — los detectados en este ciclo por primera vez
    """
    if not newly_found:
        return

    n = len(newly_found)
    lines = [
        f"🔥 HOT STREAKS DETECTADOS — {n} trader{'s' if n > 1 else ''} en racha",
        f"📅 {datetime.now().strftime('%d/%m %H:%M')}  ·  Watch activo 48h",
        "",
    ]

    for i, t in enumerate(newly_found, 1):
        wr5_str  = f"{round(t['wr5']*100)}%"  if t["wr5"]  is not None else "—"
        wr10_str = f"{round(t['wr10']*100)}%" if t["wr10"] is not None else "—"
        wr20_str = f"{round(t['wr20']*100)}%" if t["wr20"] is not None else "—"

        hot_tag = ""
        if t["wr5"]  is not None and t["wr5"]  >= HOT_LAST_5_WR:  hot_tag += "🔥🔥"
        if t["wr10"] is not None and t["wr10"] >= HOT_LAST_10_WR: hot_tag += "🔥"

        lines += [
            f"{'─'*38}",
            f"#{i}  {hot_tag} {t['name']}",
            f"   Últimos  5: {_streak_bar(t['wr5'])}",
            f"   Últimos 10: {_streak_bar(t['wr10'])}",
            f"   Últimos 20: {_streak_bar(t['wr20'])}",
            f"   Histórico:  WR {round(t['hist_wr']*100)}%  "
            f"PF {t['profit_factor']}×  ({t['hist_closed']} cerradas)",
            f"   P&L total:  +${t['total_pnl']:,.0f}",
            f"   Wallet:     {t['wallet'][:10]}...{t['wallet'][-6:]}",
        ]

    # Resumen de todos los activos
    total_active = len(hot_traders)
    if total_active > len(newly_found):
        lines += [
            "",
            f"📋 Watch activo total: {total_active} traders",
        ]

    lines += [
        "",
        "👁  Monitoreando posiciones. Si la racha continúa → promover a TRUSTED.",
    ]

    msg = "\n".join(lines)
    # Telegram tiene límite de 4096 chars por mensaje
    for chunk in [msg[i:i+4000] for i in range(0, len(msg), 4000)]:
        await telegram_bot.send_with_retry(
            telegram_bot.bot.send_message(
                chat_id=config.TELEGRAM_CHAT_ID,
                text=chunk,
            )
        )
        if len(msg) > 4000:
            await asyncio.sleep(1)


# ── DB ────────────────────────────────────────────────────────────────────────

def init_hot_streak_table():
    database._init_hot_streak_table()

def save_hot_streak(trader: dict):
    database.save_hot_streak_trader(
        wallet         = trader["wallet"],
        username       = trader["name"],
        last_10_wr     = trader["wr10"] or 0.0,
        last_5_wr      = trader["wr5"]  or 0.0,
        last_20_wr     = trader["wr20"] or 0.0,
        historical_wr  = trader["hist_wr"],
        total_pnl      = trader["total_pnl"],
        profit_factor  = trader["profit_factor"],
    )

def get_active_hot_traders() -> list:
    return database.get_active_hot_streak_traders()

def expire_old_streaks():
    database.expire_hot_streak_traders()


# ── Loop principal ────────────────────────────────────────────────────────────

async def hot_streak_loop():
    """Se ejecuta al arrancar y cada 6 horas."""
    init_hot_streak_table()
    print("[HOT] 🔥 Scanner hot streaks iniciado (cada 6h)")

    while True:
        try:
            await run_hot_streak_scan()
        except Exception as e:
            print(f"[HOT] Error en ciclo: {e}")
        print(f"[HOT] Próximo ciclo en {SCAN_INTERVAL_H}h")
        await asyncio.sleep(SCAN_INTERVAL_H * 3600)


async def run_hot_streak_scan():
    """Ejecuta un ciclo completo de detección."""
    print("[HOT] ── Iniciando escaneo de hot streaks ──")
    expire_old_streaks()

    # 1. Leaderboard top 100 por PnL
    top_traders = fetch_top_pnl_traders(LEADERBOARD_LIMIT)
    if not top_traders:
        print("[HOT] No se pudo obtener leaderboard")
        return

    # Filtrar wallets ya en TRUSTED_TRADERS (ya las monitoreamos)
    try:
        from trusted_scanner import TRUSTED_TRADERS as _trusted
        trusted_wallets = set(_trusted.keys())
    except Exception:
        trusted_wallets = set()

    # Cargar hot streakers ya activos en DB para dedup
    active_db = {t["wallet"] for t in get_active_hot_traders()}

    print(f"[HOT] Analizando {len(top_traders)} traders (excluyendo {len(trusted_wallets)} trusted ya monitoreados)...")

    newly_found = []
    analyzed    = 0

    for i, trader in enumerate(top_traders):
        wallet = trader["wallet"]

        # Skip TRUSTED (ya monitoreados)
        if wallet in trusted_wallets:
            continue
        # Skip PnL insuficiente — evitar fetch pesado
        if trader["leaderboard_pnl"] < MIN_PNL_USD:
            continue

        print(f"[HOT]   [{i+1}/{len(top_traders)}] {trader['name'][:20]}...", end="\r")
        analyzed += 1

        result = analyze_streak(trader)
        if result is None:
            await asyncio.sleep(0.3)   # anti-rate-limit
            continue

        wr10_tag = f"{round(result['wr10']*100)}%" if result["wr10"] is not None else "—"
        wr5_tag  = f"{round(result['wr5']*100)}%" if result["wr5"] is not None else "—"
        print(f"\n[HOT] 🔥 {result['name']} — L10:{wr10_tag}  L5:{wr5_tag}  hist:{round(result['hist_wr']*100)}%  PnL:${result['total_pnl']:,.0f}")

        if wallet not in active_db:
            save_hot_streak(result)
            newly_found.append(result)
        else:
            # Actualizar métricas aunque ya esté en DB
            save_hot_streak(result)
            print(f"[HOT]   ↳ Ya en watch list — actualizado")

        await asyncio.sleep(0.5)

    print(f"\n[HOT] Ciclo completo — analizados={analyzed} nuevos={len(newly_found)} activos={len(get_active_hot_traders())}")

    if newly_found:
        all_active = get_active_hot_traders()
        await send_hot_streak_alert(all_active, newly_found)
    else:
        print("[HOT] Sin nuevos hot streakers en este ciclo")
