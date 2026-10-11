import asyncio
import requests
from datetime import datetime
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler
import config
import database
import executor
from research_agent import get_alert_level
from leaderboard_analyzer import NICHE_KEYWORDS


async def send_with_retry(coro_or_fn, max_retries=3):
    """Envuelve una corutina o función-fábrica de Telegram con retry + backoff.

    2026-06-10 FIX: las corutinas Python no son reutilizables — await coro dos
    veces lanza 'cannot reuse already awaited coroutine'. Si se pasa un callable
    (lambda / función) se llama en cada intento para crear una nueva corutina.
    Si se pasa una corutina ya construida, solo se ejecuta una vez (sin retry).
    """
    _is_fn = callable(coro_or_fn) and not asyncio.iscoroutine(coro_or_fn)
    for attempt in range(max_retries):
        try:
            coro = coro_or_fn() if _is_fn else coro_or_fn
            return await coro
        except Exception as e:
            if not _is_fn:
                # Coroutine ya consumida, no se puede reintentar
                print(f"[TELEGRAM] Error (no retry): {e}")
                return None
            if attempt < max_retries - 1:
                wait = 5 * (attempt + 1)
                print(f"[TELEGRAM] Error intento {attempt+1}: {e}. Reintentando en {wait}s...")
                await asyncio.sleep(wait)
            else:
                print(f"[TELEGRAM] Fallo definitivo tras {max_retries} intentos: {e}")
                return None

import re as _re

_POLITICAL_TERMS_VS = {
    "trump", "biden", "russia", "ukraine", "china", "iran", "israel",
    "congress", "senate", "election", "president", "minister", "government",
    "war", "ceasefire", "nato", "nuclear", "sanctions", "putin", "xi",
}

def _kw_in_question(kw_lower, question_lower):
    """Word-boundary match para palabras puras; substring para frases/símbolos."""
    if _re.match(r'^\w+$', kw_lower):
        return bool(_re.search(r'\b' + kw_lower + r'\b', question_lower))
    return kw_lower in question_lower

def get_niche_from_question(question):
    q = question.lower()
    for niche, keywords in NICHE_KEYWORDS.items():
        if any(_kw_in_question(kw.lower(), q) for kw in keywords):
            return niche
    # Regla "vs": dos palabras con mayúscula separadas por "vs" → DEPORTES
    if _re.search(r'[A-Z][A-Za-z]* vs\.? [A-Z]', question):
        if not any(term in q for term in _POLITICAL_TERMS_VS):
            return "sports"
    return "other"

def _save_trade_feedback(trade_id, action):
    """Lee detected_trades y guarda fila enriquecida en user_feedback."""
    try:
        conn = database.get_connection()
        c = conn.cursor()
        c.execute("""SELECT market_question, entry_price, confidence_score,
                            days_left, wallet_address, size_usdc
                     FROM detected_trades WHERE id = ?""", (trade_id,))
        row = c.fetchone()
        insider_score = 0.0
        if row:
            wallet = row[4]
            c.execute("SELECT insider_score FROM wallets WHERE address = ?", (wallet,))
            w = c.fetchone()
            insider_score = w[0] if w else 0.0
        conn.close()
        if not row:
            return
        question, entry_price, confidence, days_left, wallet_addr, size_usdc = row
        retorno = round((1 / entry_price) - 1, 4) if entry_price and entry_price > 0 else 0.0
        niche    = get_niche_from_question(question or "")
        hour_now = datetime.now().hour
        database.save_feedback(
            trade_id     = trade_id,
            action       = action,
            niche        = niche,
            confidence   = confidence or 0.0,
            retorno      = retorno,
            days_left    = days_left if days_left is not None else 999,
            insider_score= insider_score,
            wallet       = wallet_addr,
            hour_of_day  = hour_now,
            size_usdc    = size_usdc,
        )
    except Exception as e:
        print(f"Error guardando feedback: {e}")

