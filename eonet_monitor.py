"""
eonet_monitor.py — Monitor de eventos naturales NASA EONET
=========================================================
Flujo:
  1. Consulta EONET cada 10 min (sin API key).
  2. Para cada evento activo nuevo busca mercados meteorológicos en Polymarket.
  3. Filtra falsos positivos: el título del mercado debe contener la keyword exacta.
  4. Llama a Claude Haiku para generar una explicación de 60 chars.
  5. Envía alerta a Telegram con formato propio (sin stats de trader).
  6. Guarda el event_id en BD para no repetir en 24 h.
"""

import asyncio
import json
import re
import requests
from datetime import datetime, timedelta, timezone

import anthropic
import config
import database
import telegram_bot
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# Singleton — un único cliente para toda la vida del proceso
_haiku_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

# ── Constantes ────────────────────────────────────────────────────────────────

EONET_URL         = "https://eonet.gsfc.nasa.gov/api/v3/events?status=open&limit=20"
POLYMARKET_SEARCH = "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=20&q="
POLL_INTERVAL     = 600   # segundos entre consultas (10 min)
DEDUP_HOURS       = 24    # horas mínimas entre alertas del mismo evento

# ── Keywords por categoría EONET ──────────────────────────────────────────────
# Regla: SOLO términos específicos. NUNCA "fire", "storm", "seismic" solos.
CATEGORY_KEYWORDS = {
    "Severe Storms":         ["hurricane", "typhoon", "cyclone"],
    "Wildfires":             ["wildfire"],
    "Earthquakes":           ["earthquake"],
    "Volcanoes":             ["volcano"],
    "Floods":                ["flooding"],
    "Snow":                  ["blizzard"],
    "Landslides":            ["landslide"],
    "Drought":               ["drought"],
    "Temperature Extremes":  ["heatwave"],
    "Dust and Haze":         ["dust storm"],
    # Categorías sin keywords útiles para Polymarket → ignorar
    "Sea and Lake Ice":      [],
    "Water Color":           [],
}

# Emoji por categoría para el encabezado del mensaje
CATEGORY_EMOJI = {
    "Severe Storms":         "🌪",
    "Wildfires":             "🔥",
    "Earthquakes":           "🌍",
    "Volcanoes":             "🌋",
    "Floods":                "🌊",
    "Snow":                  "❄️",
    "Landslides":            "⛰",
    "Drought":               "☀️",
    "Temperature Extremes":  "🌡",
    "Dust and Haze":         "🌫",
}


# ── BD: deduplicación 24 h ────────────────────────────────────────────────────

