import asyncio
import requests
from datetime import datetime
import config
import database
import executor
import telegram_bot
from leaderboard_analyzer import NICHE_KEYWORDS, get_filters_for_niche
# L81 (2026-06-10): research_agent eliminado — Claude research desactivado desde
# L30 (2026-05-31). El import añadía latencia de arranque y era dead code.
from probability_engine import daily_performance
# L31: behavior_analyzer no se importa — el filtro behavior se eliminó.

DATA_URL = "https://data-api.polymarket.com"
# L81 FIX: seen_trades como deque con cap en lugar de set sin límite.
# Set puro crece sin fin en run 24/7 (semanas → cientos de miles de IDs →
# memoria creciente). Cap de 50k entradas es holgado para dedup (Swisstony/Lily
# hacen ~200 ops/día → 50k cubre >250 días de IDs únicos) y acota la RAM.
from collections import deque as _deque
_seen_trades_deque: _deque = _deque(maxlen=50_000)
seen_trades: set = set()  # mantenemos el set para lookup O(1); deque gestiona el cap

def _seen_add(tid):
    """Añade un trade_id al set+deque. Si el deque ya estaba lleno, expulsa
    el más antiguo del set también para mantener coherencia."""
    if len(_seen_trades_deque) == _seen_trades_deque.maxlen:
        oldest = _seen_trades_deque[0]  # se expulsará con append
        seen_trades.discard(oldest)
    _seen_trades_deque.append(tid)
    seen_trades.add(tid)

end_date_cache = {}
# L81: claude_semaphore eliminado — research desactivado desde L30, nunca se usó.
# L81: _research_ram eliminado — nunca se escribía (research es un stub vacío).

# ── Cache de deduplicación y cobertura ───────────────────────────────────────
# Clave: wallet + "|" + market_id
# Valor: {"first_ts": datetime, "sides": {"BUY": size, "SELL": size}, "alerted": bool}
market_trade_cache = {}
DEDUP_MINUTES  = 30     # ventana de acumulación
HEDGE_WINDOW_H = 2      # ventana para detectar cobertura (horas)
HEDGE_SYMBOLIC = 0.01   # apuesta < 1% del total → ignorar (simbólica)

# L31: el reload periódico de _user_prefs se eliminó (la función ya no las usa).

PRIORITY_KEYWORDS = [
    "Iran", "Russia", "Ukraine", "Trump", "China", "Israel", "Gaza",
    "NATO", "nuclear", "ceasefire", "war", "sanctions", "coup",
    "election", "president", "congress", "Fed", "OPEC", "military"
]

CRYPTO_SHORT_KEYWORDS = ["Bitcoin Up or Down", "BTC", "ETH", "crypto"]

def is_crypto_short_term(question, end_date):
    if not any(kw.lower() in question.lower() for kw in CRYPTO_SHORT_KEYWORDS):
        return False
    if end_date:
        try:
            from datetime import timezone as _tz
            dt = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_tz.utc)
            diff = dt - datetime.now(_tz.utc)
            return diff.total_seconds() < 86400
        except:
            pass
    return False

import re as _re

_POLITICAL_TERMS_VS = {
    "trump", "biden", "russia", "ukraine", "china", "iran", "israel",
    "congress", "senate", "election", "president", "minister", "government",
    "war", "ceasefire", "nato", "nuclear", "sanctions", "putin", "xi",
}

def _kw_in_question(kw_lower, question_lower):
    """
    Comprueba si una keyword aparece en la pregunta.
    - Palabras puras (solo \w): exige word boundaries → evita "inter"→"interest"
    - Con espacios o caracteres especiales: substring simple
    """
    if _re.match(r'^\w+$', kw_lower):
        return bool(_re.search(r'\b' + kw_lower + r'\b', question_lower))
    return kw_lower in question_lower

# Niches descartados antes de cualquier procesamiento (2026-05-25 user
# decision). Una señal con niche en este set NO se analiza, NO se almacena
# en detected_trades, NO genera paper, NO genera real, NO se considera para
# calificar al trader que la emitió.
# crypto añadido 2026-05-25: 0 trades históricos en cualquier capa,
# nicho operacionalmente irrelevante.
IGNORED_NICHES = {"politics", "geopolitics", "entertainment", "crypto"}