bot = Bot(token=config.TELEGRAM_BOT_TOKEN)

SIDE_EMOJI = {"BUY": "🟢", "SELL": "🔴", "YES": "🟢", "NO": "🔴"}

def get_token_id(market_id, side):
    try:
        url = "https://clob.polymarket.com/markets/" + market_id
        print(f"[TOKEN_ID] GET {url}")
        r = requests.get(url, timeout=10)
        print(f"[TOKEN_ID] Status: {r.status_code}")
        data = r.json()
        tokens = data.get("tokens", [])
        print(f"[TOKEN_ID] Tokens encontrados: {len(tokens)} | side buscado: {side}")
        for token in tokens:
            outcome = token.get("outcome", "").upper()
            tid = token.get("token_id", "")
            print(f"[TOKEN_ID]   outcome={outcome} token_id={str(tid)[:20]}")
            if side.upper() in ["BUY", "YES"] and outcome == "YES":
                print(f"[TOKEN_ID] ✅ Match YES: {str(tid)[:20]}")
                return tid, token.get("outcome", side)
            elif side.upper() in ["SELL", "NO"] and outcome == "NO":
                print(f"[TOKEN_ID] ✅ Match NO: {str(tid)[:20]}")
                return tid, token.get("outcome", side)
        if tokens:
            tid = tokens[0].get("token_id")
            print(f"[TOKEN_ID] ⚠️ Sin match exacto, usando primer token: {str(tid)[:20]}")
            return tid, tokens[0].get("outcome", side)
    except Exception as e:
        print(f"[TOKEN_ID] ❌ Error: {e}")
    print(f"[TOKEN_ID] ❌ No token encontrado para market_id={market_id}")
    return None, side

def get_outcome_name(market_id, side):
    try:
        r = requests.get("https://clob.polymarket.com/markets/" + market_id, timeout=10)
        data = r.json()
        tokens = data.get("tokens", [])
        for token in tokens:
            outcome = token.get("outcome", "").upper()
            if side.upper() in ["BUY", "YES"] and outcome == "YES":
                return token.get("outcome", side)
            elif side.upper() in ["SELL", "NO"] and outcome == "NO":
                return token.get("outcome", side)
        if tokens:
            return tokens[0].get("outcome", side)
    except:
        pass
    return side

def format_resolution_time(end_date):
    """Devuelve (hora_UTC, fecha) de la fecha de resolución."""
    if not end_date:
        return None, None
    try:
        from datetime import timezone as _tz
        dt = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz.utc)
        else:
            dt = dt.astimezone(_tz.utc)   # normalizar siempre a UTC
        hora_utc = dt.strftime("%H:%M")
        fecha    = dt.strftime("%d/%m")
        return hora_utc, fecha
    except:
        return None, None

