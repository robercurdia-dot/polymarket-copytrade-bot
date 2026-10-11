"""
trusted_scanner.py — Escáner de posiciones + cross-reference entre traders trusted
====================================================================================
Flujo cada 20 min:
  1. Fetch posiciones abiertas de TODOS los trusted traders
  2. Cross-reference: buscar mercados donde ≥2 traders tienen posición
       → mismo lado  = CONSENSO  (señal fuerte, se alerta primero)
       → lados opuestos = CONFLICTO (alerta de cautela)
  3. Rankear el resto por score = cashPnl × curPrice
  4. Enviar top 10 individuales por trader (solo los no alertados antes)

Ranking: score = cashPnl × curPrice
  cashPnl alto  → trader ganando mucho dinero aquí
  curPrice alto → mercado lo da como casi ganado (alta probabilidad)
"""

import asyncio
import hashlib
import requests
import re as _re
from collections import defaultdict
from datetime import datetime, timezone

import config
import database
import telegram_bot
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from leaderboard_analyzer import NICHE_KEYWORDS
from trade_evaluator import evaluate_trade, format_evaluation_for_telegram, MIN_CONFIDENCE

SCAN_INTERVAL  = 20 * 60   # segundos entre escaneos
TOP_PER_TRADER = 10        # máximo alertas individuales por trader por ciclo

MIN_REAL_WR = 0.75         # WR real mínimo para procesar un trader

# ── Traders activos (WR real ≥ 75%) ──────────────────────────────────────────
TRUSTED_TRADERS = {
    # sovereign2013: 79.2% WR real · 24 cerradas · PF 3.1 · multi-sport
    "0xee613b3fc183ee44f9da9c05f53e2da107e3debf": {
        "username":    "sovereign2013",
        "alias":       "sovereign",
        "emoji":       "👑",
        "description": "79.2% WR real · 24 cerradas · PnL +1,045 USDC · multi-sport",
        "min_size":    20,
        "niche":       "sports",
        "real_wr":     0.792,
        "real_pnl":    1045,
    },
    # denizz: API no devuelve posiciones aún (posible wallet proxy diferente).
    # WR conocida ~87% sobre muestra pequeña. Se mantiene activo para seguimiento.
    "0xbaa2bcb543dde9bf6ded15b26b7dca6fe0aad6a9": {
        "username":    "denizz",
        "alias":       "denizz",
        "emoji":       "🧠",
        "description": "Sin datos API · geopolítica Irán",
        "min_size":    100,
        "niche":       "geopolitics",
        "real_wr":     None,   # API no devuelve posiciones — se mantiene por reputación
        "real_pnl":    None,
    },
    # Gohst: 70.5% WR real · 44 cerradas · PF 7.28 · política · +12,487 USDC PnL
    # real_wr=None para no ser bloqueado por el gate de 75% — su PF excepcional
    # (7.28×: +427 USDC/win, -59 USDC/loss) justifica seguimiento activo.
    "0x2d4bf8f846bf68f43b9157bf30810d334ac6ca7a": {
        "username":    "Gohst",
        "alias":       "gohst",
        "emoji":       "👻",
        "description": "70.5% WR · 44 cerradas (31W/13L) · PF 7.28× · política +12,487 USDC",
        "min_size":    50,
        "niche":       "politics",
        "real_wr":     None,   # PF excepcional (7.28×) — monitoreado a pesar de WR < 75%
        "real_pnl":    12487,
    },
}

# ── Traders en observación (WR real < 75% — solo log, sin alertas) ───────────
WATCH_TRADERS = {
    # elkmonkey: WR real 42.4% sobre 33 cerradas. PF 2.9.
    # Descalificado por WR < 75%. Se monitorea por si mejora con más muestra.
    "0xead152b855effa6b5b5837f53b24c0756830c76a": {
        "username":    "elkmonkey",
        "alias":       "elkmonkey",
        "emoji":       "🦊",
        "description": "42.4% WR real · 33 cerradas · PnL +611 USDC · NBA",
        "min_size":    50,
        "niche":       "sports",
        "real_wr":     0.424,
        "real_pnl":    611,
    },
}

DATA_URL = "https://data-api.polymarket.com"


# ── Clasificación de nicho ────────────────────────────────────────────────────

def _kw_in_q(kw: str, q: str) -> bool:
    if _re.match(r'^\w+$', kw):
        return bool(_re.search(r'\b' + kw + r'\b', q))
    return kw in q

def _get_niche(title: str):
    q = title.lower()
    for niche, kws in NICHE_KEYWORDS.items():
        if any(_kw_in_q(kw.lower(), q) for kw in kws):
            return niche
    if _re.search(r'[A-Z][A-Za-z]* vs\.? [A-Z]', title):
        return "sports"
    return None

_NICHE_EMOJI  = {"esports":"🏆","sports":"🏆","crypto":"💎",
                 "geopolitics":"🏛","politics":"🏛"}