def _init_eonet_table():
    """Crea la tabla eonet_seen_events si no existe (migración automática)."""
    conn = database.get_connection()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS eonet_seen_events (
            event_id     TEXT PRIMARY KEY,
            processed_at TEXT
        )
    """)
    conn.commit()
    conn.close()

def _is_seen(event_id: str) -> bool:
    """True si el evento fue alertado en las últimas DEDUP_HOURS."""
    try:
        conn = database.get_connection()
        c = conn.cursor()
        c.execute("SELECT processed_at FROM eonet_seen_events WHERE event_id = ?", (event_id,))
        row = c.fetchone()
        conn.close()
        if not row:
            return False
        processed = datetime.fromisoformat(row[0])
        # Normalizar a naive UTC para comparar
        if processed.tzinfo is not None:
            processed = processed.astimezone(timezone.utc).replace(tzinfo=None)
        return (datetime.utcnow() - processed).total_seconds() < DEDUP_HOURS * 3600
    except Exception:
        return False

def _mark_seen(event_id: str):
    """Registra o actualiza el timestamp del evento procesado."""
    try:
        conn = database.get_connection()
        c = conn.cursor()
        c.execute("""
            INSERT INTO eonet_seen_events (event_id, processed_at) VALUES (?, ?)
            ON CONFLICT(event_id) DO UPDATE SET processed_at = excluded.processed_at
        """, (event_id, datetime.utcnow().isoformat()))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[EONET] Error guardando event_id en BD: {e}")


# ── Fetch EONET ───────────────────────────────────────────────────────────────

def fetch_eonet_events() -> list:
    """Devuelve eventos EONET activos. Sin API key necesaria."""
    try:
        r = requests.get(EONET_URL, timeout=15)
        if r.status_code == 200:
            return r.json().get("events", [])
    except Exception as e:
        print(f"[EONET] Error fetching events: {e}")
    return []


# ── Búsqueda en Polymarket ────────────────────────────────────────────────────

def _parse_outcome_price(market) -> float:
    """
    outcomePrices llega como lista ["0.62","0.38"] o como string "['0.62','0.38']".
    Devuelve el precio YES como float.
    """
    raw = market.get("outcomePrices")
    if not raw:
        return 0.5
    if isinstance(raw, str):
        try:
            raw = json.loads(raw.replace("'", '"'))
        except Exception:
            return 0.5
    try:
        return float(raw[0])
    except Exception:
        return 0.5

def search_polymarket(keyword: str) -> list:
    """Busca mercados activos en Polymarket que contengan la keyword."""
    try:
        r = requests.get(POLYMARKET_SEARCH + requests.utils.quote(keyword), timeout=10)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                return data
    except Exception as e:
        print(f"[EONET] Error buscando '{keyword}' en Polymarket: {e}")
    return []

def _market_is_relevant(market, keyword: str) -> bool:
    """
    El título del mercado debe contener la keyword meteorológica exacta
    (word boundary) para evitar falsos positivos.
    """
    title = (market.get("question") or market.get("title", "")).lower()
    pattern = r'\b' + re.escape(keyword.lower()) + r'\b'
    return bool(re.search(pattern, title))

def find_matching_markets(event: dict) -> list:
    """
    Para cada categoría del evento busca mercados en Polymarket.
    Solo incluye mercados donde el título contiene la keyword exacta.
    Devuelve máximo 2 mercados (los más relevantes).
    """
    categories = [c.get("title", "") for c in event.get("categories", [])]
    seen_ids   = set()
    matches    = []

    for cat in categories:
        keywords = CATEGORY_KEYWORDS.get(cat, [])
        for kw in keywords:
            markets = search_polymarket(kw)
            for m in markets:
                mid = m.get("conditionId") or m.get("id", "")
                if not mid or mid in seen_ids:
                    continue
                if not _market_is_relevant(m, kw):
                    continue
                seen_ids.add(mid)
                matches.append(m)
                if len(matches) >= 2:
                    return matches

    return matches


# ── Geo ───────────────────────────────────────────────────────────────────────

def _extract_geo(event: dict):
    """
    Extrae (date_str, lat, lon) del campo geometry.
    Las coordenadas EONET siguen el estándar GeoJSON: [longitud, latitud].
    """
    geo = event.get("geometry", [])
    if not geo:
        return "", None, None
    last     = geo[-1]
    date_str = last.get("date", "")[:10]
    coords   = last.get("coordinates", [])
    try:
        lon = float(coords[0])
        lat = float(coords[1])
    except (IndexError, TypeError, ValueError):
        lon, lat = None, None
    return date_str, lat, lon


# ── Claude Haiku: explicación breve ──────────────────────────────────────────

def _get_haiku_explanation(event_title: str, market_question: str) -> str:
    """
    Llama a Claude Haiku (singleton) para generar una explicación de ≤60 chars
    sobre por qué el evento NASA afecta al mercado Polymarket.
    """
    try:
        prompt = (
            f"Evento NASA: {event_title}. "
            f"Mercado Polymarket: {market_question}. "
            f"En máximo 60 caracteres explica por qué este evento afecta "
            f"a este mercado. Solo la explicación, sin preámbulos."
        )
        msg = _haiku_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{"role": "user", "content": prompt}]
        )
        return msg.content[0].text.strip()[:60]
    except Exception as e:
        print(f"[EONET] Error llamando Claude Haiku: {e}")
        return "Evento meteorológico activo con impacto potencial"


# ── Alerta Telegram ───────────────────────────────────────────────────────────

async def send_eonet_alert(event: dict, market: dict):
    """
    Envía alerta EONET con formato propio — sin stats de trader.
    Confianza fija 60 %. No muestra win rate ni score.
    """
    # ── Datos del evento ──
    title_event = event.get("title", "Evento desconocido")
    categories  = [c.get("title", "") for c in event.get("categories", [])]
    cat_label   = categories[0] if categories else "Evento natural"
    cat_emoji   = CATEGORY_EMOJI.get(cat_label, "🌍")
    date_str, lat, lon = _extract_geo(event)

    geo_str = ""
    if lat is not None:
        geo_str = f"📍 {round(lat, 2)}°{'N' if lat >= 0 else 'S'}  {round(abs(lon), 2)}°{'E' if lon >= 0 else 'W'}"
    else:
        # Si no hay coords, intentar extraer región del título
        geo_str = f"📍 {title_event.split(',')[-1].strip()}" if "," in title_event else ""

    # ── Datos del mercado ──
    question  = market.get("question") or market.get("title", "Mercado desconocido")
    market_id = market.get("conditionId", "")
    price_yes = _parse_outcome_price(market)
    end_date  = market.get("endDate", None)

    try:
        retorno = round((1 / price_yes) - 1, 2) if price_yes > 0 else 0
    except Exception:
        retorno = 0
    ganancia = round(config.MAX_TRADE_SIZE_USDC * retorno, 2)

    # Fecha de resolución
    fecha_res = ""
    if end_date:
        try:
            dt = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            fecha_res = f"  ·  Resuelve {dt.strftime('%d/%m')} {dt.strftime('%H:%M')} UTC"
        except Exception:
            pass

    # ── Explicación de Claude Haiku ──
    explanation = _get_haiku_explanation(title_event, question)

    # ── Guardar en BD (wallet="eonet_nasa" para tracking separado) ──
    trade_data = {
        "wallet":     "eonet_nasa",
        "market_id":  market_id,
        "question":   question,
        "side":       "BUY",
        "size":       0,
        "price":      price_yes,
        "confidence": 0.60,
        "research":   f"NASA EONET [{cat_label}]: {title_event}",
        "days_left":  999,
        "hours_left": None,
        "retorno":    retorno,
    }
    trade_id = database.save_detected_trade(trade_data)

    # ── Mensaje ──
    msg_lines = [
        f"{cat_emoji} NASA EONET — {cat_label}",
        "",
        f"📡 {title_event}",
    ]
    if geo_str:
        msg_lines.append(geo_str)
    msg_lines += [
        f"🗓 Detectado: {date_str or 'hoy'}",
        "",
        f"🏛 Mercado relacionado:",
        f"{question[:80]}{fecha_res}",
        f"🟢 YES  |  @{round(price_yes, 2)}  |  +{retorno}x",
        f"💰 +{ganancia} USDC si gana",
        "",
        f"🤖 {explanation}",
        f"⚠️ Verifica liquidez antes de copiar",
    ]
    msg = "\n".join(msg_lines)

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Copiar",  callback_data=f"copy_{trade_id}"),
        InlineKeyboardButton("❌ Ignorar", callback_data=f"ignore_{trade_id}"),
    ]])

    print(f"[EONET] {cat_emoji} {title_event[:40]} → {question[:40]}")

    await telegram_bot.send_with_retry(telegram_bot.bot.send_message(
        chat_id=config.TELEGRAM_CHAT_ID,
        text=msg,
        reply_markup=keyboard,
    ))


# ── Loop principal ────────────────────────────────────────────────────────────

async def eonet_loop():
    """
    Consulta NASA EONET cada 10 min.
    Solo alerta eventos nuevos o que no hayan sido alertados en 24 h.
    """
    _init_eonet_table()
    print("[EONET] 🛰  Monitor NASA EONET iniciado (polling cada 10 min)")

    while True:
        try:
            events = fetch_eonet_events()
            print(f"[EONET] {len(events)} eventos activos encontrados")

            for event in events:
                event_id = event.get("id", "")
                if not event_id or _is_seen(event_id):
                    continue

                markets = find_matching_markets(event)
                if not markets:
                    continue

                # Marcar como procesado ANTES de enviar para evitar duplicados
                # si el bot se reinicia durante el envío
                _mark_seen(event_id)

                for market in markets:
                    try:
                        await send_eonet_alert(event, market)
                        await asyncio.sleep(2)  # pausa entre mensajes
                    except Exception as e:
                        print(f"[EONET] Error enviando alerta: {e}")

        except Exception as e:
            print(f"[EONET] Error en loop: {e}")

        await asyncio.sleep(POLL_INTERVAL)