async def send_alert(trade_id, wallet, question, side, size, price, insider_stats, research, end_date=None, outcome_name=None):
    confidence = research.get("confidence", 0)
    summary = research.get("summary", "")
    stat = research.get("stat", "")
    noticia = research.get("noticia", "")
    risk = research.get("risk", "")
    is_sports = research.get("is_sports", False)
    days_left = research.get("days_left", 999)
    hours_left = research.get("hours_left", None)
    minutes_left = research.get("minutes_left", None)
    win_rate = round(insider_stats.get("win_rate", 0) * 100)
    score = round(insider_stats.get("insider_score", 0), 2)
    total = insider_stats.get("total_trades", 0)

    level, level_emoji = get_alert_level(confidence, days_left, hours_left)

    if hours_left is not None and days_left == 0 and hours_left <= 1:
        time_str = str(minutes_left) + "min 🚨" if minutes_left else "< 1h 🚨"
    elif days_left == 0 and hours_left is not None:
        time_str = str(hours_left) + "h"
    elif days_left == 0:
        time_str = "HOY"
    elif days_left == 1:
        time_str = "24h"
    elif days_left >= 999:
        time_str = "sin fecha"
    else:
        time_str = str(days_left) + "d"

    hora_res, fecha_res = format_resolution_time(end_date)

    side_emoji = SIDE_EMOJI.get(side.upper(), "🔵")
    wallet_short = wallet[:6] + "..." + wallet[-4:]
    trader_name = insider_stats.get("name", "") if insider_stats else ""
    name = ("👤 " + trader_name + " | " + wallet_short) if trader_name else ("👤 " + wallet_short)

    # Tier de reputación dinámica
    rep = database.get_trader_reputation(wallet)
    tier = rep["tier"]
    TIER_TAG = {"ELITE": "  👑 ELITE", "BUENO": "  ⭐ BUENO", "PENALIZADO": "  ☠️ PENALIZADO", "NORMAL": ""}
    tier_tag = TIER_TAG.get(tier, "")
    rep_line = ""
    if rep["total_copied"] > 0:
        rep_line = f"  [{rep['total_won']}W/{rep['total_lost']}L real · {round(rep['real_pnl_usdc'],2):+.2f} USDC]"

    try:
        retorno = round((1 / price) - 1, 2) if price > 0 else 0
        retorno_str = "+" + str(retorno) + "x"
        ganancia_usdc = round(config.MAX_TRADE_SIZE_USDC * retorno, 2)
        ganancia_str = "+" + str(ganancia_usdc) + " USDC si gana"
    except:
        retorno_str = "?"
        ganancia_str = ""

    posicion = outcome_name if outcome_name else side

    # Nicho detectado → emoji y etiqueta
    _niche = get_niche_from_question(question)
    _NICHE_EMOJI  = {
        "esports": "🏆", "sports": "🏆", "nba": "🏆", "nfl": "🏆",  # nba/nfl: compat. legacy
        "crypto": "💎",
        "geopolitics": "🏛", "politics": "🏛",
    }
    _NICHE_LABEL  = {
        "esports": "DEPORTES", "sports": "DEPORTES", "nba": "DEPORTES", "nfl": "DEPORTES",
        "crypto": "CRYPTO",
        "geopolitics": "GEOPOLÍTICA", "politics": "POLÍTICA",
    }
    cat_emoji   = _NICHE_EMOJI.get(_niche, "📈")
    niche_label = _NICHE_LABEL.get(_niche, "ECONOMÍA")

    # — Bloque 1: cabecera de alerta —
    header = [level_emoji + " " + level + " — " + time_str + " restantes"]
    if hora_res and fecha_res:
        header.append("🕐 Resuelve: " + fecha_res + " a las " + hora_res + " UTC")

    # — Bloque 2: mercado y posición —
    market = [
        cat_emoji + " " + niche_label + " · " + question[:50],
        side_emoji + " Apostar: " + posicion + "  @" + str(round(price, 2)) + "  " + retorno_str,
        "💰 " + ganancia_str + "  (insider apostó " + str(round(size)) + " USDC)",
    ]

    # — Bloque 3: análisis —
    analysis = []
    if is_sports and stat:
        analysis.append("📊 " + stat[:55])
    elif noticia:
        analysis.append("📰 " + noticia[:55])
    analysis.append("🤖 " + str(confidence) + "%  ·  " + summary[:55])
    if risk:
        analysis.append("⚠️  " + risk[:55])

    # — Bloque 4: trader —
    trader_line = [name + tier_tag + rep_line + "  ·  " + str(win_rate) + "%WR  ·  " + str(total) + " trades  ·  score " + str(score)]

    msg = chr(10).join(
        header + [""] +
        market + [""] +
        analysis + [""] +
        trader_line
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📋 Registrar", callback_data="register_" + str(trade_id)),
        InlineKeyboardButton("❌ Ignorar", callback_data="ignore_" + str(trade_id))
    ]])

    await send_with_retry(bot.send_message(
        chat_id=config.TELEGRAM_CHAT_ID,
        text=msg,
        reply_markup=keyboard
    ))