def get_question_niche(question):
    q = question.lower()
    # Orden garantizado por el dict: esports → sports → crypto → geopolitics → politics
    for niche, keywords in NICHE_KEYWORDS.items():
        if any(_kw_in_question(kw.lower(), q) for kw in keywords):
            return niche
    # Regla "vs": dos palabras con mayúscula separadas por "vs" → DEPORTES
    # Excepción: contexto político/geopolítico claro.
    # 2026-05-23: usar word boundaries — "iran" substring matcheaba dentro de
    # "Tirante" (apellido tenista) y excluía erróneamente trades sports.
    if _re.search(r'[A-Z][A-Za-zÀ-ÿ]* vs\.? [A-Z]', question):
        political_hit = any(_re.search(r'\b' + _re.escape(term) + r'\b', q)
                            for term in _POLITICAL_TERMS_VS)
        if not political_hit:
            return "sports"
    # L66 (2026-06-04) — capturar "Will X win on YYYY-MM-DD" y "Will X win the [Y]"
    # como sports. Lily tiene 44% de su volumen en este formato (Copa África,
    # ligas europeas, etc.) que se perdían como niche=None → fail-closed.
    if _re.search(r'\bwill\s+.+\s+win\s+(on|the)\b', q):
        political_hit = any(_re.search(r'\b' + _re.escape(term) + r'\b', q)
                            for term in _POLITICAL_TERMS_VS)
        if not political_hit:
            return "sports"
    # Mercados "Spread:", "O/U", "Match O/U" sin "vs" claro también son sports
    if _re.search(r'\b(spread:|o/u|over/under)\b', q):
        return "sports"
    return None