_NICHE_LABEL  = {"esports":"DEPORTES","sports":"DEPORTES","crypto":"CRYPTO",
                 "geopolitics":"GEOPOLÍTICA","politics":"POLÍTICA"}


# ── Scoring ───────────────────────────────────────────────────────────────────

def _score(pos: dict) -> float:
    """score = cashPnl × curPrice  → premia ganancia grande Y probabilidad alta."""
    pnl   = float(pos.get("cashPnl", 0) or 0)
    price = float(pos.get("curPrice", pos.get("avgPrice", 0.5)) or 0.5)
    return pnl * price


# ── BD: dedup ────────────────────────────────────────────────────────────────

def _init_table():
    conn = database.get_connection()
    c    = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS trusted_position_alerts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet       TEXT,
            condition_id TEXT,
            outcome      TEXT,
            alerted_at   TEXT,
            UNIQUE(wallet, condition_id, outcome)
        )
    """)
    # Tabla de consensos/conflictos ya alertados
    c.execute("""
        CREATE TABLE IF NOT EXISTS trusted_cross_alerts (
            condition_id TEXT,
            alert_type   TEXT,
            alerted_at   TEXT,
            PRIMARY KEY  (condition_id, alert_type)
        )
    """)
    conn.commit()
    conn.close()

def _is_alerted(wallet: str, cid: str, outcome: str) -> bool:
    conn = database.get_connection()
    c    = conn.cursor()
    c.execute("SELECT 1 FROM trusted_position_alerts WHERE wallet=? AND condition_id=? AND outcome=?",
              (wallet, cid, outcome))
    found = c.fetchone() is not None
    conn.close()
    return found

def _mark_alerted(wallet: str, cid: str, outcome: str):
    try:
        conn = database.get_connection()
        c    = conn.cursor()
        c.execute("INSERT OR IGNORE INTO trusted_position_alerts (wallet,condition_id,outcome,alerted_at) VALUES(?,?,?,?)",
                  (wallet, cid, outcome, datetime.utcnow().isoformat()))
        conn.commit(); conn.close()
    except Exception as e:
        print(f"[TRUSTED] Error dedup: {e}")

def _is_cross_alerted(cid: str, alert_type: str) -> bool:
    conn = database.get_connection()
    c    = conn.cursor()
    c.execute("SELECT 1 FROM trusted_cross_alerts WHERE condition_id=? AND alert_type=?", (cid, alert_type))
    found = c.fetchone() is not None
    conn.close()
    return found

def _mark_cross_alerted(cid: str, alert_type: str):
    try:
        conn = database.get_connection()
        c    = conn.cursor()
        c.execute("INSERT OR IGNORE INTO trusted_cross_alerts (condition_id,alert_type,alerted_at) VALUES(?,?,?)",
                  (cid, alert_type, datetime.utcnow().isoformat()))
        conn.commit(); conn.close()
    except Exception as e:
        print(f"[TRUSTED] Error cross-dedup: {e}")

def _cid(pos: dict) -> str:
    c = pos.get("conditionId", "")
    if not c:
        c = "noid_" + hashlib.md5((pos.get("title","") + pos.get("outcome","")).encode()).hexdigest()[:12]
    return c

_SYMBOLIC_THRESHOLD = 0.01  # < 1% del total → apuesta simbólica, no cobertura

def _extract_match_names_ts(title: str) -> list:
    """Extrae [nombre_A, nombre_B] de títulos con ' vs '."""
    m = _re.split(r'\s+vs\.?\s+', title, maxsplit=1, flags=_re.IGNORECASE)
    if len(m) == 2:
        a = _re.split(r'[:\(]', m[0])[0].strip()
        b = _re.split(r'[:\(]', m[1])[0].strip()
        if a and b:
            return [a, b]
    return []

def _is_hedged(pos: dict, all_wallet_positions: list) -> bool:
    """
    Devuelve True si el trader tiene dinero real en AMBOS lados del mismo mercado
    o en ambos jugadores/equipos del mismo partido (mercados separados).

    Regla: cualquier importe en ambos lados = cobertura.
    Excepción: si el lado menor es < 1% del total → apuesta simbólica, no filtra.
    """
    cid      = pos.get("conditionId", "")
    title    = pos.get("title", "")
    outcome  = pos.get("outcome", "")
    size_pos = float(pos.get("initialValue", pos.get("size", 0)) or 0)

    # ── Caso 1: mismo conditionId, outcome diferente ──────────────────────────
    for other in all_wallet_positions:
        if other is pos:
            continue
        if other.get("conditionId", "") != cid or not cid:
            continue
        if other.get("outcome", "") == outcome:
            continue
        size_other = float(other.get("initialValue", other.get("size", 0)) or 0)
        total = size_pos + size_other
        if total <= 0:
            continue
        minor = min(size_pos, size_other)
        if minor / total >= _SYMBOLIC_THRESHOLD:
            return True

    # ── Caso 2: mismo partido vía ' vs ', mercados separados ─────────────────
    names = _extract_match_names_ts(title)
    if not names:
        return False

    for other in all_wallet_positions:
        if other is pos:
            continue
        other_cid = other.get("conditionId", "")
        # Mismo conditionId ya lo cubre Caso 1
        if cid and other_cid and other_cid == cid:
            continue
        other_title = other.get("title", "")
        # Extraer nombres del partido del otro título (o del mismo título con distinto outcome)
        other_names = _extract_match_names_ts(other_title) if other_title != title else names
        if not other_names:
            continue
        if set(n.lower() for n in other_names) != set(n.lower() for n in names):
            continue
        # Mismo partido — verificar que son lados opuestos
        if other.get("outcome", "") == outcome and other_title == title:
            continue  # mismo outcome en mismo mercado = duplicado, no cobertura
        size_other = float(other.get("initialValue", other.get("size", 0)) or 0)
        total = size_pos + size_other
        if total <= 0:
            continue
        minor = min(size_pos, size_other)
        if minor / total >= _SYMBOLIC_THRESHOLD:
            return True

    return False


# ── Fetch posiciones ──────────────────────────────────────────────────────────

def fetch_open_positions(wallet: str, min_size: float) -> list:
    """Todas las posiciones abiertas válidas (sin cap de 10 — el ranking va después)."""
    try:
        r = requests.get(DATA_URL + "/positions?user=" + wallet
                         + "&limit=500", timeout=20)
        if r.status_code != 200: return []
        raw = r.json()
        if not isinstance(raw, list): return []
    except Exception as e:
        print(f"[TRUSTED] Error fetch {wallet[:10]}: {e}"); return []

    now, result = datetime.now(timezone.utc), []
    for p in raw:
        if bool(p.get("redeemable", False)): continue
        size = float(p.get("initialValue", p.get("size", 0)) or 0)
        if size < min_size: continue
        cur  = float(p.get("curPrice", p.get("avgPrice", 0.5)) or 0.5)
        if cur <= 0.04 or cur >= 0.96: continue
        end_raw = p.get("endDate") or p.get("endDateIso") or ""
        if end_raw:
            try:
                end_dt = datetime.fromisoformat(str(end_raw).replace("Z","+00:00"))
                if end_dt.tzinfo is None: end_dt = end_dt.replace(tzinfo=timezone.utc)
                if end_dt <= now: continue
            except: pass
        if float(p.get("cashPnl", 0) or 0) < 0: continue
        result.append(p)
    return result


# ── Cross-reference ───────────────────────────────────────────────────────────

def build_cross_index(all_positions: dict) -> dict:
    """
    all_positions: {wallet: [pos, ...]}
    Devuelve: {conditionId: [{wallet, trader_info, pos, outcome}, ...]}
    """
    index = defaultdict(list)
    for wallet, positions in all_positions.items():
        trader = TRUSTED_TRADERS[wallet]
        for pos in positions:
            index[_cid(pos)].append({
                "wallet":  wallet,
                "trader":  trader,
                "pos":     pos,
                "outcome": pos.get("outcome", ""),
            })
    return index

def classify_cross(entries: list) -> str:
    """'consensus' | 'conflict' | 'single'"""
    if len(entries) < 2:
        return "single"
    # Ignorar conflictos de un trader consigo mismo (hedging interno)
    wallets = [e["wallet"] for e in entries]
    if len(set(wallets)) == 1:
        return "single"
    outcomes = [e["outcome"] for e in entries]
    return "consensus" if len(set(outcomes)) == 1 else "conflict"


# ── Motor probabilístico bayesiano ────────────────────────────────────────────

def _load_trader_stats(wallet: str) -> dict:
    """
    Carga win_rate, total_trades y profit_factor desde la BD.
    Devuelve defaults conservadores si no hay datos.
    """
    try:
        conn = database.get_connection()
        c    = conn.cursor()
        c.execute("""
            SELECT win_rate, total_trades, profit_factor, insider_score
            FROM wallets WHERE address = ?
        """, (wallet,))
        row = conn.execute(
            "SELECT win_rate, total_trades, profit_factor, insider_score FROM wallets WHERE address=?",
            (wallet,)
        ).fetchone()
        conn.close()
        if row:
            return {
                "win_rate":      float(row[0] or 0.5),
                "total_trades":  int(row[1]   or 10),
                "profit_factor": float(row[2] or 1.0),
                "insider_score": float(row[3] or 0.5),
            }
    except Exception:
        pass
    return {"win_rate": 0.5, "total_trades": 10, "profit_factor": 1.0, "insider_score": 0.5}

def _effective_wr(win_rate: float, total_trades: int, profit_factor: float) -> float:
    """
    Win rate ajustado por:
      1. Laplace smoothing: 10 trades virtuales al 50% para estabilizar muestras pequeñas.
         Un trader con 10 trades al 100% no debería ser tan fiable como uno con 100 al 90%.
      2. Profit factor: PF>2 indica que las ganancias son sistemáticamente mayores que
         las pérdidas — boost moderado de hasta +5pp. PF<1 penaliza.

    Resultado acotado en [0.52, 0.96] para que nunca invalide el cálculo bayesiano.
    """
    # 1. Laplace smoothing (10 priors virtuales al 50%)
    smoothed = (win_rate * total_trades + 10 * 0.5) / (total_trades + 10)

    # 2. Ajuste por profit factor
    #    PF=1 → neutro (×1.0)  |  PF=3 → +5pp  |  PF<1 → malus
    pf_boost = (min(profit_factor, 5.0) - 1.0) / (5.0 - 1.0) * 0.05  # max +0.05
    adjusted = smoothed + pf_boost

    return max(0.52, min(0.96, adjusted))

def bayesian_consensus_prob(market_price: float, entries: list) -> dict:
    """
    Actualiza la probabilidad implícita del mercado con el historial de
    cada trader que coincide en el mismo lado, usando el teorema de Bayes.

    Fórmula para N traders independientes:
        P(win | todos apuestan) =
            P(win) × ∏ WR_eff_i
            ─────────────────────────────────────────────────────
            P(win) × ∏ WR_eff_i + P(lose) × ∏ (1 - WR_eff_i)

    donde WR_eff_i es el win rate ajustado por sample size y profit factor.

    Devuelve dict con:
      combined_prob   → probabilidad final [0, 1]
      market_prob     → precio actual del mercado
      lift_pp         → mejora en puntos porcentuales sobre el mercado
      confidence      → 'ALTA' / 'MEDIA' / 'BAJA'
      trader_details  → lista de stats por trader
    """
    p_win  = market_price
    p_lose = 1.0 - market_price

    trader_details   = []
    total_trades_sum = 0

    for entry in entries:
        wallet = entry["wallet"]
        stats  = _load_trader_stats(wallet)
        wr_eff = _effective_wr(
            stats["win_rate"],
            stats["total_trades"],
            stats["profit_factor"]
        )
        trader_details.append({
            "username":      entry["trader"]["username"],
            "emoji":         entry["trader"]["emoji"],
            "win_rate_raw":  stats["win_rate"],
            "win_rate_eff":  wr_eff,
            "total_trades":  stats["total_trades"],
            "profit_factor": stats["profit_factor"],
            "insider_score": stats["insider_score"],
        })
        total_trades_sum += stats["total_trades"]
        p_win  *= wr_eff
        p_lose *= (1.0 - wr_eff)

    total = p_win + p_lose
    combined = (p_win / total) if total > 0 else market_price
    lift_pp  = round((combined - market_price) * 100, 1)

    # Confianza basada en trades combinados
    if total_trades_sum >= 60:
        confidence = "ALTA"
    elif total_trades_sum >= 25:
        confidence = "MEDIA"
    else:
        confidence = "BAJA"

    return {
        "combined_prob":  round(combined, 4),
        "market_prob":    round(market_price, 4),
        "lift_pp":        lift_pp,
        "confidence":     confidence,
        "total_trades":   total_trades_sum,
        "trader_details": trader_details,
    }


# ── Mensajes Telegram ─────────────────────────────────────────────────────────

def _prob_bar(cur_price: float) -> str:
    blocks = round(cur_price * 5)
    return "🟩" * blocks + "⬜" * (5 - blocks)

def _days_str(end_raw: str) -> tuple:
    if not end_raw: return "", 999
    try:
        end_dt = datetime.fromisoformat(str(end_raw).replace("Z","+00:00"))
        if end_dt.tzinfo is None: end_dt = end_dt.replace(tzinfo=timezone.utc)
        days = max(0, (end_dt - datetime.now(timezone.utc)).days)
        return end_dt.strftime("%d/%m/%Y"), days
    except: return "", 999


async def send_ranking_summary(trader: dict, ranked: list, total_pos: int):
    """Tabla compacta del top N antes de los mensajes individuales."""
    lines = [
        f"{trader['emoji']} {trader['username'].upper()} — TOP {len(ranked)} posiciones",
        f"📊 Ranking: profit × prob  ({total_pos} abiertas en total)",
        "─" * 36,
    ]
    for i, pos in enumerate(ranked, 1):
        title     = pos.get("title","?")[:46]
        outcome   = pos.get("outcome","?")
        cur       = float(pos.get("curPrice", pos.get("avgPrice",0.5)) or 0.5)
        pnl       = float(pos.get("cashPnl",0) or 0)
        pct       = float(pos.get("percentPnl",0) or 0)
        _, days   = _days_str(pos.get("endDate","") or pos.get("endDateIso",""))
        days_tag  = f" · {days}d" if days < 999 else ""
        bar       = _prob_bar(cur)
        lines.append(
            f"\n#{i}  {bar} @{round(cur,2)}  +{round(pnl):,} USDC (+{round(pct)}%){days_tag}"
            f"\n     {outcome} → {title}"
        )

    msg    = "\n".join(lines)
    chunks = [msg[i:i+4000] for i in range(0, len(msg), 4000)]
    try:
        for chunk in chunks:
            await telegram_bot.send_with_retry(telegram_bot.bot.send_message(chat_id=config.TELEGRAM_CHAT_ID, text=chunk))
            if len(chunks) > 1: await asyncio.sleep(1)
    except Exception as e:
        print(f"[TRUSTED] Error resumen: {e}")


async def send_consensus_alert(entries: list, trade_id: int):
    """
    CONSENSO: ≥2 traders en el mismo lado.
    Incluye probabilidad bayesiana combinada.
    """
    pos       = entries[0]["pos"]
    title     = pos.get("title", "Mercado desconocido")
    outcome   = entries[0]["outcome"]
    cur_price = float(pos.get("curPrice", pos.get("avgPrice", 0.5)) or 0.5)
    niche     = _get_niche(title)
    cat_emoji = _NICHE_EMOJI.get(niche, "📈")
    nl        = _NICHE_LABEL.get(niche, "ECONOMÍA")
    fecha, days = _days_str(pos.get("endDate", "") or pos.get("endDateIso", ""))

    try:
        retorno_mercado = round((1 / cur_price) - 1, 2)
    except Exception:
        retorno_mercado = 0

    n    = len(entries)
    star = "🔥🔥🔥" if n >= 3 else "⚡⚡"

    # ── Cálculo bayesiano ──────────────────────────────────────────────────────
    bayes = bayesian_consensus_prob(cur_price, entries)
    combined_pct  = round(bayes["combined_prob"] * 100, 1)
    market_pct    = round(bayes["market_prob"]   * 100, 1)
    lift          = bayes["lift_pp"]
    conf_label    = bayes["confidence"]
    total_trades  = bayes["total_trades"]

    lift_sign  = f"+{lift}pp" if lift >= 0 else f"{lift}pp"
    conf_emoji = {"ALTA": "🟢", "MEDIA": "🟡", "BAJA": "🔴"}.get(conf_label, "⚪")

    try:
        retorno_bayes = round((1 / bayes["combined_prob"]) - 1, 2)
    except Exception:
        retorno_bayes = retorno_mercado

    # ── Construcción del mensaje ───────────────────────────────────────────────
    lines = [
        f"{star} CONSENSO — {n} TRADERS CONFIRMAN",
        f"{cat_emoji} {nl}",
        "",
        f"📋 {title[:72]}",
        f"🎯 Todos apuestan: {outcome}",
        "",
        # Bloque de probabilidad combinada — el corazón del mensaje
        f"┌─ PROBABILIDAD COMBINADA ──────────────────",
        f"│  Mercado:   {_prob_bar(cur_price)} {market_pct}%   (+{retorno_mercado}x)",
        f"│  Bayesiana: {_prob_bar(bayes['combined_prob'])} {combined_pct}%   (+{retorno_bayes}x)",
        f"│  Señal:     {lift_sign} sobre el mercado",
        f"│  Confianza: {conf_emoji} {conf_label}  ({total_trades} trades combinados)",
        f"└───────────────────────────────────────────",
        "",
        "👥 Traders confirmando:",
    ]

    total_pnl = 0
    for d in bayes["trader_details"]:
        pf_str = f"PF={round(d['profit_factor'],1)}" if d["profit_factor"] > 0 else "PF=n/a"
        lines.append(
            f"   {d['emoji']} {d['username']:<14} "
            f"WR={round(d['win_rate_raw']*100)}%  "
            f"{pf_str}  "
            f"{d['total_trades']} trades  "
            f"score={round(d['insider_score'],2)}"
        )

    # PnL latente por trader
    lines.append("")
    lines.append("💸 Dinero en juego:")
    for e in entries:
        t   = e["trader"]
        p   = e["pos"]
        avg = float(p.get("avgPrice", 0.5) or 0.5)
        pnl = float(p.get("cashPnl", 0) or 0)
        sz  = float(p.get("initialValue", p.get("size", 0)) or 0)
        total_pnl += pnl
        lines.append(
            f"   {t['emoji']} {t['username']}  "
            f"entrada @{round(avg,3)}  "
            f"invertido {round(sz):,} USDC  "
            f"PnL latente +{round(pnl):,} USDC"
        )

    lines += [
        "",
        f"💰 PnL latente combinado: +{round(total_pnl):,} USDC",
    ]
    if fecha:
        lines.append(f"🗓 Resuelve: {fecha}  ({days}d)")

    # ── Evaluación IA (consenso = boost de confianza) ────────────────────────
    consensus_eval_block = ""
    consensus_score = 10   # default: consenso ya es señal fuerte
    try:
        best_wallet = max(entries, key=lambda e: _score(e["pos"]))["wallet"]
        trader_stats = database.get_trader_full_stats(best_wallet)
        similar      = database.get_similar_trades_history(best_wallet, _get_niche(title) or "other")
        evaluation   = await evaluate_trade(
            market_data   = {"question": title},
            trade_data    = {"side": outcome, "price": cur_price, "retorno": retorno_mercado,
                             "days_left": days, "niche": _get_niche(title) or "other",
                             "size": float(entries[0]["pos"].get("initialValue", 0) or 0)},
            trader_stats  = trader_stats,
            similar_history = similar,
            trade_id      = trade_id,
        )
        consensus_score = evaluation.get("confidence", 10)
        # Para CONSENSO el umbral es más bajo (≥4 en vez de 5)
        if consensus_score < 4:
            print(f"[EVAL] ⛔ CONSENSO SKIP — confidence {consensus_score}/10")
            return
        consensus_eval_block = format_evaluation_for_telegram(evaluation, config.MAX_TRADE_SIZE_USDC)
        print(f"[EVAL] CONSENSO score {consensus_score}/10 · {evaluation.get('verdict')}")
    except Exception as e:
        print(f"[EVAL] Error evaluación consenso: {e}")

    if consensus_eval_block:
        lines.append(consensus_eval_block)

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📋 Registrar", callback_data=f"register_{trade_id}"),
        InlineKeyboardButton("❌ Ignorar",   callback_data=f"ignore_{trade_id}"),
    ]])

    print(f"[TRUSTED] 📊 Bayes: mercado={market_pct}% → combinada={combined_pct}% "
          f"({lift_sign}) confianza={conf_label}")

    await telegram_bot.send_with_retry(telegram_bot.bot.send_message(
        chat_id=config.TELEGRAM_CHAT_ID,
        text="\n".join(lines),
        reply_markup=keyboard,
    ))


async def send_conflict_alert(entries: list):
    """CONFLICTO: traders en lados opuestos del mismo mercado."""
    pos   = entries[0]["pos"]
    title = pos.get("title","Mercado desconocido")
    niche = _get_niche(title)
    cat_emoji = _NICHE_EMOJI.get(niche,"📈")
    nl        = _NICHE_LABEL.get(niche,"ECONOMÍA")
    fecha, days = _days_str(pos.get("endDate","") or pos.get("endDateIso",""))

    lines = [
        f"⚠️ CONFLICTO ENTRE TRADERS",
        f"{cat_emoji} {nl}",
        "",
        f"📋 {title[:72]}",
        "",
        "👥 Posiciones opuestas:",
    ]
    for e in entries:
        t   = e["trader"]
        p   = e["pos"]
        avg = float(p.get("avgPrice",0.5) or 0.5)
        cur = float(p.get("curPrice", avg) or avg)
        pnl = float(p.get("cashPnl",0) or 0)
        lines.append(f"   {t['emoji']} {t['username']}  → {e['outcome']}  @{round(avg,3)}→@{round(cur,3)}  pnl={round(pnl):+,} USDC")

    if fecha:
        lines.append(f"\n🗓 Resuelve: {fecha}  ({days}d)")
    lines.append("\n🤔 Señales opuestas — analizar antes de copiar")

    await telegram_bot.send_with_retry(telegram_bot.bot.send_message(
        chat_id=config.TELEGRAM_CHAT_ID, text="\n".join(lines)
    ))


async def send_position_alert(rank: int, wallet: str, trader: dict, pos: dict, trade_id: int):
    """Alerta individual con botón Copiar."""
    title     = pos.get("title","Mercado desconocido")
    outcome   = pos.get("outcome","?")
    avg_price = float(pos.get("avgPrice",0.5) or 0.5)
    cur_price = float(pos.get("curPrice",avg_price) or avg_price)
    size      = float(pos.get("initialValue", pos.get("size",0)) or 0)
    cash_pnl  = float(pos.get("cashPnl",0) or 0)
    pct_pnl   = float(pos.get("percentPnl",0) or 0)
    fecha, days = _days_str(pos.get("endDate","") or pos.get("endDateIso",""))

    try: retorno = round((1/cur_price)-1, 2)
    except: retorno = 0

    niche       = _get_niche(title)
    cat_emoji   = _NICHE_EMOJI.get(niche,"📈")
    niche_label = _NICHE_LABEL.get(niche,"ECONOMÍA")
    arrow       = "📈" if cur_price > avg_price else "📉"

    lines = [
        f"{trader['emoji']} #{rank} — {trader['username']}  |  {cat_emoji} {niche_label}",
        "",
        f"📋 {title[:72]}",
        f"🎯 {outcome}  |  {_prob_bar(cur_price)} {round(cur_price*100)}%",
        f"   Entrada @{round(avg_price,3)}  →  Ahora @{round(cur_price,3)} {arrow}",
        "",
        f"💰 Invertido: {round(size):,} USDC",
        f"📊 PnL latente: +{round(cash_pnl):,} USDC  (+{round(pct_pnl)}%)",
        f"↩️  Retorno desde ahora: +{retorno}x",
    ]
    if fecha:
        lines.append(f"🗓 Resuelve: {fecha}  ({days}d)")
    lines += ["", f"👤 {trader['description']}"]

    # ── Evaluación IA antes de alertar ───────────────────────────────────────
    print(f"[EVAL] Evaluando trade de {trader['username']}...")
    try:
        trader_stats = database.get_trader_full_stats(wallet)
        similar      = database.get_similar_trades_history(wallet, niche or "other")
        evaluation   = await evaluate_trade(
            market_data   = {"question": title},
            trade_data    = {"side": outcome, "price": cur_price, "retorno": retorno,
                             "days_left": days, "niche": niche or "other", "size": size},
            trader_stats  = trader_stats,
            similar_history = similar,
            trade_id      = trade_id,
        )
        score = evaluation.get("confidence", 5)
        print(f"[EVAL] Score {score}/10 · {evaluation.get('verdict')} · {trader['username']}: {title[:40]}")

        if score < MIN_CONFIDENCE:
            print(f"[EVAL] ⛔ SKIP — confidence {score} < {MIN_CONFIDENCE} (no alerta)")
            return

        eval_block = format_evaluation_for_telegram(evaluation, config.MAX_TRADE_SIZE_USDC)
        lines.append(eval_block)

    except Exception as e:
        print(f"[EVAL] Error evaluación: {e} — enviando alerta estándar")

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📋 Registrar", callback_data=f"register_{trade_id}"),
        InlineKeyboardButton("❌ Ignorar",   callback_data=f"ignore_{trade_id}"),
    ]])
    await telegram_bot.send_with_retry(telegram_bot.bot.send_message(
        chat_id=config.TELEGRAM_CHAT_ID, text="\n".join(lines), reply_markup=keyboard
    ))
    print(f"[EVAL] ✅ Alerta enviada (confidence: {evaluation.get('confidence', '?')}/10)")


# ── Loop principal ────────────────────────────────────────────────────────────

async def trusted_scan_loop():
    _init_table()
    print("[TRUSTED] 👁  Scanner trusted iniciado (cross-ref · top 10/trader · cada 20 min)")

    while True:
        # ── FASE 0: Log traders en observación (sin alertas) ─────────────────
        for wallet, trader in WATCH_TRADERS.items():
            wr = trader.get("real_wr")
            if wr is not None:
                positions = fetch_open_positions(wallet, trader["min_size"])
                if positions:
                    best = sorted(positions, key=_score, reverse=True)[:3]
                    tops = " | ".join(p.get("title","")[:30] for p in best)
                    print(f"[WATCH] {trader['emoji']} {trader['username']} (WR {round(wr*100,1)}% — sin alertas): {len(positions)} abiertas → {tops}")
                else:
                    print(f"[WATCH] {trader['emoji']} {trader['username']}: sin posiciones abiertas")

        # ── FASE 0b: Log hot streak traders en observación ───────────────────
        try:
            hot_watch = database.get_active_hot_streak_traders()
            if hot_watch:
                for ht in hot_watch:
                    w = ht["wallet"]
                    # Obtener pocas posiciones solo para log (no alerta completa)
                    try:
                        r = requests.get(DATA_URL + "/positions?user=" + w + "&limit=10", timeout=10)
                        n_open = len([p for p in (r.json() if r.status_code == 200 else [])
                                      if not bool(p.get("redeemable", False))]) if r.status_code == 200 else "?"
                    except Exception:
                        n_open = "?"
                    expires_h = ""
                    try:
                        from datetime import datetime as _dt, timezone as _tz
                        exp = _dt.fromisoformat(ht["expires_at"])
                        if exp.tzinfo is None:
                            exp = exp.replace(tzinfo=_tz.utc)
                        hours_left = max(0, (exp - _dt.now(_tz.utc)).total_seconds() / 3600)
                        expires_h = f" · expira en {int(hours_left)}h"
                    except Exception:
                        pass
                    print(f"[HOT-WATCH] 🔥 {ht['username'][:20]} "
                          f"L10:{round(ht['wr10']*100)}% L5:{round(ht['wr5']*100)}% "
                          f"hist:{round(ht['hist_wr']*100)}% "
                          f"PnL:${ht['total_pnl']:,.0f} | {n_open} abiertas{expires_h}")
        except Exception as e:
            print(f"[HOT-WATCH] Error log: {e}")

        # ── FASE 1: Fetch posiciones de traders ACTIVOS (WR ≥ 75%) ───────────
        all_positions = {}
        for wallet, trader in TRUSTED_TRADERS.items():
            wr = trader.get("real_wr")
            # Skip si WR conocida < MIN_REAL_WR
            if wr is not None and wr < MIN_REAL_WR:
                print(f"[TRUSTED] {trader['emoji']} {trader['username']}: skip — WR real {round(wr*100,1)}% < {round(MIN_REAL_WR*100)}%")
                all_positions[wallet] = []
                continue
            positions = fetch_open_positions(wallet, trader["min_size"])
            all_positions[wallet] = positions
            print(f"[TRUSTED] {trader['emoji']} {trader['username']}: {len(positions)} posiciones abiertas")

        # ── FASE 2: Cross-reference (consensos y conflictos) ──────────────────
        index = build_cross_index(all_positions)

        consensus_cids  = set()
        conflict_cids   = set()

        for cid, entries in index.items():
            if len(entries) < 2:
                continue
            ctype = classify_cross(entries)

            if ctype == "consensus" and not _is_cross_alerted(cid, "consensus"):
                # Filtrar si alguno de los traders tiene cobertura en este mercado
                hedged = any(
                    _is_hedged(e["pos"], all_positions.get(e["wallet"], []))
                    for e in entries
                )
                if hedged:
                    print(f"[TRUSTED] Filtrado [COBERTURA] consenso: trader apostó a ambos lados en {entries[0]['pos'].get('title','')[:40]}")
                    continue
                # Calcular trade_id para el botón Copiar usando la posición de mayor score
                best = max(entries, key=lambda e: _score(e["pos"]))
                bp   = best["pos"]
                cur  = float(bp.get("curPrice", bp.get("avgPrice",0.5)) or 0.5)
                end_raw = bp.get("endDate","") or bp.get("endDateIso","")
                _, dl = _days_str(end_raw)
                ret  = round((1/cur)-1, 2) if cur > 0 else 0
                td   = database.save_detected_trade({
                    "wallet":     best["wallet"],
                    "market_id":  cid,
                    "question":   bp.get("title",""),
                    "side":       best["outcome"],
                    "size":       float(bp.get("initialValue", bp.get("size",0)) or 0),
                    "price":      cur,
                    "confidence": min(0.70 + len(entries)*0.10, 0.95),
                    "research":   f"CONSENSO {len(entries)} traders: " +
                                  ", ".join(e['trader']['username'] for e in entries),
                    "days_left":  dl,
                    "hours_left": None,
                    "retorno":    ret,
                })
                _mark_cross_alerted(cid, "consensus")
                consensus_cids.add(cid)
                traders_str = " + ".join(e['trader']['emoji']+e['trader']['username'] for e in entries)
                print(f"[TRUSTED] ⚡ CONSENSO: {traders_str} → {bp.get('title','')[:45]}")
                await send_consensus_alert(entries, td)
                await asyncio.sleep(2)

            elif ctype == "conflict" and not _is_cross_alerted(cid, "conflict"):
                _mark_cross_alerted(cid, "conflict")
                conflict_cids.add(cid)
                traders_str = " vs ".join(f"{e['trader']['emoji']}{e['trader']['username']}({e['outcome']})" for e in entries)
                print(f"[TRUSTED] ⚠️  CONFLICTO: {traders_str} → {entries[0]['pos'].get('title','')[:40]}")
                await send_conflict_alert(entries)
                await asyncio.sleep(2)

        # ── FASE 3: Alertas individuales top 10 por trader ────────────────────
        for wallet, trader in TRUSTED_TRADERS.items():
            positions = all_positions[wallet]

            # Filtrar ya alertadas, participaron en consenso, o son cobertura
            new_pos = []
            for p in positions:
                if _is_alerted(wallet, _cid(p), p.get("outcome","")):
                    continue
                if _cid(p) in consensus_cids:
                    continue
                if _is_hedged(p, positions):
                    print(f"[TRUSTED] Filtrado [COBERTURA]: {trader['username']} apostó a ambos lados en {p.get('title','')[:40]}")
                    continue
                new_pos.append(p)

            if not new_pos:
                print(f"[TRUSTED] {trader['emoji']} {trader['username']}: sin posiciones nuevas individuales")
                continue

            ranked = sorted(new_pos, key=_score, reverse=True)[:TOP_PER_TRADER]
            print(f"[TRUSTED] {trader['emoji']} {trader['username']}: {len(new_pos)} nuevas → top {len(ranked)}")

            await send_ranking_summary(trader, ranked, len(positions))
            await asyncio.sleep(2)

            for rank, pos in enumerate(ranked, 1):
                cid_val = _cid(pos)
                outcome = pos.get("outcome","")
                cur     = float(pos.get("curPrice", pos.get("avgPrice",0.5)) or 0.5)
                _, dl   = _days_str(pos.get("endDate","") or pos.get("endDateIso",""))
                ret     = round((1/cur)-1,2) if cur > 0 else 0

                td = database.save_detected_trade({
                    "wallet":     wallet,
                    "market_id":  cid_val,
                    "question":   pos.get("title",""),
                    "side":       outcome,
                    "size":       float(pos.get("initialValue", pos.get("size",0)) or 0),
                    "price":      cur,
                    "confidence": min(0.60 + cur*0.35, 0.95),
                    "research":   f"Top {rank} posición abierta de {trader['username']} "
                                  f"(score={round(_score(pos)):,})",
                    "days_left":  dl,
                    "hours_left": None,
                    "retorno":    ret,
                })
                _mark_alerted(wallet, cid_val, outcome)

                print(f"[TRUSTED]   #{rank} {pos.get('title','')[:44]} | {outcome} | score={round(_score(pos)):,}")
                await send_position_alert(rank, wallet, trader, pos, td)
                await asyncio.sleep(2)

        print(f"[TRUSTED] Ciclo completo — consensos={len(consensus_cids)} conflictos={len(conflict_cids)}")
        await asyncio.sleep(SCAN_INTERVAL)