async def send_auto_trade_alert(trade_id, wallet, question, side, size, price, insider_stats, research, end_date=None, outcome_name=None, auto_result=None):
    confidence = research.get("confidence", 0)
    summary = research.get("summary", "")
    is_sports = research.get("is_sports", False)
    days_left = research.get("days_left", 999)
    hours_left = research.get("hours_left", None)
    win_rate = round(insider_stats.get("win_rate", 0) * 100)
    score = round(insider_stats.get("insider_score", 0), 2)
    total = insider_stats.get("total_trades", 0)

    hora_res, fecha_res = format_resolution_time(end_date)
    side_emoji = SIDE_EMOJI.get(side.upper(), "🔵")
    cat_emoji = "🏆" if is_sports else "🏛"
    wallet_short = wallet[:6] + "..." + wallet[-4:]
    trader_name = insider_stats.get("name", "") if insider_stats else ""
    name = ("👤 " + trader_name + " | " + wallet_short) if trader_name else ("👤 " + wallet_short)
    posicion = outcome_name if outcome_name else side

    try:
        retorno = round((1 / price) - 1, 2) if price > 0 else 0
        retorno_str = "+" + str(retorno) + "x"
        ganancia_usdc = round(config.MAX_TRADE_SIZE_USDC * retorno, 2)
        ganancia_str = "+" + str(ganancia_usdc) + " USDC si gana"
    except:
        retorno_str = "?"
        ganancia_str = ""

    executed = auto_result and auto_result.get("status") in ["ok", "paper_ok"]
    status_line = "✅ EJECUTADO AUTOMÁTICAMENTE" if executed else "⚠️ AUTO-EXEC FALLIDO"
    paper_tag = "  (PAPER)" if auto_result and auto_result.get("status") == "paper_ok" else ""
    actual_price = auto_result.get("actual_price") if auto_result else None
    slippage_val = auto_result.get("slippage") if auto_result else None
    slippage_warning = auto_result.get("slippage_warning", "") if auto_result else ""

    # Precio de ejecución real vs precio del insider
    if actual_price and abs(actual_price - price) > 0.001:
        price_line = (f"{side_emoji} Apostar: {posicion}  "
                      f"@{round(actual_price, 4)} (insider @{round(price, 2)})  {retorno_str}")
    else:
        price_line = f"{side_emoji} Apostar: {posicion}  @{round(actual_price or price, 4)}  {retorno_str}"

    # Bloque 1: cabecera
    header = ["⚡ AUTO — " + status_line + paper_tag]
    if hora_res and fecha_res:
        header.append("🕐 Resuelve: " + fecha_res + " a las " + hora_res + " UTC")

    # Bloque 2: mercado
    slippage_str = f"  slip {round(slippage_val*100, 1)}%" if slippage_val else ""
    market = [
        cat_emoji + " " + question[:55],
        price_line + slippage_str,
        "💰 " + ganancia_str + "  (insider apostó " + str(round(size)) + " USDC)",
    ]

    # Bloque 3: análisis
    analysis = []
    if slippage_warning:
        analysis.append(slippage_warning[:70])
    analysis.append("🤖 " + str(confidence) + "%  ·  " + summary[:55])

    # Bloque 4: trader
    trader_line = [name + "  ·  " + str(win_rate) + "%WR  ·  " + str(total) + " trades  ·  score " + str(score)]

    msg = chr(10).join(
        header + [""] +
        market + [""] +
        analysis + [""] +
        trader_line
    )

    bot_trade_id = auto_result.get("bot_trade_id") if auto_result else None
    if executed and bot_trade_id:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("↩️ Deshacer", callback_data="undo_" + str(bot_trade_id))
        ]])
    else:
        # Ejecución fallida — mostrar motivo, sin botón de acción
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔕 Cerrar", callback_data="ignore_" + str(trade_id))
        ]])

    await send_with_retry(bot.send_message(
        chat_id=config.TELEGRAM_CHAT_ID,
        text=msg,
        reply_markup=keyboard
    ))