def compute_days_left(end_date):
    if not end_date:
        return 999
    try:
        from datetime import timezone as _tz
        dt = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz.utc)
        diff = dt - datetime.now(_tz.utc)
        return max(0, int(diff.total_seconds() // 86400))
    except:
        return 999

def passes_user_preferences(niche, retorno, days_left, insider_score, entry_price=None):
    """
    L31 (2026-05-31) — Simplificado.

    La parte estática (`preferences.json`) y los umbrales por nicho aprendidos
    de clicks YES/NO se han eliminado: para un sistema que copia ballenas,
    el criterio del usuario sesga el edge del trader subyacente. Sample de
    feedback además era insuficiente (15 totales) para tener señal real.

    Mantiene SOLO los filtros data-driven:
      · IGNORED_NICHES (decisión histórica del usuario sobre nichos enteros)
      · config.EXCLUDED_NICHES (output del auto-tuner — basado en PnL real)
      · config.EXCLUDED_ENTRY_ABOVE (output del auto-tuner)
    """
    if niche in IGNORED_NICHES:
        return False, f"NICHE_IGNORED ({niche})"
    excluded = getattr(config, "EXCLUDED_NICHES", []) or []
    if niche and niche in excluded:
        return False, f"AUTOTUNER_NICHE_EXCLUDED ({niche})"
    entry_cap = getattr(config, "EXCLUDED_ENTRY_ABOVE", None)
    if entry_cap is not None and entry_price is not None and entry_price >= entry_cap:
        return False, f"AUTOTUNER_ENTRY_TOO_HIGH ({entry_price:.3f}>={entry_cap})"
    return True, ""

# L31 (2026-05-31): `passes_behavior_filter` eliminada. Filtraba trades
# basándose en clicks YES/NO del usuario en alertas — sesgo subjetivo
# contraproducente para un sistema que copia traders objetivamente mejores.
# El bot ya aprende de outcomes reales vía whale_tracker / leaderboard_analyzer.

def get_recent_activity(wallet, limit=5, offset=0):
    """
    Devuelve los últimos N TRADES del wallet (BUYs/SELLs).
    Fix L27 (2026-05-29): pedimos limit*8 a la API y filtramos por type=TRADE.
    L92 (2026-06-07): añadido offset para paginación. process_wallet pagina
    cuando la primera página no cubre los trades desde last_ts.
    """
    try:
        api_limit = max(limit * 8, 30)
        url = (f"{DATA_URL}/activity?user={wallet}"
               f"&limit={api_limit}&offset={offset}")
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return []
        data = r.json() or []
        trades = [a for a in data if a.get("type") == "TRADE"]
        return trades[:limit]
    except Exception as e:
        print(f"Error {wallet[:8]}: {e}")
        return []

def get_market_end_date(slug):
    if slug in end_date_cache:
        return end_date_cache[slug]
    try:
        r = requests.get("https://gamma-api.polymarket.com/markets?slug=" + slug, timeout=5)
        data = r.json()
        if data and isinstance(data, list) and len(data) > 0:
            end_date = data[0].get("endDate", None)
            end_date_cache[slug] = end_date
            return end_date
    except:
        pass
    return None

def get_trade_id(trade):
    tx = trade.get("transactionHash") or trade.get("id") or ""
    asset = trade.get("asset") or ""
    return tx + "|" + asset

def passes_hard_filters(trade, insider_stats, end_date=None):
    """
    Filtros duros pre-Claude. Devuelve (True, nivel) o (False, motivo).
    Todos los trades que pasen aquí se ejecutarán automáticamente.

    L29 (2026-05-31) — reajuste post-curaduría de whitelist:
    los filtros del trader (score/win_rate/total/profit_factor) se ELIMINAN
    de esta capa porque la whitelist (`REAL_WALLET_WHITELIST` / `REAL_D_WALLET_WHITELIST`)
    ya garantiza wallets de calidad. Re-vetar cada trade es redundante y
    rechaza señales válidas cuando insider_stats viene incompleto.
    También se elimina `retorno≥0.20` (era ≡ entry≤0.83) — el filtro de
    rango operativo vive ahora en el router por agente. NO se añade piso
    de entry para no perder posibles macro-outliers.
    Lo que queda: market resuelto, precio extremo, size mínimo, nicho.
    """
    size = float(trade.get("usdcSize", 0))
    score = insider_stats.get("insider_score", 0)
    price = float(trade.get("price", 0.5))
    question = trade.get("title", "")

    # Fecha ya pasada → mercado cerrado con certeza, sin importar el precio
    if end_date:
        try:
            from datetime import timezone as _tz
            _edt = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
            if _edt.tzinfo is None:
                _edt = _edt.replace(tzinfo=_tz.utc)
            if _edt <= datetime.now(_tz.utc):
                return False, "RESUELTO"
        except:
            pass

    # Precio near-0 o near-1 con fecha aún activa: mercado sin valor (retorno insignificante)
    # DEF_COPY wallets bypass: Swisstony opera "No" bets a 0.97-0.99 con edge probado
    _wallet_addr_hf = (trade.get("proxyWallet") or trade.get("user") or "").lower()
    _def_copy_wallets_hf = {
        '0x204f72f35326db932158cba6adff0b9a1da95e14',
        '0x84dbb7103982e3617704a2ed7d5b39691952aeeb',
    }
    if _wallet_addr_hf not in _def_copy_wallets_hf:
        if price <= 0.02 or price >= 0.98:
            return False, "SIN_VALOR"

    # L66 (2026-06-04) — min_size adaptativo.
    # L80 (2026-06-07) — DEF_COPY wallets pasan sin filtro de tamaño.
    # El sistema DEF_COPY tiene su propio umbral de $50 acumulado downstream
    # (paper_executor). Filtrar aquí destruiría señales que DEF_COPY necesita.
    wallet_addr = (trade.get("proxyWallet") or trade.get("user") or "").lower()

    # Wallets DEF_COPY: bypass completo del filtro de tamaño
    DEF_COPY_WALLETS = {
        '0x204f72f35326db932158cba6adff0b9a1da95e14',  # Swisstony
        '0x84dbb7103982e3617704a2ed7d5b39691952aeeb',  # Soarin22
        '0xc96e5287ab294ac0388c2ddb00180fc464cff1f9',  # wigglew
        '0xa6d9b55a6a3a54a9d50ca94c64f00af69c50fd2f',  # kinderSman
    }
    if wallet_addr in DEF_COPY_WALLETS:
        min_size = 1  # DEF_COPY paper_executor decide con su propio umbral $50

    else:
        # Sistema legacy B/D
        try:
            import os as _os
            from dotenv import load_dotenv as _ldenv
            _ldenv(dotenv_path=str(__file__).replace('monitor.py', '.env'), override=True)
            whitelist_addrs = set()
            for env in ("REAL_WALLET_WHITELIST", "REAL_D_WALLET_WHITELIST"):
                for a in (_os.getenv(env,"") or "").split(","):
                    if a.strip():
                        whitelist_addrs.add(a.strip().lower())
        except Exception:
            whitelist_addrs = set()
        if wallet_addr in whitelist_addrs:
            min_size = 5
        elif is_crypto_short_term(question, end_date):
            min_size = 50
        else:
            min_size = 100

    if size < min_size:
        return False, "SIZE_PEQUENO"
    # Detectar nicho del trade y descartar los IGNORED
    niche = get_question_niche(question)
    if niche in IGNORED_NICHES:
        return False, f"NICHE_IGNORED ({niche})"

    # L81: eliminado scan PRIORITY_KEYWORDS para nivel CRITICO/ALTO/MEDIO.
    # Era puramente cosmético (label de log sin efecto en routing). Además nunca
    # disparaba para señales sports/esports — politics ya filtrado por IGNORED_NICHES.
    return True, "PASS"

def init_last_timestamps(wallets):
    """
    Inicializa last_timestamps a NOW-1h, no al último trade visible.
    Permite recuperar trades de la última hora cuando el bot reinicia (puede
    reprocesar pocos por dedup pero NO pierde la ventana, que era el bug
    histórico — perdíamos ~30-60min en cada restart). L27 bis.
    """
    last_timestamps = {}
    # Fix C2 (2026-06-18): ventana 1h → 6h para recuperar señales de Swis
    # llegadas durante downtime. Señales antiguas duplicadas son inofensivas:
    # market_cache DEF dedup las filtra y paper_executor comprueba BD.
    six_hours_ago = int(datetime.now().timestamp()) - 6 * 3600
    for wallet_data in wallets:
        wallet = wallet_data[0]
        last_timestamps[wallet] = six_hours_ago
    print(f"Timestamps inicializados (NOW-6h): {len(last_timestamps)} wallets")
    return last_timestamps

def _cache_key(wallet, market_id):
    return wallet + "|" + market_id

def _normalize_side(side):
    """Normaliza BUY/YES → 'YES', SELL/NO → 'NO'."""
    return "YES" if side.upper() in ("BUY", "YES") else "NO"

def update_market_cache(wallet, market_id, side, size, question=""):
    """
    Acumula el trade en el cache.
    Devuelve (is_duplicate, total_size, hedge_status).
      is_duplicate  : True si ya se mandó alerta en los últimos DEDUP_MINUTES
      total_size    : tamaño acumulado en esta ventana
      hedge_status  : None | 'COBERTURA'
    """
    key      = _cache_key(wallet, market_id)
    now      = datetime.now()
    norm     = _normalize_side(side)
    entry    = market_trade_cache.get(key)

    if entry:
        age_min = (now - entry["first_ts"]).total_seconds() / 60
        if age_min <= DEDUP_MINUTES:
            # Dentro de la ventana: acumular
            entry["sides"][norm] = entry["sides"].get(norm, 0) + size
            entry["last_ts"] = now
        else:
            # Ventana expirada: resetear
            market_trade_cache[key] = {
                "first_ts": now, "last_ts": now,
                "sides": {norm: size}, "alerted": False,
                "question": question,
            }
    else:
        market_trade_cache[key] = {
            "first_ts": now, "last_ts": now,
            "sides": {norm: size}, "alerted": False,
            "question": question,
        }

    entry        = market_trade_cache[key]
    total_size   = sum(entry["sides"].values())
    is_duplicate = entry["alerted"]

    # Detectar cobertura solo dentro de la ventana de 2h
    hedge_status = None
    age_h = (now - entry["first_ts"]).total_seconds() / 3600
    if age_h <= HEDGE_WINDOW_H and len(entry["sides"]) == 2:
        yes_amt = entry["sides"].get("YES", 0)
        no_amt  = entry["sides"].get("NO",  0)
        minor   = min(yes_amt, no_amt)
        # Excepción: apuesta simbólica < 1% del total → no es cobertura real
        if total_size > 0 and minor / total_size >= HEDGE_SYMBOLIC:
            hedge_status = "COBERTURA"   # cualquier dinero real en ambos lados = cobertura

    return is_duplicate, total_size, hedge_status

def mark_alerted(wallet, market_id):
    key = _cache_key(wallet, market_id)
    if key in market_trade_cache:
        market_trade_cache[key]["alerted"] = True

# ── Ventana para detectar cobertura entre mercados separados (minutos) ─────────
MATCH_HEDGE_WINDOW_MIN = 60

def _extract_match_names(question: str):
    """
    Si el título contiene ' vs ' (case-insensitive) devuelve [nombre_A, nombre_B].
    Ejemplo: "Anna Blinkova vs Nuria Brancaccio" → ["Anna Blinkova", "Nuria Brancaccio"]
    Devuelve [] si no hay ' vs '.
    """
    import re as _re
    m = _re.split(r'\s+vs\.?\s+', question, maxsplit=1, flags=_re.IGNORECASE)
    if len(m) == 2:
        # Limpiar sufijos comunes de Polymarket ("O/U 6.5", "Spread...", etc.)
        a = _re.split(r'[:\(]', m[0])[0].strip()
        b = _re.split(r'[:\(]', m[1])[0].strip()
        if a and b:
            return [a, b]
    return []

def detect_match_hedge(wallet: str, question: str, side: str, size: float):
    """
    Detecta si este trade es cobertura de partido completo:
    el trader ya apostó al OTRO jugador/equipo del mismo partido
    en un mercado separado en los últimos MATCH_HEDGE_WINDOW_MIN minutos.

    Devuelve (hedge_type, dominant_question, dominant_side, dominant_size):
      hedge_type: None | 'COBERTURA_PARTIDO'
      dominant_*: datos del lado con mayor apuesta (para el mensaje)
    """
    names = _extract_match_names(question)
    if not names:
        return None, None, None, None

    name_a, name_b = names
    # El nombre del rival es el que NO aparece en la pregunta actual
    # (la pregunta actual incluye a ambos, así que buscamos por el que apostó antes)
    now = datetime.now()

    for key, entry in market_trade_cache.items():
        if not key.startswith(wallet + "|"):
            continue
        age_min = (now - entry["first_ts"]).total_seconds() / 60
        if age_min > MATCH_HEDGE_WINDOW_MIN:
            continue

        cached_q = entry.get("question", "")
        if not cached_q or cached_q == question:
            continue

        cached_names = _extract_match_names(cached_q)
        if not cached_names:
            continue

        # Mismo partido = los dos nombres del cached coinciden con los del actual
        # (independientemente del orden)
        if set(n.lower() for n in cached_names) == set(n.lower() for n in names):
            cached_size = sum(entry["sides"].values())
            total       = size + cached_size
            if total == 0:
                continue
            minor = min(size, cached_size)
            # Excepción: apuesta simbólica < 1% del total → no es cobertura real
            if minor / total < HEDGE_SYMBOLIC:
                continue
            # Cualquier dinero real en ambos lados del mismo partido = cobertura
            return "COBERTURA_PARTIDO", None, None, None

    return None, None, None, None

async def process_new_trade(wallet, trade, insider_stats):
    trade_id = get_trade_id(trade)
    if trade_id in seen_trades:
        return
    _seen_add(trade_id)

    question = trade.get("title", "Evento desconocido")
    side = trade.get("side", "BUY")
    size = float(trade.get("usdcSize", 0))
    price = float(trade.get("price", 0.5))
    market_id = trade.get("conditionId") or trade.get("asset") or ""
    if not market_id:
        print(f"[MONITOR] trade sin conditionId/asset — skip {trade.get('transactionHash','')[:16]}")
        return
    slug = trade.get("slug", "")
    outcome_name = trade.get("outcome", side)
    end_date = get_market_end_date(slug) if slug else None

    # --- Deduplicación y detección de cobertura (mismo mercado) ---
    is_duplicate, total_size, hedge_status = update_market_cache(wallet, market_id, side, size, question)

    DEF_COPY_WALLETS = {
        '0x204f72f35326db932158cba6adff0b9a1da95e14',  # Swisstony
        '0x84dbb7103982e3617704a2ed7d5b39691952aeeb',  # Soarin22
        '0xc96e5287ab294ac0388c2ddb00180fc464cff1f9',  # wigglew
        '0xa6d9b55a6a3a54a9d50ca94c64f00af69c50fd2f',  # kinderSman
    }
    _is_def_wallet = (wallet or '').lower() in DEF_COPY_WALLETS
    if is_duplicate:
        if not _is_def_wallet:
            print(f"[DEDUP] Size acumulado +{round(size)} USDC para {question[:35]}")
            return
        # L91 — DEF wallets: permitir compras adicionales (REINFORCE).
        # No mandamos Telegram pero sí guardamos en detected_trades y
        # llamamos al paper executor para que recalcule el size.
        # L80 (2026-06-09): marcar is_reinforce=True para que el real router
        # NO abra posición nueva — ya tenemos una para este mercado.
        print(f"[DEDUP-DEF] REINFORCE +{round(size)} USDC · {question[:35]}")

    # L90 (2026-06-07) — Bypass filtros COBERTURA para wallets DEF_COPY.
    # El hedge de Swisstony/Lily es parte de su estrategia (in-play). Su MERGE
    # los disuelve a $0.50 y el watcher MERGE-detection ya lo gestiona. Filtrar
    # aquí nos hace perder ~30% de su volumen sin razón válida.
    DEF_COPY_BYPASS_HEDGE = {
        '0x204f72f35326db932158cba6adff0b9a1da95e14',  # Swisstony
        '0x84dbb7103982e3617704a2ed7d5b39691952aeeb',  # Soarin22
        '0xc96e5287ab294ac0388c2ddb00180fc464cff1f9',  # wigglew
        '0xa6d9b55a6a3a54a9d50ca94c64f00af69c50fd2f',  # kinderSman
    }
    _wallet_lc = (wallet or '').lower()
    if _wallet_lc not in DEF_COPY_BYPASS_HEDGE:
        if hedge_status == "COBERTURA":
            print(f"Filtrado [COBERTURA]: {wallet[:10]} apostó a ambos lados en {question[:40]}")
            return

        # --- Detección de cobertura entre mercados separados del mismo partido ---
        match_hedge, dom_q, dom_side, dom_size = detect_match_hedge(wallet, question, side, size)

        if match_hedge == "COBERTURA_PARTIDO":
            print(f"Filtrado [COBERTURA_PARTIDO]: {wallet[:10]} apostó a ambos lados del partido en {question[:40]}")
            return

    # --- Filtros duros ---
    passed, level = passes_hard_filters(trade, insider_stats, end_date)
    if not passed:
        print(f"Filtrado [{level}]: {question[:40]}")
        return

    # --- Pausa automática por pérdidas consecutivas ---
    # L81: DEF_COPY wallets (Swisstony/Lily) se saltan todos los checks de
    # reputación/penalización. Son los traders que monitoreamos precisamente
    # por su edge probado — pausarlos o penalizarlos no tiene sentido, y
    # las queries DB añaden latencia innecesaria en el fast path (2s poll).
    if not _is_def_wallet:
        paused, pause_reason = daily_performance(wallet)
        if paused:
            print(f"Wallet pausada [{pause_reason}]: {wallet[:10]}")
            return

    # --- Métricas para preferencias y BD ---
    retorno = (1 / price) - 1 if price > 0 else 0
    days_left_val = compute_days_left(end_date)
    niche = get_question_niche(question)
    insider_score = insider_stats.get("insider_score", 0)

    # Early-exit total para niches descartados (2026-05-25 user decision).
    # Ni se almacena la señal — el trader que la emite tampoco recibe
    # crédito en su perfil para este trade.
    if niche in IGNORED_NICHES:
        return

    ok, reason = passes_user_preferences(niche, retorno, days_left_val, insider_score, entry_price=price)
    if not ok:
        print(f"Filtrado [{reason}]: {question[:40]}")
        return

    # Research: desactivado desde L30 (2026-05-31). Stub directo sin cache.
    print(f"[{level}] {wallet[:8]}... | {question[:45]} | {side} {round(size)} USDC | NO-CLAUDE")
    research = {
        "confidence": 100,
        "summary": "",
        "is_sports": (niche in {"sports", "esports"}),
        "is_political": False,
    }
    adjusted_confidence = 100

    # --- Filtro de reputación dinámica — solo wallets no-DEF ─────────────
    # L81: DEF_COPY wallets bypass estas queries (saves 3 DB reads por señal).
    if _is_def_wallet:
        is_elite = True   # por definición
        rep_tier  = "ELITE"
    else:
        is_elite = database.is_elite_trader(wallet, niche) if niche else False
        if database.is_trader_penalized(wallet):
            print(f"Filtrado [PENALIZADO]: {wallet[:10]} ignorado por 3 pérdidas seguidas")
            return
        rep = database.get_trader_reputation(wallet)
        rep_tier = rep["tier"]

    # --- Guardar trade detectado ---
    elite_tag = f" [ELITE {niche.upper()}]" if is_elite else ""
    if rep_tier in ("ELITE", "BUENO"):
        elite_tag += f" [{rep_tier}]"

    # Usar total_size acumulado (puede ser mayor que el size individual)
    display_size = total_size if total_size > size else size

    trade_data = {
        "wallet": wallet,
        "market_id": market_id,
        "question": question,
        "side": side,
        "size": display_size,
        "price": price,
        "confidence": adjusted_confidence / 100,
        "research": research.get("summary", ""),
        "days_left": days_left_val,
        "hours_left": research.get("hours_left"),
        "retorno": round(retorno, 4)
    }
    trade_id_db = database.save_detected_trade(trade_data)

    # --- Horas hasta resolución ---
    hours_until_resolution = None
    if end_date:
        try:
            from datetime import timezone as _tz
            dt = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_tz.utc)
            diff = dt - datetime.now(_tz.utc)
            hours_until_resolution = max(0, diff.total_seconds() / 3600)
        except:
            pass

    # --- Alerta manual + Telegram (solo primera vez, no en REINFORCE DEF) ---
    time_tag = f"{days_left_val}d" if days_left_val < 999 else "sin fecha"
    size_tag  = f" (acum {round(display_size)} USDC)" if display_size > size else ""
    if not (is_duplicate and _is_def_wallet):
        print(f"ALERTA{elite_tag}: {round(adjusted_confidence)}% conf | {time_tag}{size_tag} | {wallet[:10]}")
        # L81 — Telegram fire-and-forget: marcar ANTES de crear la tarea para que
        # la deduplicación funcione aunque lleguen señales rápidas mientras el
        # HTTP de Telegram está en vuelo. El await bloqueaba ~400ms en el fast path.
        mark_alerted(wallet, market_id)
        asyncio.create_task(telegram_bot.send_alert(
            trade_id=trade_id_db,
            wallet=wallet,
            question=question,
            side=side,
            size=display_size,
            price=price,
            insider_stats=insider_stats,
            research=research,
            end_date=end_date,
            outcome_name=outcome_name
        ))

    # ── Paper trading automático (Fase 2.3) ───────────────────────────────
    # Crea 1 paper_trade simulado por cada señal que pase los filtros.
    # NO afecta executor real, manual_trades ni my_wallet. Cualquier fallo
    # se loguea pero no tumba el monitor.
    try:
        import paper_executor
        # Fase 2.6: devuelve lista de paper_trade IDs (uno por agente activo)
        ids = paper_executor.evaluate_signal_for_paper_trade({
            "detected_trade_id": trade_id_db,
            "wallet":            wallet,
            "market_id":         market_id,
            "token_id":          trade.get("asset"),  # Polymarket activity payload
            "question":          question,
            "outcome_name":      outcome_name,
            "side":              side,
            "niche":             niche,
            "insider_price":     price,
            "size_usdc":         display_size,  # tamaño acumulado (posición actual del insider)
            "timestamp":         trade.get("timestamp"),  # unix ts del trade original
            "is_reinforce":      is_duplicate and _is_def_wallet,
        })
        if ids:
            print(f"[PAPER] {len(ids)} agentes abrieron trade para señal {trade_id_db}")
    except Exception as e:
        print(f"[PAPER] evaluate_signal failed for {trade_id_db}: {e}")

async def process_wallet(wallet_data, last_timestamps):
    wallet        = wallet_data[0]
    insider_score = wallet_data[1]
    win_rate      = wallet_data[2]
    total_trades  = wallet_data[3]
    profit_factor = wallet_data[4] if len(wallet_data) > 4 else 0

    # L42 (2026-06-02) — limit=3 era catastrófico para traders hiperactivos.
    # Lilybaeum hace 43 BUYs/h en Polymarket. Entre dos polls de 30s del
    # monitor puede haber 5+ trades; con limit=3 perdíamos el 95% de sus
    # signals válidos (capture rate 8/170 en 48h). Subimos a 30 trades para
    # cubrir cualquier burst razonable. El filtro por last_timestamp ya evita
    # re-procesar trades antiguos, así que un limit mayor solo añade margen.
    # L92 (2026-06-07) — Paginación hasta cubrir last_ts.
    # Antes: solo primera página de la API (100 trades). Si el bot tenía un
    # hiccup (DNS, network) y Swisstony hacía bursts, los trades entre
    # last_ts y los 100 más recientes se perdían silenciosamente.
    # Ahora: paginamos páginas adicionales hasta que la página más antigua
    # contenga trades con ts <= last_ts. Tope MAX_PAGES=5 (≈500 trades atrás).
    last_ts = last_timestamps.get(wallet, int(datetime.now().timestamp()))
    db_username = database.get_wallet_username(wallet)
    MAX_PAGES = 5
    PAGE_SIZE = 100
    trades = []
    for page in range(MAX_PAGES):
        page_trades = get_recent_activity(wallet, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
        if not page_trades:
            break
        trades.extend(page_trades)
        # Si el más antiguo de esta página ya está cubierto, no necesitamos más
        oldest_ts = int(page_trades[-1].get("timestamp", 0))
        if oldest_ts <= last_ts:
            break
    if page >= MAX_PAGES - 1 and trades:
        print(f"[MONITOR] {wallet[:10]}… alcanzó tope {MAX_PAGES} páginas — posible burst histórico no cubierto")
    # Procesar todos los trades nuevos (la deduplicación por seen_trades evita
    # reprocesar si la misma trade aparece en varias páginas)
    for trade in trades:
        ts = int(trade.get("timestamp", 0))
        if ts > last_ts:
            trade_name = trade.get("name", "") or ""
            username = db_username or (trade_name if not trade_name.startswith("0x") else "")
            insider_stats = {
                "insider_score": insider_score,
                "win_rate":      win_rate,
                "total_trades":  total_trades,
                "profit_factor": profit_factor,
                "name":          username
            }
            await process_new_trade(wallet, trade, insider_stats)
            last_timestamps[wallet] = ts

async def whitelist_fast_loop():
    """L68 (2026-06-04) — polling ultra-rápido SOLO para wallets whitelist.
    L87 (2026-06-06) — intervalo 2s (<3s gap señal→acción objetivo del usuario).
    Reduce gap Lily-action → nuestra acción a ~2s.
    """
    import os as _os
    fast_addrs = set()
    for env in ("REAL_WALLET_WHITELIST", "REAL_D_WALLET_WHITELIST"):
        for a in (_os.getenv(env,"") or "").split(","):
            if a.strip():
                fast_addrs.add(a.strip().lower())
    if not fast_addrs:
        print("[FAST_MONITOR] sin whitelist, loop no necesario")
        return
    interval = float(_os.getenv('FAST_MONITOR_INTERVAL','2'))
    print(f"[FAST_MONITOR] Loop iniciado cada {interval}s para {len(fast_addrs)} wallets whitelist")
    # Construir wallet_data objects desde DB
    wallets_meta = {}
    fast_ts = {}
    while True:
        try:
            # Cargar metadata wallets (re-fresh cada loop por si hay cambios)
            for addr in fast_addrs:
                if addr not in wallets_meta:
                    _wconn = database.get_connection()
                    r = _wconn.execute(
                        "SELECT address, insider_score, win_rate, total_trades, profit_factor "
                        "FROM wallets WHERE LOWER(address)=?", (addr,)).fetchone()
                    _wconn.close()  # L81 FIX: conexión siempre cerrada
                    if r:
                        wallets_meta[addr] = r
                        # init timestamp = now - 60s para no perder señales recientes al arrancar
                        from datetime import timezone as _tz
                        fast_ts[addr] = int(datetime.now(_tz.utc).timestamp()) - 60
            # Procesar cada whitelist wallet en paralelo
            tasks = [process_wallet(w, fast_ts) for w in wallets_meta.values()]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as e:
            print(f"[FAST_MONITOR] err: {e}")
        await asyncio.sleep(interval)


async def monitor_loop():
    # Purgar entradas de research caducadas al arrancar
    database.purge_expired_research_cache()
    print("Monitor iniciado (modo 100% automático)...")
    wallets = database.get_top_wallets(limit=100)
    if not wallets:
        print("Sin wallets. Ejecuta fetch_traders.py primero.")
        return
    last_timestamps = init_last_timestamps(wallets)
    while True:
        # L31: reload de preferencias eliminado.
        wallets = database.get_top_wallets(limit=100)
        tasks = [process_wallet(w, last_timestamps) for w in wallets]
        await asyncio.gather(*tasks)
        await asyncio.sleep(30)

if __name__ == "__main__":
    asyncio.run(monitor_loop())