async def handle_callback(update, context):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("copy_"):
        trade_id = int(data.split("_")[1])
        print(f"[CALLBACK] copy_ recibido | trade_id={trade_id}")
        database.update_trade_action(trade_id, "copied")
        _save_trade_feedback(trade_id, "copied")

        conn = database.get_connection()
        c = conn.cursor()
        c.execute("SELECT market_id, side, size_usdc, entry_price, days_left, wallet_address FROM detected_trades WHERE id = ?", (trade_id,))
        row = c.fetchone()
        conn.close()

        if row:
            market_id, side, size_usdc, entry_price, days_left_stored, wallet_addr = row
            print(f"[CALLBACK] DB row: market_id={market_id} side={side} entry_price={entry_price} days_left={days_left_stored}")
            # Registrar copia en sistema de reputación
            if wallet_addr:
                username = database.get_wallet_username(wallet_addr)
                database.record_trade_copied(wallet_addr, username)
            token_id, outcome = get_token_id(market_id, side)
            if token_id:
                print(f"[CALLBACK] Llamando execute_trade: token={str(token_id)[:20]} side={side} size={config.MAX_TRADE_SIZE_USDC} price={entry_price}")
                result = executor.execute_trade(
                    detected_trade_id=trade_id,
                    market_id=market_id,
                    token_id=token_id,
                    side=side,
                    size_usdc=config.MAX_TRADE_SIZE_USDC,
                    entry_price=float(entry_price),
                )
                print(f"[CALLBACK] Resultado execute_trade: {result}")
                if result and result.get("status") in ["ok", "paper_ok"]:
                    actual = result.get("actual_price")
                    price_str = f" @ {round(actual, 4)}" if actual else ""
                    conf_line = f"✅ Trade ejecutado: {config.MAX_TRADE_SIZE_USDC} USDC en {outcome}{price_str}"
                    warning = result.get("slippage_warning", "")
                    suffix = chr(10) + chr(10) + conf_line
                    if warning:
                        suffix += chr(10) + warning
                    await query.edit_message_text(query.message.text + suffix)
                else:
                    reason = "resultado None — ver logs del servidor"
                    await query.edit_message_text(query.message.text + chr(10) + chr(10) + f"⚠️ Error ejecutando trade: {reason}")
            else:
                await query.edit_message_text(query.message.text + chr(10) + chr(10) + "⚠️ Token no encontrado para este mercado")
        else:
            await query.edit_message_text(query.message.text + chr(10) + chr(10) + "⚠️ Trade no encontrado en BD")

    elif data.startswith("register_"):
        import manual_trade_tracker as _mtt
        trade_id = int(data.split("_")[1])
        print(f"[CALLBACK] register_ recibido | trade_id={trade_id}")
        database.update_trade_action(trade_id, "copied")
        _save_trade_feedback(trade_id, "copied")

        conn = database.get_connection()
        c = conn.cursor()
        c.execute(
            "SELECT market_id, side, size_usdc, entry_price, days_left, wallet_address, market_question "
            "FROM detected_trades WHERE id = ?",
            (trade_id,)
        )
        row = c.fetchone()
        conn.close()

        if row:
            market_id, side, size_usdc, entry_price, days_left_stored, wallet_addr, question = row
            trader_name = (database.get_wallet_username(wallet_addr) or "trusted") if wallet_addr else "trusted"
            try:
                result = _mtt.log_trade(
                    market_id = market_id,
                    side      = side,
                    price     = float(entry_price) if entry_price else 0.5,
                    size      = float(size_usdc) if size_usdc else config.MAX_TRADE_SIZE_USDC,
                    trader    = trader_name,
                    reason    = "Registrado desde alerta",
                    days_left = int(days_left_stored) if days_left_stored is not None else 999,
                )
                q_short = (result.get("question") or question or market_id or "")[:55]
                conf_line = f"📋 Trade #{result['id']} registrado: {side} en {q_short}"

                # Registrar posición de la ballena para whale_tracker.
                # initial_size = shares actuales de la ballena en este (condition_id, outcome).
                tracked_id = None
                try:
                    if wallet_addr and market_id:
                        import requests as _rq
                        r = _rq.get(
                            "https://data-api.polymarket.com/positions",
                            params={"user": wallet_addr, "limit": 500},
                            timeout=10,
                        )
                        whale_size  = 0.0
                        whale_price = float(entry_price) if entry_price else 0.0
                        if r.status_code == 200:
                            target_oc = (side or "").upper().strip()
                            for p in (r.json() or []):
                                if p.get("conditionId") == market_id and (
                                    target_oc == "" or
                                    (p.get("outcome") or "").upper().strip() == target_oc
                                ):
                                    whale_size  = float(p.get("size") or 0)
                                    whale_price = float(
                                        p.get("curPrice") or p.get("currentPrice") or whale_price
                                    )
                                    break
                        if whale_size > 0:
                            tracked_id = database.add_tracked_position(
                                manual_trade_id=result["id"],
                                wallet_address=wallet_addr,
                                condition_id=market_id,
                                outcome=(side or "").upper().strip(),
                                initial_size=whale_size,
                                initial_price=whale_price,
                            )
                            if tracked_id:
                                conf_line += f"\n🐋 Tracking ballena ({whale_size:.0f} shares)"
                        else:
                            print(f"[CALLBACK] Whale tracker: posición no encontrada en /positions "
                                  f"para wallet={wallet_addr[:10]}… cid={market_id[:10]}…")
                except Exception as e:
                    print(f"[CALLBACK] Error registrando whale tracker: {e}")

                await query.edit_message_text(query.message.text + chr(10) + chr(10) + conf_line)
            except Exception as e:
                print(f"[CALLBACK] Error registrando trade manual: {e}")
                await query.edit_message_text(
                    query.message.text + chr(10) + chr(10) + f"⚠️ Error registrando: {e}"
                )
        else:
            await query.edit_message_text(query.message.text + chr(10) + chr(10) + "⚠️ Trade no encontrado en BD")

    elif data.startswith("ignore_"):
        trade_id = int(data.split("_")[1])
        database.update_trade_action(trade_id, "ignored")
        _save_trade_feedback(trade_id, "ignored")
        await query.edit_message_text(query.message.text + chr(10) + chr(10) + "❌ Trade ignorado.")

    elif data.startswith("undo_"):
        bot_trade_id = int(data.split("_")[1])
        await query.edit_message_text(query.message.text + chr(10) + chr(10) + "⏳ Cerrando posición...")
        result = executor.close_position(bot_trade_id)
        if result and result.get("status") in ["ok", "paper_ok"]:
            await query.edit_message_text(query.message.text + chr(10) + "✅ Posición cerrada correctamente.")
        else:
            msg_err = result.get("msg", "error desconocido") if result else "sin respuesta"
            await query.edit_message_text(query.message.text + chr(10) + "⚠️ No se pudo cerrar: " + msg_err)

async def stats_command(update, context):
    """Responde a /stats con el dashboard personalizado del usuario."""
    try:
        import behavior_analyzer
        msg = behavior_analyzer.build_stats_message()
    except Exception as e:
        print(f"[STATS] Error generando estadísticas: {e}")
        msg = "⚠️ Error generando estadísticas. Revisa los logs."
    await send_with_retry(context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=msg,
    ))

async def logtrade_command(update, context):
    """
    /logtrade <market_id> <YES|NO> <precio> <USDC> [trader] ["razón"] [días]
    Registra un trade manual para aprendizaje de patrones.
    """
    import manual_trade_tracker as _mtt
    full_text = update.message.text or ""
    parsed = _mtt.parse_logtrade_command(full_text)

    if not parsed or "error" in parsed:
        err = (parsed or {}).get("error", "Formato incorrecto")
        await send_with_retry(context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ {err}"
        ))
        return

    await send_with_retry(context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="⏳ Buscando mercado en Polymarket..."
    ))

    try:
        result = _mtt.log_trade(
            market_id = parsed["market_id"],
            side      = parsed["side"],
            price     = parsed["price"],
            size      = parsed["size"],
            trader    = parsed["trader"],
            reason    = parsed["reason"],
            days_left = parsed["days_left"],
        )
        retorno_str = f"+{result['retorno']}x" if result["retorno"] else "?"
        days_str    = f"{result['days_left']}d" if result["days_left"] < 999 else "sin fecha"
        q           = result["question"] or parsed["market_id"][:30]
        msg = (
            f"✅ Trade #{result['id']} registrado\n\n"
            f"📋 {q[:60]}\n"
            f"🎯 {parsed['side']} @{parsed['price']}  ({retorno_str})\n"
            f"💰 {parsed['size']} USDC  |  {days_str}  |  {result['niche'].upper()}\n"
        )
        if parsed["trader"] != "manual":
            msg += f"👤 Copiando: {parsed['trader']}"
            if parsed["reason"]:
                msg += f"  — {parsed['reason']}"
            msg += "\n"
        msg += (
            f"\nEl sistema resolverá automáticamente cuando el mercado cierre.\n"
            f"/trades para ver abiertos  ·  /tradepatterns para análisis"
        )
        print(f"[MANUAL] Trade #{result['id']} registrado: {parsed['side']} {q[:40]}")
    except Exception as e:
        print(f"[MANUAL] Error logtrade: {e}")
        msg = f"⚠️ Error registrando trade: {e}"

    await send_with_retry(context.bot.send_message(
        chat_id=update.effective_chat.id, text=msg
    ))

async def trades_command(update, context):
    """Responde a /trades con los trades manuales abiertos."""
    import manual_trade_tracker as _mtt
    try:
        msg = _mtt.build_open_trades_message()
    except Exception as e:
        msg = f"⚠️ Error: {e}"
    await send_with_retry(context.bot.send_message(
        chat_id=update.effective_chat.id, text=msg
    ))

async def traderesult_command(update, context):
    """
    /traderesult <id> <won|lost>
    Registra manualmente el resultado de un trade.
    """
    text  = (update.message.text or "").strip()
    parts = text.split()
    # parts[0] = /traderesult, parts[1] = id, parts[2] = won/lost
    if len(parts) < 3:
        await send_with_retry(context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ Formato: /traderesult <id> <won|lost>\nEjemplo: /traderesult 3 won"
        ))
        return

    try:
        trade_id = int(parts[1])
    except ValueError:
        await send_with_retry(context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ ID inválido: '{parts[1]}'"
        ))
        return

    result = parts[2].lower()
    if result not in ("won", "lost"):
        await send_with_retry(context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ Resultado debe ser 'won' o 'lost'"
        ))
        return

    winning_side = "YES" if result == "won" else "NO"
    # Necesitamos saber el lado del trade para calcular el PnL correctamente
    # Leemos primero
    conn = database.get_connection()
    c = conn.cursor()
    c.execute("SELECT side FROM manual_trades WHERE id=?", (trade_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        await send_with_retry(context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ Trade #{trade_id} no encontrado"
        ))
        return

    # Si el resultado es "won", winning_side = lado del trade
    trade_side = row[0].upper()
    if result == "won":
        winning_side = trade_side
    else:
        winning_side = "NO" if trade_side == "YES" else "YES"

    won = database.resolve_manual_trade(trade_id, winning_side)
    emoji = "✅" if won else "❌"
    tag   = "GANADO" if won else "PERDIDO"
    await send_with_retry(context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"{emoji} Trade #{trade_id} marcado como {tag}\n/tradepatterns para ver análisis"
    ))

async def tradepatterns_command(update, context):
    """Responde a /tradepatterns con el análisis de patrones manuales."""
    import manual_trade_tracker as _mtt
    try:
        msg = _mtt.build_patterns_message()
    except Exception as e:
        msg = f"⚠️ Error: {e}"
    await send_with_retry(context.bot.send_message(
        chat_id=update.effective_chat.id, text=msg
    ))

async def pause_command(update, context):
    """Activa veto manual permanente (Fase 2.5)."""
    import risk_manager
    eid = risk_manager.manual_pause(reason=f"telegram /pause from {update.effective_user.id}")
    msg = (f"⏸ <b>Bot pausado</b> (event #{eid})\n"
           f"Veto manual permanente activo.\n"
           f"Usa /resume para reanudar.")
    await send_with_retry(context.bot.send_message(
        chat_id=update.effective_chat.id, text=msg, parse_mode="HTML"
    ))


async def resume_command(update, context):
    """Libera veto manual. Si hay otros vetos, los reporta."""
    import risk_manager
    result = risk_manager.manual_resume()
    if not result["manual_released"] and not result["other_vetoes"]:
        msg = "ℹ️ El bot ya estaba activo (no había veto manual)."
    elif result["manual_released"] and not result["other_vetoes"]:
        msg = "▶️ <b>Bot reanudado</b>. Sin otros vetos activos."
    else:
        lines = ["⚠️ Veto manual liberado pero hay <b>otros vetos activos</b>:"]
        for v in result["other_vetoes"]:
            cd = f" · countdown {v['countdown_s']}s" if v["countdown_s"] else " · permanente"
            lines.append(f"  • <b>{v['type']}</b>: trigger={v['trigger']}, threshold={v['threshold']}{cd}")
        msg = "\n".join(lines)
    await send_with_retry(context.bot.send_message(
        chat_id=update.effective_chat.id, text=msg, parse_mode="HTML"
    ))


async def risk_status_command(update, context):
    """Devuelve estado completo del risk manager."""
    import risk_manager
    s = risk_manager.get_dashboard_state()
    vetoes = s["active_vetoes"]
    if vetoes:
        v_lines = [f"🛑 <b>VETOS ACTIVOS</b>:"]
        for v in vetoes:
            cd = f" · {v['countdown_s']}s restantes" if v["countdown_s"] else " · permanente"
            v_lines.append(f"  • {v['type']}: trigger={v['trigger']}, threshold={v['threshold']}{cd}")
        v_str = "\n".join(v_lines)
    else:
        v_str = "✅ <b>SIN VETOS</b>"

    msg = (
        f"<b>Risk Manager</b> · {'enabled' if s['enabled'] else 'DISABLED'}\n\n"
        f"{v_str}\n\n"
        f"<b>Sesión actual</b>\n"
        f"  PnL net: <b>${s['session_pnl_net']:+.2f}</b>\n"
        f"  Drawdown trigger: ${s['drawdown_threshold']:+.2f} (${s['trade_size']} × {s['drawdown_multiplier']})\n"
        f"  Distancia al stop: <b>${s['drawdown_distance']:+.2f}</b>\n\n"
        f"<b>Otros checks</b>\n"
        f"  API errors (1h): {s['api_errors_last_hour']} / {s['max_api_errors_hour']}\n"
        f"  Wallet balance: ${s['wallet_balance']} (min ${s['min_wallet_balance']})\n\n"
        f"<b>Eventos últimos 14d</b>: {s['events_14d_total']}"
    )
    await send_with_retry(context.bot.send_message(
        chat_id=update.effective_chat.id, text=msg, parse_mode="HTML"
    ))


def run_bot():
    from telegram.ext import CommandHandler as _CommandHandler
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(_CommandHandler("stats", stats_command))
    print("Bot Telegram escuchando...")
    app.run_polling()

if __name__ == "__main__":
    run_bot()