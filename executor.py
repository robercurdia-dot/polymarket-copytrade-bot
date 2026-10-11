"""
executor.py — Ejecución de órdenes en Polymarket CLOB
======================================================
Fase 3.0: refactor crítico (kill switch doble, transacción atómica, fill parcial).
Fase 3.1: slippage realista, retries con backoff, validación de respuesta CLOB,
          sanitización de logs, singleton ClobClient, close_position con orderbook.

NO ejecutar este módulo directamente. Para tests: tests/test_executor.py
"""
import os
import re
import sys
import time
import threading
import concurrent.futures
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime

import requests

from py_clob_client_v2 import (
    ClobClient,
    ApiCreds,
    OrderArgs,
    OrderType,
    SignatureTypeV2,
    PartialCreateOrderOptions,
    BalanceAllowanceParams,
    AssetType,
)
from py_clob_client_v2.exceptions import PolyApiException
from dotenv import load_dotenv, set_key

import config
import database
import risk_manager

_ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH)

# Timeout HTTP para llamadas críticas al CLOB. py_clob_client.ClobClient no
# expone timeout en el constructor → envolvemos llamadas con concurrent.futures.
CLOB_OP_TIMEOUT_SECONDS = 10

# Slippage máximo permitido (Fase 3.1 — desde config, default 0.05 = 5%).
MAX_SLIPPAGE_ALLOWED = config.MAX_SLIPPAGE

# Spread máximo aceptable al cerrar posición (best_ask - best_bid) / best_bid.
MAX_CLOSE_SPREAD = 0.10


# ─────────────────────────────────────────────────────────────────────
# FIX F — Sanitización de logs
# ─────────────────────────────────────────────────────────────────────

SENSITIVE_PATTERNS = [
    r"0x[a-fA-F0-9]{64}",       # private keys (32 bytes hex)
    r"key=[\w\-]+",
    r"secret=[\w\-]+",
    r"passphrase=[\w\-]+",
    r"api_key=[\w\-]+",
    r"api_secret=[\w\-]+",
]


def _sanitize(text) -> str:
    """Redacta patrones sensibles antes de loguear. Acepta cualquier tipo."""
    s = str(text)
    for pattern in SENSITIVE_PATTERNS:
        s = re.sub(pattern, "[REDACTED]", s)
    return s


# ─────────────────────────────────────────────────────────────────────
# FIX D — Retries con backoff exponencial
# ─────────────────────────────────────────────────────────────────────

def _is_transient(exc: Exception) -> bool:
    """
    True si la excepción es transitoria (merece retry).
    Transitorios: HTTP 429/502/503/504, 'Request exception' del SDK,
    ConnectionError, TimeoutError.
    No transitorios: 4xx (excepto los listados), validación, formato.
    """
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    if isinstance(exc, PolyApiException):
        code = getattr(exc, "status_code", None)
        if code in (429, 502, 503, 504):
            return True
        msg = str(getattr(exc, "error_msg", "") or "")
        if "Request exception" in msg:
            return True
        return False
    # httpx errors u otros con nombre indicativo
    name = type(exc).__name__.lower()
    if any(k in name for k in ("connect", "timeout", "readtimeout", "remoteprotocol")):
        return True
    return False


def _retry_with_backoff(func, max_retries=3, base_delay=1.0):
    """
    Ejecuta func() con retries para errores transitorios.
    Errores no transitorios se propagan inmediatamente sin retry.
    """
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            if _is_transient(e) and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                print(f"[EXECUTOR] Retry {attempt+1}/{max_retries} tras "
                      f"{_sanitize(e)} (espera {delay}s)")
                time.sleep(delay)
            else:
                raise


def _with_timeout(fn, *args, timeout=CLOB_OP_TIMEOUT_SECONDS, **kwargs):
    """
    Ejecuta una función síncrona con timeout. Si vence, devuelve None y loguea.
    Las excepciones internas se propagan al caller.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(fn, *args, **kwargs)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            print(f"[EXECUTOR] Timeout {timeout}s en operación CLOB ({fn.__name__})")
            return None


def refresh_poly_credentials():
    """
    Deriva nuevas API credentials desde la clave privada y las actualiza en
    config (memoria) y en .env. Invalida el singleton del cliente.
    Devuelve True/False.
    """
    try:
        print("[CREDS] Renovando API keys de Polymarket...")
        bare_client = ClobClient(
            "https://clob.polymarket.com",
            key=config.POLY_PRIVATE_KEY,
            chain_id=137,
        )
        creds = bare_client.create_or_derive_api_key()  # v2: create_or_derive_api_key (no _creds)

        config.POLY_API_KEY        = creds.api_key
        config.POLY_API_SECRET     = creds.api_secret
        config.POLY_API_PASSPHRASE = creds.api_passphrase

        set_key(str(_ENV_PATH), "POLY_API_KEY",        creds.api_key)
        set_key(str(_ENV_PATH), "POLY_API_SECRET",     creds.api_secret)
        set_key(str(_ENV_PATH), "POLY_API_PASSPHRASE", creds.api_passphrase)

        # Invalidar singleton — la próxima llamada reconstruirá con creds nuevas
        global _client_cache
        with _client_cache_lock:
            _client_cache = None

        print(f"[CREDS] API keys renovadas correctamente | key={creds.api_key[:8]}...")
        return True
    except Exception as e:
        print(f"[CREDS] Error renovando credentials: {_sanitize(e)}")
        return False


# ─────────────────────────────────────────────────────────────────────
# FIX G — Singleton ClobClient con refresh on 401
# ─────────────────────────────────────────────────────────────────────

_client_cache = None
_client_cache_lock = threading.Lock()

# Contador de trades reales exitosos creados desde el arranque del bot.
_real_trades_session = 0


def _check_session_limit():
    """
    Single-shot: tras un trade real exitoso, si se alcanza
    REAL_TRADES_SESSION_LIMIT (>0) → auto-pausa REAL_TRADING_ENABLED.
    Cuenta trades reales con status open/closed desde el último arranque.
    """
    global _real_trades_session
    _real_trades_session += 1
    limit = config.REAL_TRADES_SESSION_LIMIT
    if limit > 0 and _real_trades_session >= limit:
        try:
            set_key(str(_ENV_PATH), "REAL_TRADING_ENABLED", "false")
            config.REAL_TRADING_ENABLED = False
            print(f"[EXECUTOR] Session limit alcanzado ({_real_trades_session}/"
                  f"{limit}) · auto-pausa real trading")
            try:
                import telegram_bot
                telegram_bot.notify_user(
                    f"Single-shot: {_real_trades_session} trade(s) real(es) "
                    f"ejecutado(s). Real trading auto-pausado.")
            except Exception:
                pass
        except Exception as e:
            print(f"[EXECUTOR] Error en auto-pausa: {_sanitize(e)}")


def get_client(force_refresh=False):
    """
    Devuelve un ClobClient cacheado (singleton). force_refresh=True lo
    reconstruye — usar tras renovar credenciales (401).
    """
    global _client_cache
    with _client_cache_lock:
        if force_refresh or _client_cache is None:
            # SDK v2 — deposit wallet flow (POLY_1271 / sig_type=3).
            # ApiCreds v2 acepta secret sin padding manual (gestión interna).
            kwargs = dict(
                host="https://clob.polymarket.com",
                key=config.POLY_PRIVATE_KEY,
                chain_id=137,
                creds=ApiCreds(
                    api_key=config.POLY_API_KEY,
                    api_secret=config.POLY_API_SECRET,
                    api_passphrase=config.POLY_API_PASSPHRASE,
                ),
                signature_type=config.POLY_SIGNATURE_TYPE,
            )
            funder = config.POLY_FUNDER_ADDRESS or config.DEPOSIT_WALLET_ADDRESS
            if funder:
                kwargs["funder"] = funder
            _client_cache = ClobClient(**kwargs)
        return _client_cache


def get_current_price(token_id):
    """Precio midpoint del token. None si falla. Con retry para transitorios."""
    try:
        def _fetch():
            client = get_client()
            price = client.get_midpoint(token_id=token_id)
            return float(price.get("mid", 0))
        return _retry_with_backoff(_fetch, max_retries=3, base_delay=1.0)
    except Exception as e:
        print(f"[EXECUTOR] Error precio: {_sanitize(e)}")
        return None


# ─────────────────────────────────────────────────────────────────────
# FIX E — Validación robusta de respuesta CLOB
# ─────────────────────────────────────────────────────────────────────
# py_clob_client.create_and_post_order → post_order → post() → request()
# devuelve resp.json() (dict) en HTTP 200, o lanza PolyApiException en !=200.
#
# Esquema documentado de POST /order del CLOB de Polymarket:
#   success:             bool
#   errorMsg:            str   (vacío si success)
#   orderID:             str   (hash de la orden)
#   transactionsHashes:  list[str] (hashes on-chain de los fills, si los hubo)
#   status:              str   ("matched" | "live" | "delayed" | "unmatched")
#
# LIMITACIÓN CONOCIDA: la respuesta de POST /order NO incluye el precio/size
# exacto del fill. Para 'matched' la orden se llenó; para 'live' quedó en el
# libro; para 'unmatched' no se ejecutó. La reconciliación exacta de fill
# (precio/size real) requiere consultar el endpoint de trades — queda como
# follow-up de Prompt 3. Mientras tanto, para 'matched' asumimos fill al
# precio solicitado y size solicitado, marcado en el campo `_fill_assumed`.

@dataclass
class OrderResponse:
    success: bool
    status: str            # matched | live | delayed | unmatched
    order_id: str
    error_msg: str
    transaction_hashes: list
    raw: dict


def validate_order_response(raw) -> "OrderResponse | None":
    """
    Valida la respuesta cruda de create_and_post_order.
    Devuelve OrderResponse si es válida y success=True, None en caso contrario.
    """
    if raw is None or not isinstance(raw, dict):
        return None
    success = bool(raw.get("success", False))
    if not success:
        return None
    status = str(raw.get("status", "") or "")
    order_id = str(raw.get("orderID") or raw.get("order_id") or "")
    error_msg = str(raw.get("errorMsg", "") or "")
    tx_hashes = raw.get("transactionsHashes") or raw.get("transactionHashes") or []
    if not isinstance(tx_hashes, list):
        tx_hashes = [str(tx_hashes)]
    return OrderResponse(
        success=success, status=status, order_id=order_id,
        error_msg=error_msg, transaction_hashes=tx_hashes, raw=raw,
    )


# ─────────────────────────────────────────────────────────────────────
# execute_trade
# ─────────────────────────────────────────────────────────────────────

def execute_trade(detected_trade_id, market_id, token_id, side, size_usdc,
                  entry_price, agent_id='B'):
    """
    Ejecuta una orden en Polymarket.

    Fase 3.0: kill switch doble, risk_manager (bool,reason), transacción atómica
              pending→filled/failed/rejected, fill parcial, timeout 10s.
    Fase 3.1: slippage 5% (sin bypass invertido), retries con backoff,
              validación de respuesta CLOB, singleton client, logs sanitizados.
    Fase D (2026-05-28): parámetro `agent_id` ('B' o 'D'). D = hold-to-resolve,
                          tamaño independiente (no clampado por MAX_TRADE_SIZE_USDC).
    """
    print(f"[EXECUTOR] Iniciando execute_trade | agent={agent_id} | "
          f"market={market_id[:12]} | token={str(token_id)[:12]} | "
          f"side={side} | size={size_usdc} | entry_price={entry_price}")

    # ── Doble kill switch — hot-reload para que cambios en .env surtan efecto
    # sin reiniciar el proceso. config.PAPER_TRADING/REAL_TRADING_ENABLED se
    # leen solo en startup; aquí re-leemos os.getenv para coherencia con el
    # router (que usa os.getenv). Usamos config como fallback si os.getenv falla.
    try:
        load_dotenv(dotenv_path=_ENV_PATH, override=True)
        _paper_now   = os.getenv("PAPER_TRADING",        "true" ).strip().lower() != "false"
        _real_now    = os.getenv("REAL_TRADING_ENABLED", "false").strip().lower() == "true"
    except Exception:
        _paper_now = config.PAPER_TRADING
        _real_now  = config.REAL_TRADING_ENABLED
    if not _paper_now and not _real_now:
        print("[EXECUTOR] Real trading bloqueado: REAL_TRADING_ENABLED=false")
        return {"status": "blocked", "reason": "real_trading_disabled"}

    # ── Risk Manager (Fase 2.5) — devuelve (bool, reason) ─────────────────
    can, reason = risk_manager.can_trade(size_usdc, agent_id=agent_id)
    if not can:
        print(f"[EXECUTOR] Bloqueado por risk_manager: {reason}")
        return {"status": "blocked", "reason": reason}

    # Agentes B usan MAX_TRADE_SIZE_USDC como cap absoluto.
    # DEF_COPY usa DEFINITIVE_MAX_PER_TRADE (puede ser mayor que el global).
    # D usa su propio tamaño configurado en REAL_D_TRADE_SIZE.
    # Nota: para DEF_COPY, risk_manager ya vetó si size > def_max, así que
    # el min() siguiente es un no-op de seguridad (nunca cambia el valor).
    if agent_id == 'B':
        size_usdc = min(size_usdc, config.MAX_TRADE_SIZE_USDC)
    elif agent_id == 'DEF_COPY':
        import os as _os
        from dotenv import load_dotenv as _ldenv
        _ldenv(dotenv_path=_ENV_PATH, override=True)
        def_max = float(_os.getenv('DEFINITIVE_MAX_PER_TRADE', '20'))
        size_usdc = min(size_usdc, def_max)

    # ── Precio CLOB con timeout ───────────────────────────────────────────
    print(f"[EXECUTOR] Obteniendo precio CLOB para token {str(token_id)[:20]}...")
    current_price = _with_timeout(get_current_price, token_id)
    if current_price is None:
        print(f"[EXECUTOR] CLOB no respondió o timeout, usando entry_price "
              f"{entry_price} como fallback")
        current_price = entry_price
    else:
        print(f"[EXECUTOR] Precio CLOB: {round(current_price, 4)} | "
              f"Insider: {entry_price}")

    # ── Guard RESOLVED: precio CLOB ya resuelto ───────────────────────────
    # Mismo criterio que paper_executor y real_signal_router.
    # El precio Insider puede ser <0.97 (pasa el router) pero el CLOB
    # ya cotiza >0.97 porque el mercado resolvió entre detección y ejecución.
    # 2026-06-10: subido 0.90→0.97. Swisstony usa estrategia "No" en Copa del
    # Mundo a 88-97¢ — el guard 0.90 bloqueaba la ejecución final incluso cuando
    # el router ya había aprobado la señal. Alineado con paper_executor y router.
    if current_price < 0.05 or current_price > 0.97:
        print(f"[EXECUTOR] RESOLVED-SKIP precio CLOB={current_price:.4f} — "
              f"mercado ya resuelto, cancelando ejecución real")
        return {"status": "blocked", "reason": "resolved_skip_clob",
                "clob_price": current_price}

    slippage = abs(current_price - entry_price) / entry_price if entry_price > 0 else 0

    # ── Slippage threshold — por agente ───────────────────────────────────
    # DEF_COPY: copy-trading con umbral $300. Swisstony puede acumular durante
    # varios minutos antes de que nuestro agg cruce el umbral. En ese tiempo
    # el precio puede moverse más del 5% y bloquearnos aunque la señal sea
    # válida. Usamos DEFINITIVE_MAX_SLIPPAGE (default 15%) para dar margen.
    # B/D usan MAX_SLIPPAGE global (5%) — más estricto, señales más frescas.
    if agent_id == 'DEF_COPY':
        try:
            load_dotenv(dotenv_path=_ENV_PATH, override=True)
            _slip_max = float(os.getenv('DEFINITIVE_MAX_SLIPPAGE', '0.15'))
        except Exception:
            _slip_max = 0.15
    else:
        _slip_max = MAX_SLIPPAGE_ALLOWED
    print(f"[EXECUTOR] Slippage: {slippage:.2%} | Máx permitido: {_slip_max:.2%}")

    # ── FIX C — Slippage threshold realista (sin bypass invertido) ────────
    if slippage > _slip_max:
        print(f"[EXECUTOR] Slippage excede umbral: {slippage:.2%} > "
              f"{_slip_max:.2%}")
        return {"status": "blocked", "reason": "slippage_too_high",
                "slippage": slippage, "threshold": _slip_max}

    # ── PAPER TRADING ─────────────────────────────────────────────────────
    # L81 FIX: usar _paper_now (hot-reloado en línea 326) en vez de config.PAPER_TRADING
    # (valor de startup). Si el bot arranca con PAPER_TRADING=true y el usuario lo cambia
    # a false en .env para activar real, config.PAPER_TRADING nunca se actualiza →
    # todas las órdenes iban a paper silenciosamente. _paper_now = os.getenv live.
    if _paper_now:
        print(f"[EXECUTOR] PAPER: {side} {size_usdc} USDC @ {round(current_price, 4)}")
        bot_trade_id = database.insert_pending_trade(
            detected_trade_id=detected_trade_id,
            market_id=market_id, token_id=token_id, side=side,
            size_usdc=size_usdc,
            client_order_id=f"bt_paper_{int(time.time()*1000)}",
            paper_trade=True,
        )
        database.update_trade_filled(
            bot_trade_id=bot_trade_id,
            entry_price=current_price,
            size_filled=size_usdc,   # paper asume fill completo
            fees=0.0,
            order_id=None,
            status='open',
        )
        print(f"[EXECUTOR] PAPER OK | bot_trade_id={bot_trade_id}")
        return {"status": "paper_ok", "bot_trade_id": bot_trade_id,
                "actual_price": current_price, "slippage": round(slippage, 4)}

    # ── REAL TRADING ──────────────────────────────────────────────────────
    # INSERT pending ANTES de mandar la orden (atomicidad).
    bot_trade_id = None  # inicializado antes del try para evitar UnboundLocalError
    client_order_id = f"bt_{int(time.time()*1000)}"
    bot_trade_id = database.insert_pending_trade(
        detected_trade_id=detected_trade_id,
        market_id=market_id, token_id=token_id, side=side,
        size_usdc=size_usdc,
        client_order_id=client_order_id,
        paper_trade=False,
        agent_id=agent_id,
    )
    new_coid = f"bt_{bot_trade_id}_{int(time.time())}"
    conn = database.get_connection()
    c = conn.cursor()
    c.execute("UPDATE bot_trades SET client_order_id=? WHERE id=?",
              (new_coid, bot_trade_id))
    conn.commit()
    conn.close()

    # FIX (post-trade #6): CLOB v2 OrderArgs.size = SHARES, no USDC nominal.
    # Convertimos size_usdc (USDC nominal) → shares para que la orden gaste
    # exactamente el USDC pedido. Bug histórico: bot pasaba size=5 esperando
    # $5 nominales pero CLOB lo interpretaba como 5 shares (gasto ≈ $2 a 0.4).
    if current_price <= 0:
        print(f"[EXECUTOR] current_price <= 0; abort ({current_price})")
        database.update_trade_status(bot_trade_id, status='rejected',
                                      error="price_zero_or_negative")
        return {"status": "error", "reason": "price_zero",
                "bot_trade_id": bot_trade_id}
    shares_to_order = round(size_usdc / current_price, 4)

    # Guard mínimo de shares: Polymarket CLOB rechaza órdenes < 5 shares.
    # Usamos 5.5 como target (margen) igual que paper_executor y real_signal_router.
    # Si el precio se movió entre detección y ejecución, el bump del router (hecho
    # con insider_price) puede quedar corto → recalculamos aquí con current_price.
    # 2026-06-10 FIX: antes hacía fail-fast (rejected); ahora bumpeamos igual que
    # paper, evitando perder la señal por un movimiento de precio de 1-2 centavos.
    _MIN_SHARES_POLYMARKET = 5.5
    if shares_to_order < _MIN_SHARES_POLYMARKET:
        _bumped_from_usdc = size_usdc
        try:
            load_dotenv(dotenv_path=_ENV_PATH, override=True)
            _max_t = float(os.getenv('DEFINITIVE_MAX_PER_TRADE', '20'))
        except Exception:
            _max_t = 20.0
        size_usdc = round(min(_MIN_SHARES_POLYMARKET * current_price, _max_t), 2)
        shares_to_order = round(size_usdc / current_price, 4)
        print(f"[EXECUTOR] min_shares_bump ${_bumped_from_usdc}→${size_usdc} "
              f"({shares_to_order:.2f} shares @ {current_price:.4f})")
        # Actualizar size_usdc en BD para coherencia
        conn2 = database.get_connection()
        conn2.execute("UPDATE bot_trades SET size_usdc=? WHERE id=?",
                      (size_usdc, bot_trade_id))
        conn2.commit()
        conn2.close()

    print(f"[EXECUTOR] Enviando orden al CLOB: {side} {size_usdc} USDC "
          f"({shares_to_order} shares) @ {round(current_price, 4)} | "
          f"bot_trade_id={bot_trade_id} | coid={new_coid}")

    order_args = OrderArgs(
        token_id=token_id,
        price=current_price,
        size=shares_to_order,
        side=side,
    )
    # SDK v2 requiere tick_size + neg_risk explícitos en options.
    # tick_size: el SDK lo cachea via get_order_book; pasamos default conservador.
    # neg_risk: auto-detect vía client.get_neg_risk(token_id).
    try:
        client = get_client()
        try:
            neg_risk = client.get_neg_risk(token_id)
        except Exception:
            neg_risk = False
        try:
            tick_size = client.get_tick_size(token_id)
        except Exception:
            tick_size = "0.01"
        order_options = PartialCreateOrderOptions(
            tick_size=tick_size, neg_risk=neg_risk,
        )

        # Envío con: timeout 10s + retry backoff transitorios + retry 401.
        def _send():
            return _with_timeout(
                client.create_and_post_order,
                order_args, options=order_options, order_type=OrderType.GTC,
            )

        try:
            raw_order = _retry_with_backoff(_send, max_retries=3, base_delay=1.0)
        except Exception as first_err:
            if "401" in _sanitize(first_err) or "signer address" in _sanitize(first_err).lower():
                print("[EXECUTOR] Error 401 — credenciales expiradas, renovando...")
                if not refresh_poly_credentials():
                    print("[EXECUTOR] No se pudo renovar credentials tras 401")
                    database.update_trade_status(bot_trade_id, status='failed',
                                                  error="401 + refresh_failed")
                    return {"status": "error", "reason": "clob_auth_failed",
                            "bot_trade_id": bot_trade_id}
                client = get_client(force_refresh=True)

                def _send_retry():
                    return _with_timeout(
                        client.create_and_post_order,
                        order_args, options=order_options, order_type=OrderType.GTC,
                    )
                raw_order = _retry_with_backoff(_send_retry, max_retries=2,
                                                base_delay=1.0)
                print("[EXECUTOR] Retry tras renovar credentials OK")
            else:
                raise

        # Timeout o respuesta vacía
        if raw_order is None:
            print("[EXECUTOR] Timeout o respuesta vacía del CLOB")
            database.update_trade_status(bot_trade_id, status='failed',
                                          error="clob_timeout_or_empty")
            return {"status": "error", "reason": "clob_timeout",
                    "bot_trade_id": bot_trade_id}

        # FIX E — validación robusta
        order = validate_order_response(raw_order)
        if order is None:
            print(f"[EXECUTOR] Orden rechazada/inválida por CLOB: "
                  f"{_sanitize(raw_order)}")
            database.update_trade_status(bot_trade_id, status='rejected',
                                          error=_sanitize(raw_order)[:200])
            return {"status": "error", "reason": "clob_rejected",
                    "bot_trade_id": bot_trade_id, "raw": raw_order}

        # status del CLOB: matched (lleno) | live (en libro) | delayed |
        #                   unmatched | invalid | canceled | expired | otros
        # Fix L16 (2026-05-24): clasificación explícita.
        clob_status = (order.status or "").lower()

        # Estados que indican rechazo/no-ejecución: marcar 'rejected' sin
        # tocar size_filled (queda 0).
        REJECT_STATES = {"unmatched", "invalid", "canceled", "cancelled",
                         "expired", "rejected", "error"}
        if clob_status in REJECT_STATES:
            print(f"[EXECUTOR] Orden rechazada por CLOB (status={order.status})")
            database.update_trade_status(bot_trade_id, status='rejected',
                                          error=f"clob_{clob_status}")
            return {"status": "error", "reason": f"clob_{clob_status}",
                    "bot_trade_id": bot_trade_id}

        # 'matched' → fill confirmado (asumimos fill al precio/shares solicitado;
        # reconciler refinará desde /trades). 'live'/'delayed' → posted, sin
        # fill confirmado todavía; NO inflamos size_filled — el reconciler
        # Pase 2 (refinar fill vía /trades) lo poblará cuando el batch matcher
        # asincrónico complete o partial fills se acumulen.
        # Cualquier status desconocido → tratamos como rejected (fail-closed).
        ACCEPT_STATES = {"matched", "live", "delayed"}
        if clob_status not in ACCEPT_STATES:
            print(f"[EXECUTOR] CLOB status desconocido: {order.status!r} → rejected (fail-closed)")
            database.update_trade_status(bot_trade_id, status='rejected',
                                          error=f"clob_unknown_status_{order.status}")
            return {"status": "error", "reason": "clob_unknown_status",
                    "bot_trade_id": bot_trade_id, "clob_status": order.status}

        price_filled = current_price
        if clob_status == "matched":
            # Fill asumido al precio/shares solicitado.
            size_filled = round(shares_to_order * current_price, 6)
            fill_assumed = True
        else:
            # L23 (2026-05-26): para live/delayed intentamos get_order
            # inmediato. Si CLOB ya muestra MATCHED con shares (caso común,
            # el "delayed" es instantáneo en mercados líquidos), usamos esos
            # datos directos en vez de esperar al reconciler. Reduce gap
            # dashboard de ≤60s a 0s.
            size_filled = 0.0
            fill_assumed = False
            try:
                _o = client.get_order(order.order_id)
                if isinstance(_o, dict):
                    _m = float(_o.get("size_matched") or 0)
                    _p = float(_o.get("price") or 0)
                    _st = (_o.get("status") or "").upper()
                    if _st == "MATCHED" and _m > 0.01 and _p > 0:
                        size_filled = round(_m * _p, 6)
                        price_filled = _p
                        fill_assumed = False  # fill confirmado on-chain
                        print(f"[EXECUTOR] post-check matched immediate: "
                              f"size_filled=${size_filled} px={_p} (L23)")
            except Exception:
                pass
        fees = 0.0

        database.update_trade_filled(
            bot_trade_id=bot_trade_id,
            entry_price=price_filled,
            size_filled=size_filled,
            fees=fees,
            order_id=order.order_id,
            status='open',
        )

        print(f"[EXECUTOR] REAL OK | bot_trade_id={bot_trade_id} | "
              f"clob_status={order.status} | order_id={order.order_id[:16]}... | "
              f"price_assumed={price_filled} | size_assumed={size_filled}")

        # L54 (2026-06-03) — OCO post-fill ELIMINADO.
        # Histórico: L39 (2026-06-01) posteaba LIMIT GTC SELL @ TP y @ SL
        # inmediatamente tras BUY fill. L51 (2026-06-02) descubrió que el SL
        # limit cruzaba inmediatamente con el mid bid (Polymarket CLOB matchea
        # por mejor precio), cerrando trades al instante a entry−1¢.
        # post_oco_brackets quedó como no-op (sl_id=None, tp_id=None).
        # Ahora eliminamos el callsite. TP/SL se gestiona 100% vía
        # fast_price_check_b polling cada 15s + close_position al disparar.

        # ── Single-shot: auto-pausa al alcanzar el límite de sesión ──────
        _check_session_limit()

        return {"status": "ok", "bot_trade_id": bot_trade_id,
                "clob_status": order.status, "order_id": order.order_id,
                "actual_price": price_filled, "size_filled": size_filled,
                "fees": fees, "slippage": round(slippage, 4),
                "_fill_assumed": fill_assumed,
                "transaction_hashes": order.transaction_hashes}

    except Exception as e:
        print(f"[EXECUTOR] Excepción enviando orden: {_sanitize(e)}")
        try:
            risk_manager.record_api_error()
        except Exception:
            pass
        if bot_trade_id is not None:
            database.update_trade_status(bot_trade_id, status='failed',
                                          error=_sanitize(e)[:200])
        return {"status": "error", "reason": "clob_exception",
                "bot_trade_id": bot_trade_id, "error": _sanitize(e)[:200]}


# ─────────────────────────────────────────────────────────────────────
# FIX B — close_position con precio ejecutable (orderbook)
# ─────────────────────────────────────────────────────────────────────

def _best_bid_ask(token_id):
    """
    Lee el orderbook y devuelve (best_bid, best_ask) como floats.
    None en cualquiera si no hay ese lado del libro.
    Lanza la excepción del SDK si la llamada falla (para que el retry actúe).
    """
    client = get_client()
    ob = client.get_order_book(token_id)
    # L58 (2026-06-03) — el SDK puede devolver dict O objeto según versión.
    # Bug histórico: getattr(dict, "bids") siempre devuelve None → close_position
    # fallaba con no_bids aunque el book tuviera 5 bids reales. bt#229 quedó
    # bloqueado horas en SL_HIT por esto.
    if isinstance(ob, dict):
        bids_raw = ob.get("bids") or []
        asks_raw = ob.get("asks") or []
        # Cada entrada es {"price": "0.05", "size": "..."}
        best_bid = max((float(b.get("price", 0)) for b in bids_raw), default=None)
        best_ask = min((float(a.get("price", 1)) for a in asks_raw), default=None)
    else:
        bids = getattr(ob, "bids", None) or []
        asks = getattr(ob, "asks", None) or []
        best_bid = max((float(b.price) for b in bids), default=None)
        best_ask = min((float(a.price) for a in asks), default=None)
    return best_bid, best_ask


def close_position(bot_trade_id, size_usdc_override=None):
    """
    Cierra una posición abierta vendiendo al best_bid del orderbook (no midpoint).

    Fase 3.1:
      - Lee orderbook real con get_order_book.
      - Sin bids → close_failed (no ejecuta).
      - Spread > 10% → close_failed (no ejecuta).
      - Orden al best_bid, transacción atómica (closing → closed/close_failed).
      - NO reintenta automáticamente — el usuario reintenta vía Telegram.
    """
    conn = database.get_connection()
    c = conn.cursor()
    c.execute("""SELECT market_id, token_id, side, size_usdc, size_filled,
                        entry_price, paper_trade
                 FROM bot_trades WHERE id = ?""",
              (bot_trade_id,))
    row = c.fetchone()
    conn.close()

    if not row:
        return {"status": "error", "msg": "trade no encontrado"}
    market_id, token_id, side, size_usdc, size_filled, entry_price, paper_trade = row

    if not token_id:
        return {"status": "error", "msg": "token_id no disponible"}

    if paper_trade:
        print(f"[EXECUTOR] PAPER CLOSE: posición {bot_trade_id} cerrada")
        database.update_trade_status(bot_trade_id, status='closed')
        return {"status": "paper_ok"}

    # Tamaño USDC a cerrar: override proporcional (cierre parcial), fill real, o solicitado.
    size_usdc_to_close = size_usdc_override if size_usdc_override else (size_filled if size_filled else size_usdc)

    # Helper local: registrar fallo de cierre SIN dejar el trade en limbo.
    # Fix 2026-05-21 (agujero negro): close_failed era estado terminal
    # → 8 trades históricos perdieron $-40 por quedar atrapados. Ahora cualquier
    # fallo revierte a status='open' con anotación para que próximo ciclo
    # del fast_price_check_b o reconciler reintente. Hasta cerrar o resolver.
    def _close_attempt_failed(reason_short: str, error_msg: str):
        from datetime import datetime as _dt
        attempt_note = f"close_attempt_{_dt.utcnow().isoformat()}: {error_msg[:140]}"
        database.update_trade_status(bot_trade_id, status='open', error=attempt_note)
        print(f"[EXECUTOR] Close {bot_trade_id} fallido ({reason_short}); "
              f"REVIERTO a 'open' para reintento. Razón: {error_msg[:80]}")
        return {"status": "close_failed", "reason": reason_short,
                "bot_trade_id": bot_trade_id, "error": error_msg[:200]}

    if not entry_price or entry_price <= 0:
        return _close_attempt_failed("invalid_entry_price",
                                      f"entry_price={entry_price}")

    # ── Leer orderbook con retry ──────────────────────────────────────────
    try:
        best_bid, best_ask = _retry_with_backoff(
            lambda: _best_bid_ask(token_id), max_retries=3, base_delay=1.0
        )
    except Exception as e:
        return _close_attempt_failed("orderbook_error", _sanitize(e))

    # ── Sin bids → no se puede vender ─────────────────────────────────────
    if best_bid is None:
        return _close_attempt_failed("no_bids", "no_bids_in_orderbook")

    # ── Spread excesivo → no ejecutar ─────────────────────────────────────
    # L58b (2026-06-03) — usar spread ABSOLUTO también, no solo %.
    # En extremos (bid 0.05 ask 0.06), spread 20% relativo pero solo $0.01
    # absoluto. Bloquear esto perdía la venta legítima (bt#229 caso).
    # Política: aceptar si spread abs ≤ $0.02 INDEPENDIENTEMENTE del %,
    # o si spread% ≤ MAX_CLOSE_SPREAD (lo histórico).
    if best_ask is not None and best_bid > 0:
        spread_abs = best_ask - best_bid
        spread_pct = spread_abs / best_bid
        if spread_pct > MAX_CLOSE_SPREAD and spread_abs > 0.05:
            return _close_attempt_failed("spread_too_wide",
                                          f"spread_too_wide: {spread_pct:.4f} abs={spread_abs:.4f}")

    # ── Ejecutar venta al best_bid ────────────────────────────────────────
    close_side = "SELL" if str(side).upper() in ("BUY", "YES") else "BUY"
    database.update_trade_status(bot_trade_id, status='closing')

    # CLOB v2: OrderArgs.size = SHARES. Convertimos USDC a shares con entry_price.
    # Usar floor (no round) para evitar "not enough balance" por redondeo al alza.
    import math as _math
    shares_to_sell = _math.floor(size_usdc_to_close / entry_price * 100) / 100

    order_args = OrderArgs(
        token_id=token_id,
        price=best_bid,
        size=shares_to_sell,
        side=close_side,
    )

    try:
        client = get_client()
        raw_order = _with_timeout(client.create_and_post_order, order_args)
        if raw_order is None:
            return _close_attempt_failed("clob_timeout", "clob_timeout")

        order = validate_order_response(raw_order)
        if order is None or order.status == "unmatched":
            return _close_attempt_failed("clob_rejected", _sanitize(raw_order))

        # L81 FIX — usar _persist_trade_close en lugar de update_trade_status.
        # update_trade_status solo cambia el status — deja pnl_usdc=NULL y
        # outcome=NULL para siempre (el reconciler ya no procesa status='closed').
        # _persist_trade_close escribe status + pnl_usdc + outcome de una vez,
        # con el guard de race-condition integrado.
        _ep = float(entry_price) if entry_price and float(entry_price) > 0 else 0
        if _ep > 0 and size_usdc_to_close > 0:
            _shares_sold = size_usdc_to_close / _ep
            _recv   = round(_shares_sold * best_bid, 6)
            _pnl    = round(_recv - size_usdc_to_close, 4)
            _outcome = 'won' if _pnl >= 0 else 'lost'
            _persist_trade_close(
                bot_trade_id, _outcome, _pnl,
                source='close_position',
                extra_note=f'close@{best_bid:.4f} clob={order.status}'
            )
        else:
            # fallback para edge case (entry_price inválido)
            _persist_trade_close(bot_trade_id, 'lost', 0.0,
                                 source='close_position_fallback',
                                 extra_note='entry_price_invalid')

        print(f"[EXECUTOR] Posición {bot_trade_id} cerrada @ {best_bid} | "
              f"clob_status={order.status} | order_id={order.order_id[:16]}...")
        return {"status": "ok", "close_price": best_bid,
                "clob_status": order.status, "order_id": order.order_id}
    except Exception as e:
        try:
            risk_manager.record_api_error()
        except Exception:
            pass
        return _close_attempt_failed("clob_exception", _sanitize(e))


# ─────────────────────────────────────────────────────────────────────
# Fase 3.2 — Reconciliación de fills reales
# ─────────────────────────────────────────────────────────────────────
# Fase 3.1 dejó los fills como "asumidos" (_fill_assumed): la respuesta de
# POST /order no incluye precio/size exacto. Esta función consulta el
# endpoint /trades del CLOB por order_id y actualiza con los valores reales.

def _check_market_resolved(condition_id: str, token_id: str):
    """
    Comprueba si un market está resuelto y devuelve el precio final
    del token que tenemos.

    Returns:
        float in [0.0, 1.0] si está resuelto → precio final de nuestro token.
        None si no está resuelto o si la consulta falla.
    """
    try:
        r = requests.get(
            f"https://clob.polymarket.com/markets/{condition_id}",
            timeout=10,
        )
        if r.status_code != 200:
            return None
        d = r.json() or {}
        closed = bool(d.get("closed"))
        if not closed:
            return None
        tok_str = str(token_id)
        for t in d.get("tokens", []):
            if str(t.get("token_id")) == tok_str:
                # Usar winner (booleano) si está disponible — es el precio real de resolución.
                # t.get("price") es el precio CLOB del momento, que puede estar en tránsito
                # (e.g. 0.44) y provocaría un PnL incorrecto. Las shares se redimen a 1.0/0.0.
                winner = t.get("winner")
                if winner is True:
                    return 1.0
                if winner is False:
                    return 0.0
                # winner=None → mercado cerrado pero sin ganador declarado aún, esperar
                return None
        return None
    except Exception as e:
        print(f"[RECONCILE] _check_market_resolved error: {_sanitize(e)}")
        return None


def _persist_trade_close(bt_id: int, outcome: str, pnl_usdc: float,
                          source: str, extra_note: str = "") -> bool:
    """L54 (2026-06-03) — punto ÚNICO de cierre de trades reales.

    Sustituye a 4 sitios dispersos que escribían `status='closed', pnl_usdc=?`:
      - _mark_resolved_closed (market resuelto)
      - _close_via_activity_log (sells encontrados en activity)
      - poll_sell normal (limit sell fill)
      - poll_sell OCO TP/SL (deprecated post-L51)

    Centraliza el guard contra estados terminales (L21) y el formato del
    error note para diagnostics. Devuelve True si efectivamente cerró,
    False si otro loop ya lo había cerrado.
    """
    from datetime import datetime as _dt
    conn = database.get_connection()
    note = f" | {source}_{_dt.utcnow().isoformat()}"
    if extra_note:
        note += f": {extra_note}"
    c = conn.execute(
        """UPDATE bot_trades
           SET status='closed', outcome=?, pnl_usdc=?,
               error=COALESCE(error,'') || ?,
               closed_at=COALESCE(closed_at, ?)
           WHERE id=? AND status NOT IN ('closed','rejected','failed')""",
        (outcome, round(pnl_usdc, 4), note, _dt.utcnow().isoformat(), bt_id))
    affected = c.rowcount
    conn.commit()
    conn.close()
    if affected == 0:
        print(f"[CLOSE/{source}] bt#{bt_id} race-skip · trade ya terminal")
    else:
        print(f"[CLOSE/{source}] bt#{bt_id} pnl=${pnl_usdc:+.4f} outcome={outcome}")
    return affected > 0


def _mark_resolved_closed(bot_trade_id: int, outcome: str, pnl_usdc: float,
                          exit_price: float) -> bool:
    """Wrapper retrocompatible. Usa _persist_trade_close."""
    return _persist_trade_close(bot_trade_id, outcome, pnl_usdc,
                                  source="resolved",
                                  extra_note=f"exit_price={exit_price}")


def reconcile_real_trades():
    """
    Reconcilia trades reales abiertos. Dos pases:

    1) Detección de market resuelto: para cada trade abierto, query CLOB
       /markets/{cid}. Si closed=True → calcula PnL final usando el precio
       resuelto del token (1.0 ganador / 0.0 perdedor) y marca status='closed'
       con outcome='won'/'lost' y pnl_usdc en USDC nominal. Esto cierra el
       ciclo completo aunque el orderbook ya no exista (caso del trade #6).

    2) Refinado de fill (precio/size/fees) vía endpoint /trades del CLOB para
       trades aún abiertos (no resueltos).

    Pensado para correr cada 5 min en un loop async. No lanza excepciones.
    """
    from py_clob_client_v2.clob_types import TradeParams
    conn = database.get_connection()
    c = conn.cursor()
    # Fase 4 fix — incluir también 'close_failed': cuando close_position falla
    # vía orderbook (no bids, spread amplio, market resuelto), el trade queda
    # con status='close_failed' pero las shares siguen on-chain. Si el market
    # luego resuelve, el reconciler debe cerrarlo. Bug histórico 2026-05-20:
    # 16 trades en limbo close_failed inflaban capital_open ficticio en el
    # dashboard y no contaban en wins/losses.
    # Alerta proactiva: warning si close_failed acumula >5
    cf_count = c.execute("SELECT COUNT(*) FROM bot_trades "
                          "WHERE paper_trade=0 AND status='close_failed'").fetchone()[0]
    if cf_count >= 5:
        print(f"[RECONCILE] ⚠ {cf_count} trades en close_failed — revisar orderbook "
              f"o lógica de cierre. Bug histórico 2026-05-20 documentado en context.md")

    # L81 FIX: incluir 'closing' — trades que quedaron en ese estado si el
    # proceso se cayó entre update_trade_status('closing') y la respuesta del
    # CLOB. El reconciler los trata igual que 'open': si el mercado resolvió
    # los cierra; si hay sells en activity API los reconcilia.
    # C-4 FIX (2026-06-10): incluir executed_at para filtrar SELLs históricos
    # en _close_via_activity_log. Sin este filtro, la activity API puede devolver
    # SELLs de posiciones anteriores en el mismo mercado → PnL inflado/incorrecto.
    c.execute("""SELECT id, order_id, size_usdc, size_filled, entry_price,
                        market_id, token_id, fees, executed_at
                 FROM bot_trades
                 WHERE paper_trade=0 AND status IN ('open','close_failed','closing')""")
    rows = c.fetchall()
    conn.close()
    if not rows:
        return {"checked": 0, "reconciled": 0, "resolved": 0}

    reconciled = 0
    resolved = 0
    via_activity = 0
    for (bt_id, order_id, size_usdc, size_filled, entry_price,
         market_id, token_id, fees, executed_at) in rows:

        # ── Pase 0: ¿shares=0 on-chain pero status='open'? ───────────────
        # Detección de "fill perdido": cerramos via limit-sell que devolvió
        # 'delayed' pero realmente matcheó. Las shares ya no están en el
        # wallet. Reconstruimos PnL desde activity API. (Bug 2026-05-23)
        try:
            closed_via_act = _close_via_activity_log(
                bt_id, market_id, token_id, size_filled or size_usdc,
                entry_price, fees, executed_at=executed_at)
            if closed_via_act:
                via_activity += 1
                continue
        except Exception as e:
            print(f"[RECONCILE] activity-log check bt={bt_id} err: {_sanitize(e)}")

        # ── Pase 1: ¿market resuelto? ────────────────────────────────────
        try:
            final_price = _check_market_resolved(market_id, token_id)
        except Exception:
            final_price = None

        if final_price is not None:
            # Market resuelto. Calcular PnL usando size_filled (USDC nominal).
            cost_usdc = float(size_filled or size_usdc or 0)
            ep = float(entry_price or 0)
            if ep <= 0 or cost_usdc <= 0:
                print(f"[RECONCILE] bt={bt_id} resuelto pero entry_price/size "
                      f"inválidos (ep={ep}, size={cost_usdc}); skip")
                continue
            shares = cost_usdc / ep
            final_value = shares * final_price
            fees_val = float(fees or 0.0)
            pnl = final_value - cost_usdc - fees_val
            outcome = "won" if final_price >= 0.5 else "lost"
            _mark_resolved_closed(bt_id, outcome, pnl, final_price)
            resolved += 1
            print(f"[RECONCILE] bt={bt_id} market RESUELTO: token_price="
                  f"{final_price} outcome={outcome} cost=${cost_usdc:.2f} "
                  f"shares={shares:.4f} pnl=${pnl:.4f}")
            continue

        # ── Pase 2: refinar fill vía /trades (sólo si order_id presente) ─
        if not order_id:
            continue
        try:
            client = get_client()
            trades = _retry_with_backoff(
                lambda: client.get_trades(TradeParams(id=order_id)),
                max_retries=2, base_delay=1.0,
            )
            # Fix L19/L22 (2026-05-24/26): /trades a veces devuelve vacío
            # aunque la orden esté MATCHED on-chain. Fallback a get_order
            # para refinar size_filled desde size_matched + price.
            if not trades or not isinstance(trades, list):
                try:
                    o = client.get_order(order_id) if order_id else None
                    if isinstance(o, dict):
                        matched = float(o.get("size_matched") or 0)
                        px = float(o.get("price") or entry_price or 0)
                        st = (o.get("status") or "").upper()

                        # L22 (2026-05-26): MATCHED con size_matched > 0
                        # pero size_filled DB = 0 → actualizar con fill real
                        if matched > 0.01 and (size_filled or 0) < 0.01 and px > 0:
                            real_size = round(matched * px, 6)
                            database.update_trade_filled(
                                bot_trade_id=bt_id,
                                entry_price=px,
                                size_filled=real_size, fees=0.0,
                                order_id=order_id, status='open',
                            )
                            print(f"[RECONCILE] bt={bt_id} size_filled actualizado "
                                  f"desde get_order: matched={matched} px={px} "
                                  f"→ size_filled=${real_size} (L22)")
                            continue

                        # L19: LIVE con 0 matched + size_filled inflado → zerar
                        if matched < 0.01 and (size_filled or 0) > 0 and st in ("LIVE", "DELAYED"):
                            database.update_trade_filled(
                                bot_trade_id=bt_id,
                                entry_price=entry_price,
                                size_filled=0.0, fees=0.0,
                                order_id=order_id, status='open',
                            )
                            print(f"[RECONCILE] bt={bt_id} size_filled zerado "
                                  f"(CLOB {st}, 0 matched) — fix L19 retroactivo")
                except Exception:
                    pass
                continue
            if not trades or not isinstance(trades, list):
                continue
            total_size = 0.0
            total_cost = 0.0
            total_fees = 0.0
            for t in trades:
                try:
                    px = float(t.get("price", 0))
                    sz = float(t.get("size", 0))
                    total_size += sz
                    total_cost += px * sz
                    total_fees += float(t.get("fee", t.get("fee_rate_bps", 0)) or 0)
                except (TypeError, ValueError):
                    continue
            if total_size <= 0:
                continue
            avg_price = total_cost / total_size
            # size_filled almacenado como USDC nominal (= cost) para
            # consistencia con execute_trade y close_position.
            database.update_trade_filled(
                bot_trade_id=bt_id,
                entry_price=round(avg_price, 6),
                size_filled=round(total_cost, 6),
                fees=round(total_fees, 6),
                order_id=order_id,
                status='open',
            )
            reconciled += 1
            print(f"[RECONCILE] bt={bt_id}: avg_price={avg_price:.4f} "
                  f"shares={total_size:.4f} cost=${total_cost:.4f} "
                  f"fees={total_fees}")
        except Exception as e:
            print(f"[RECONCILE] error en bt={bt_id}: {_sanitize(e)}")
            continue
    # Fase 4.1 — DESACTIVADO: el auto-transfer dw→proxy causaba doble conteo
    # en equity (pUSD swept al CLOB se seguía contando en proxy cache).
    # Redimir directamente con redeem_standard_ct.py y redeem_closed_positions.py.

    print(f"[RECONCILE] ciclo completo: checked={len(rows)} resolved={resolved} "
          f"reconciled={reconciled} via_activity={via_activity}")
    return {"checked": len(rows), "reconciled": reconciled,
            "resolved": resolved, "via_activity": via_activity}


def _close_via_activity_log(bt_id: int, market_id: str, token_id: str,
                             cost_usdc: float, entry_price: float,
                             fees: float, executed_at: str | None = None) -> bool:
    """Detecta fills perdidos: si shares on-chain = 0 y hay un SELL reciente
    en /activity para nuestra DW + este market, reconstruye PnL y cierra.

    Returns True si efectivamente reconcilió.
    Bug origen: CLOB devuelve status='delayed' para órdenes en batch matcher;
    nuestro código lo trataba como 'unmatched' pero el batch sí matcheaba
    segundos después, dejando trades open con 0 shares y posición fantasma.
    """
    if not token_id or not market_id:
        return False
    try:
        from web3 import Web3 as _W3
        _w3 = _W3(_W3.HTTPProvider("https://polygon-bor-rpc.publicnode.com",
                                    request_kwargs={"timeout": 6}))
        CTF = _W3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
        dw = _W3.to_checksum_address(config.DEPOSIT_WALLET_ADDRESS or "0x0")
        proxy = _W3.to_checksum_address(config.POLY_WALLET_ADDRESS or "0x0")
        abi = [{"name":"balanceOf","type":"function","inputs":[
                 {"name":"a","type":"address"},{"name":"i","type":"uint256"}],
                "outputs":[{"name":"","type":"uint256"}],"stateMutability":"view"}]
        ctf = _w3.eth.contract(address=CTF, abi=abi)
        b_dw = ctf.functions.balanceOf(dw, int(token_id)).call() / 1e6
        b_px = ctf.functions.balanceOf(proxy, int(token_id)).call() / 1e6
    except Exception as _e:
        print(f"[RECONCILE] _close_via_activity_log web3_error bt={bt_id}: {_e}")
        return False
    onchain_total = b_dw + b_px
    # Fix L20 (2026-05-24): comparar contra shares esperadas, no umbral
    # absoluto. Partial fills dejan dust (ej. 0.03 de 23.8 = 0.13%).
    # Antes el threshold 0.01 absoluto bloqueaba el rescate.
    expected = float(cost_usdc) / float(entry_price) if entry_price > 0 else 0
    if expected > 0 and onchain_total > 0.05 * expected:
        return False  # >5% de la posición → no fue venta efectiva
    if expected <= 0 and onchain_total > 0.5:
        return False  # fallback: si no podemos calcular esperadas, dust < 0.5

    # 0 shares + status='open' → debe haber un SELL no registrado
    import requests as _rq
    try:
        url = (f"https://data-api.polymarket.com/activity?user={dw.lower()}"
               f"&market={market_id}&limit=20")
        acts = _rq.get(url, timeout=8).json() or []
    except Exception as _e:
        print(f"[RECONCILE] _close_via_activity_log activity_api_error bt={bt_id}: {_e}")
        return False
    # Filtramos SELLs y agregamos (puede haber fills parciales múltiples)
    # C-4 FIX (2026-06-10): filtrar por timestamp >= executed_at del trade.
    # Sin este filtro, SELLs de posiciones anteriores en el mismo mercado
    # (el bot pudo haber operado el mismo market varias veces) inflan el PnL.
    _exec_ts = None
    if executed_at:
        try:
            from datetime import datetime as _dt
            _exec_ts = _dt.fromisoformat(str(executed_at).replace('Z', ''))
        except Exception:
            pass
    sells = []
    for a in acts:
        if a.get("type") != "TRADE":
            continue
        if str(a.get("side", "")).upper() != "SELL":
            continue
        # Filtro temporal: solo SELLs DESPUÉS de que abrimos la posición
        if _exec_ts is not None:
            try:
                act_ts_raw = a.get("timestamp") or a.get("createdAt") or ""
                act_ts = _dt.fromisoformat(str(act_ts_raw).replace('Z', ''))
                if act_ts < _exec_ts:
                    continue  # SELL anterior a nuestra posición — ignorar
            except Exception:
                pass  # si no podemos parsear, incluir (conservador)
        sells.append(a)
    if not sells:
        return False
    total_recv = sum(float(a.get("usdcSize") or 0) for a in sells)

    # L48 (2026-06-02) — FIX duplicado por token.
    # Bug histórico (desde 16-may): cuando hay N bot_trades del MISMO
    # token cerrados en la misma ventana, reconcile_via_activity asigna
    # el `total_recv` COMPLETO a cada trade individual → pnl_usdc
    # multiplicado por N. Causa: la activity API devuelve TODOS los sells
    # del wallet sobre ese mercado, sin filtro por trade.
    #
    # Fix: dividir total_recv entre el número de bot_trades activos
    # de ese token (open + el actual que cerramos ahora). Si solo somos
    # nosotros, factor = 1 (sin cambio). Si hay N hermanos abiertos,
    # cada uno se queda con su fracción proporcional al size_usdc.
    try:
        _con = database.get_connection()
        _siblings = _con.execute(
            """SELECT id, size_usdc FROM bot_trades
               WHERE token_id=? AND paper_trade=0 AND agent_id IN ('B','DEF_COPY')
                 AND (status='open' OR id=?)""",
            (token_id, bt_id)).fetchall()
        _con.close()
        if len(_siblings) > 1:
            _total_size = sum(float(s[1] or 0) for s in _siblings) or 1.0
            _self_size = float(cost_usdc or 0)
            _share_factor = _self_size / _total_size
            total_recv_full = total_recv
            total_recv = total_recv_full * _share_factor
            print(f"[RECONCILE L48] bt={bt_id} token shared by "
                  f"{len(_siblings)} trades → recv ${total_recv_full:.2f} × "
                  f"{_share_factor:.3f} = ${total_recv:.2f}")
    except Exception as _e_l48:
        print(f"[RECONCILE L48] err: {_e_l48}")

    avg_px_w = (sum(float(a.get("price") or 0) * float(a.get("size") or 0)
                     for a in sells)
                / max(1e-9, sum(float(a.get("size") or 0) for a in sells)))
    cost = float(cost_usdc or 0)
    fees_val = float(fees or 0)
    pnl = round(total_recv - cost - fees_val, 4)
    outcome = "won" if pnl >= 0 else "lost"
    return _persist_trade_close(
        bt_id, outcome, pnl,
        source="activity_reconcile",
        extra_note=f"sells={len(sells)} recv=${total_recv:.4f} avg_sell={avg_px_w:.4f}")


# ─────────────────────────────────────────────────────────────────────
# Redeem helper — sólo informativo (Polymarket auto-redime via relayer)
# ─────────────────────────────────────────────────────────────────────
# Para shares ganadoras post-resolución, Polymarket ejecuta auto-redeem
# vía su relayer dentro de unas horas/días. Aquí solo consultamos la
# situación on-chain para diagnóstico. Si se quisiera forzar manualmente,
# habría que invocar CTF.redeemPositions vía POST /submit type=WALLET.

def _cancel_open_orders_for_token(client, token_id: str) -> int:
    """Cancela órdenes propias abiertas sobre este token. Libera shares
    bloqueadas para repostear con precio actualizado. Devuelve nº canceladas.
    Tolera ausencia de get_orders en el SDK (intenta varios métodos)."""
    cancelled = 0
    orders = None
    for attr in ("get_open_orders", "get_orders"):
        fn = getattr(client, attr, None)
        if fn is None:
            continue
        try:
            orders = fn()
            break
        except Exception:
            continue
    if not orders:
        return 0
    for o in orders:
        try:
            oid  = o.get("id") if isinstance(o, dict) else getattr(o, "id", None)
            asset = (o.get("asset_id") if isinstance(o, dict)
                     else getattr(o, "asset_id", None))
            if asset and str(asset) == str(token_id) and oid:
                try:
                    client.cancel(oid)
                    cancelled += 1
                except Exception:
                    pass
        except Exception:
            continue
    return cancelled


def _best_bid_for(client, token_id: str) -> float | None:
    """Mejor precio de compra del orderbook (lo que recibirías al vender)."""
    try:
        book = client.get_order_book(token_id=token_id)
        bids = book.bids if hasattr(book, "bids") else book.get("bids", [])
        if not bids:
            return None
        def _px(b):
            return float(getattr(b, "price", None) or b.get("price"))
        return max(_px(b) for b in bids)
    except Exception:
        return None


def _onchain_share_balance(token_id: str) -> float | None:
    """Suma de balance CTF de DW + Proxy para este token, en shares (1e6)."""
    try:
        from web3 import Web3 as _W3
        w3 = None
        for u in ("https://polygon-bor-rpc.publicnode.com",
                  "https://polygon-rpc.com", "https://rpc.ankr.com/polygon"):
            try:
                w3 = _W3(_W3.HTTPProvider(u, request_kwargs={"timeout":5}))
                w3.eth.block_number; break
            except Exception:
                w3 = None
        if w3 is None: return None
        CTF = _W3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
        dw  = _W3.to_checksum_address(config.DEPOSIT_WALLET_ADDRESS or "0x0")
        pxa = _W3.to_checksum_address(config.POLY_WALLET_ADDRESS or "0x0")
        abi = [{"name":"balanceOf","type":"function","inputs":[
                 {"name":"a","type":"address"},{"name":"i","type":"uint256"}],
                "outputs":[{"name":"","type":"uint256"}],"stateMutability":"view"}]
        ctf = w3.eth.contract(address=CTF, abi=abi)
        b1 = ctf.functions.balanceOf(dw,  int(token_id)).call()
        b2 = ctf.functions.balanceOf(pxa, int(token_id)).call()
        return (b1 + b2) / 1e6
    except Exception:
        return None


def _post_limit_sell_for_close(bt_id: int, token_id: str, entry_price: float,
                                side: str, target_price: float) -> dict:
    """
    Fallback anti-agujero-negro v3 (2026-05-23 17:00):
    Cuando close_position falla por orderbook, publicamos LIMIT propia.

    Cambios vs v2:
      - **Cancela órdenes previas** sobre este token (libera shares bloqueadas
        en una limit anterior que no llegó a fillar).
      - **Precio agresivo**: usa max(best_bid, target_price * 0.95). Si SL
        ya fue cruzado, el target queda obsoleto — perseguimos el bid actual.
      - **Tamaño = on-chain shares disponibles**, no el size original (que
        ignora fills parciales y órdenes locked).

    Returns dict {status: 'posted'|'filled'|'unmatched'|'error', order_id?, ...}
    """
    close_side = "SELL" if str(side).upper() in ("BUY", "YES") else "BUY"

    try:
        client = get_client()
    except Exception as e:
        return {"status": "error", "reason": "no_client", "msg": str(e)}

    # Paso 1: cancelar órdenes previas sobre este token (libera shares).
    try:
        n_cancelled = _cancel_open_orders_for_token(client, token_id)
        if n_cancelled:
            print(f"[FALLBACK] bt#{bt_id} canceladas {n_cancelled} órdenes previas")
            time.sleep(1.5)  # dar tiempo al CLOB a liberar el lock
    except Exception:
        n_cancelled = 0

    # Paso 2: shares realmente disponibles on-chain.
    onchain = _onchain_share_balance(token_id)
    if onchain is None or onchain < 0.5:
        return {"status": "error", "reason": "no_shares_onchain",
                "msg": f"onchain={onchain}"}
    shares_to_sell = round(onchain * 0.999, 4)  # margen 0.1% por float

    # Paso 3: precio de venta — perseguir el bid si el target ya fue cruzado.
    best_bid = _best_bid_for(client, token_id)
    if best_bid and best_bid > 0:
        # Si SL ya cruzado (bid << target), usar bid para fillar inmediato.
        # Si TP aún válido (bid >= target), mantener target (mejor margen).
        effective = max(best_bid, target_price * 0.95)
        # Si effective sigue por encima del best_bid, post como maker. Si
        # estamos por debajo del best_bid, post sería rejected (price < bid
        # cruza el spread y matchea). Tomamos best_bid - 1 tick para asegurar
        # postable o matched.
        px = min(effective, best_bid)
    else:
        px = target_price
    px = max(0.01, min(0.99, round(px, 2)))

    try:
        try:
            neg_risk = client.get_neg_risk(token_id)
        except Exception:
            neg_risk = False
        try:
            tick_size = client.get_tick_size(token_id)
        except Exception:
            tick_size = "0.01"
        order_options = PartialCreateOrderOptions(
            tick_size=tick_size, neg_risk=neg_risk,
        )
        order_args = OrderArgs(token_id=token_id, price=px,
                                size=shares_to_sell, side=close_side)
        print(f"[FALLBACK] bt#{bt_id} post {close_side} {shares_to_sell} @ {px} "
              f"(target={target_price}, bid={best_bid}, onchain={onchain:.2f})")
        raw = _with_timeout(
            client.create_and_post_order,
            order_args, options=order_options, order_type=OrderType.GTC,
        )
        if raw is None:
            return {"status": "error", "reason": "clob_timeout"}
        order = validate_order_response(raw)
        if order is None:
            return {"status": "error", "reason": "rejected",
                    "raw": _sanitize(raw)[:200]}
        # status del CLOB: 'matched'|'live'|'unmatched'
        if order.status == "matched":
            # L81 FIX: mismo patrón que close_position — usar _persist_trade_close
            # para registrar pnl_usdc y outcome. update_trade_status solo cambiaba
            # el status y dejaba pnl_usdc=NULL + outcome=NULL para siempre, igual
            # que el bug crítico original de close_position.
            _cost  = round(shares_to_sell * entry_price, 6)
            _recv  = round(shares_to_sell * px, 6)
            _pnl   = round(_recv - _cost, 4)
            _outcome = 'won' if _pnl >= 0 else 'lost'
            _persist_trade_close(
                bt_id, _outcome, _pnl,
                source='fallback_limit_sell',
                extra_note=f'fill@{px:.4f} sh={shares_to_sell}',
            )
            return {"status": "filled", "order_id": order.order_id,
                    "fill_price": px}
        # 2026-05-23 — el CLOB devuelve "delayed" para órdenes que entran
        # en batch matching asíncrono (~5s antes de transicionar a live).
        # Tratamos delayed igual que live (queda posted, esperando match).
        if order.status in ("live", "delayed"):
            # Orden en libro esperando match — guardar FULL order_id para que
            # poll_pending_sells() pueda consultar al CLOB y cerrar el trade
            # cuando filla (sin depender de grace period ni activity API).
            from datetime import datetime as _dt
            conn = database.get_connection()
            c = conn.cursor()
            # Guard: solo marcar si trade aún está open (no si otro loop ya cerró)
            c.execute("""UPDATE bot_trades SET error=?
                         WHERE id=? AND status='open'""",
                      (f"limit_sell_posted_{_dt.utcnow().isoformat()}: "
                       f"order_id={order.order_id} px={px} sh={shares_to_sell}",
                       bt_id))
            conn.commit()
            conn.close()
            return {"status": "posted", "order_id": order.order_id, "price": px}
        return {"status": "unmatched", "reason": order.status}
    except Exception as e:
        return {"status": "error", "reason": "exception", "msg": _sanitize(e)}


def poll_pending_sells() -> dict:
    """Fase L18 (2026-05-24) — anti-fantasma activo.

    Cuando _post_limit_sell_for_close postea una SELL al CLOB, guardamos el
    full order_id en bot_trades.error. Esta función:
      1. Escanea open trades con marca 'limit_sell_posted_'
      2. Para cada uno: query directo a CLOB get_order(order_id)
      3. Si MATCHED → cierra el trade con PnL = (price × size_matched) - cost
      4. Si CANCELED/EXPIRED → limpia el marker, deja status='open' para retry

    Bypaseamos grace period y activity API. Cierre inmediato cuando filla.
    """
    import re as _re
    conn = database.get_connection()
    c = conn.cursor()
    c.execute("""SELECT id, size_filled, entry_price, error
                 FROM bot_trades
                 WHERE paper_trade=0 AND status='open'
                   AND error LIKE 'limit_sell_posted_%'""")
    rows = c.fetchall()
    conn.close()
    out = {"checked": 0, "filled": 0, "still_live": 0, "stale": 0, "errors": 0}
    if not rows:
        return out

    try:
        client = get_client()
    except Exception:
        out["errors"] = len(rows)
        return out

    for (bt_id, cost, entry, err) in rows:
        out["checked"] += 1
        m = _re.search(r"order_id=(0x[0-9a-fA-F]+)", err or "")
        if not m:
            continue
        oid = m.group(1)
        try:
            o = client.get_order(oid)
        except Exception:
            out["errors"] += 1
            continue
        if not isinstance(o, dict):
            continue
        st = (o.get("status") or "").upper()
        matched = float(o.get("size_matched") or 0)
        price = float(o.get("price") or 0)

        if st == "MATCHED" or (st in ("LIVE", "DELAYED") and matched > 0.01):
            # Cierre confirmado (full o partial fill significativo)
            recv = round(matched * price, 4)
            pnl = round(recv - float(cost or 0), 4)
            outcome = "won" if pnl >= 0 else "lost"
            if _persist_trade_close(bt_id, outcome, pnl,
                    source="poll_sell",
                    extra_note=f"order={oid[:18]} clob={st} recv={recv} cost={cost}"):
                out["filled"] += 1
            continue

        if st in ("INVALID", "CANCELED", "CANCELLED", "EXPIRED", "REJECTED"):
            # Limpia marker, deja status='open' para que próximo TP/SL retry.
            # Aquí NO aplicamos guard terminal porque solo tocamos `error`,
            # no transicionamos status — y el WHERE filtra ya status='open'.
            from datetime import datetime as _dt
            conn = database.get_connection()
            conn.execute("""UPDATE bot_trades
                            SET error=? WHERE id=? AND status='open'""",
                         (f"poll_sell_stale_{_dt.utcnow().isoformat()}: "
                          f"order={oid[:18]}… clob_status={st} - retry posible",
                          bt_id))
            conn.commit()
            conn.close()
            out["stale"] += 1
            print(f"[POLL_SELL] bt#{bt_id} marker limpio · clob={st} (orden no fillará)")
            continue

        # LIVE/DELAYED sin matched suficiente → sigue esperando
        out["still_live"] += 1

    # L54 (2026-06-03) — Polling de OCO brackets ELIMINADO.
    # Después de remover post_oco_brackets, ningún trade nuevo tiene
    # oco_*_order_id. Trades históricos con OCOs vivos se cierran via
    # fast_price_check_b o reconciler. Si quedaran órdenes huérfanas en
    # CLOB, no afectan al PnL: el reconciler captura el sell on-chain.
    return out


def fast_price_check_b() -> dict:
    """
    Fase 4 — chequeo rápido de TP/SL para posiciones reales del agente B.
    Diseñado para correr cada 30s y reducir latencia de cierre.

    Lógica:
      - Solo trades reales abiertos con executed_at >= B_PRICE_CHECK_FROM
      - Lee TP/SL multipliers del agente activo (config.REAL_TRADING_AGENT)
        en la tabla `agents`
      - Si el agente actual no tiene TP/SL (NULL → hold-to-resolve como C),
        retorna sin hacer nada
      - Para cada posición: query midpoint CLOB del token, comparar contra
        tp_price = entry × tp_mul y sl_price = entry × sl_mul
      - Si cruza → dispara close_position(bot_trade_id)

    Returns dict {checked, closed_tp, closed_sl, skipped}.
    """
    agent_id = config.REAL_TRADING_AGENT
    if agent_id == "NONE":
        return {"checked": 0, "skipped": "agent_NONE"}

    # Leer config TP/SL del agente activo
    conn = database.get_connection()
    c = conn.cursor()
    c.execute("SELECT tp_multiplier, sl_multiplier FROM agents WHERE id=?", (agent_id,))
    row = c.fetchone()
    if not row or row[0] is None or row[1] is None:
        conn.close()
        return {"checked": 0, "skipped": f"agent_{agent_id}_no_tpsl"}
    tp_mul, sl_mul = float(row[0]), float(row[1])

    cutoff = os.getenv("B_PRICE_CHECK_FROM", "2026-05-18T19:00:00")
    # 2026-05-28: filtrar agent_id='B' explícitamente — agente D es
    # hold-to-resolve, NO debe procesarse por TP/SL.
    c.execute("""SELECT id, market_id, token_id, side, entry_price
                 FROM bot_trades
                 WHERE paper_trade=0 AND status='open'
                   AND executed_at >= ?
                   AND token_id IS NOT NULL AND entry_price > 0
                   AND COALESCE(agent_id, 'B') = 'B'""",
              (cutoff,))
    rows = c.fetchall()
    conn.close()

    if not rows:
        return {"checked": 0, "skipped": "no_open_after_cutoff"}

    closed_tp = 0
    closed_sl = 0
    checked = 0
    for bt_id, market_id, token_id, side, entry in rows:
        try:
            # Fix divergencia paper-real 2026-05-24:
            # Antes usábamos midpoint para detectar TP/SL, pero paper B usa
            # best_bid (precio ejecutable al vender). Resultado: paper cerraba
            # con best_bid 0.64 mientras real ignoraba porque midpoint 0.635.
            # Ahora ambos usan best_bid → detección sincronizada.
            #
            # L57 (2026-06-03) — retry SSL/conn errors (bt#229 caso: blackout
            # SSL durante caída brusca → bot perdió visibilidad → SL no
            # disparó hasta que precio ya pasado a 0.05). 3 reintentos con
            # backoff exponencial corto antes de saltar la iteración.
            r = None
            for _attempt in range(3):
                try:
                    r = requests.get(
                        f"https://clob.polymarket.com/book?token_id={token_id}",
                        timeout=5,
                    )
                    if r.status_code == 200:
                        break
                except Exception as _re:
                    if _attempt == 2:
                        print(f"[PRICE_CHECK_{agent_id}] bt#{bt_id} 3 retries fallaron: {str(_re)[:80]}")
                    else:
                        time.sleep(0.5 * (_attempt + 1))
            if r is None or r.status_code != 200:
                continue
            book = r.json() or {}
            bids = book.get("bids") or []
            current = None
            if bids:
                try:
                    current = max(float(b.get("price")) for b in bids
                                  if b.get("price") is not None)
                except Exception:
                    pass
            if current is None:
                # Fallback midpoint si /book no devolvió bids
                rm = requests.get(
                    f"https://clob.polymarket.com/midpoint?token_id={token_id}",
                    timeout=5,
                )
                if rm.status_code != 200:
                    continue
                mid = (rm.json() or {}).get("mid")
                if mid is None:
                    continue
                current = float(mid)
            tp_price = min(round(entry * tp_mul, 4), 0.99)
            sl_price = max(round(entry * sl_mul, 4), 0.01)
            checked += 1
            action = None
            if current >= tp_price:
                action = "TP_HIT"
            elif current <= sl_price:
                action = "SL_HIT"

            print(f"[PRICE_CHECK_{agent_id}] bt#{bt_id} market={market_id[:12]} "
                  f"best_bid={current:.4f} entry={entry:.4f} "
                  f"tp={tp_price:.4f} sl={sl_price:.4f} → "
                  f"{action or 'hold'}")

            if action:
                # L56 (2026-06-03) — guardia de extremos relajada.
                # Antes: SL_HIT bloqueado si current ≤ 0.05 (asumiendo
                # orderbook muerto). Pero en mercados reales 0.03-0.05
                # sí hay bids vendibles (bt#229: SL en 0.06, precio cayó
                # a 0.05 durante blackout SSL — guardia bloqueó venta
                # legítima y costó $2 adicional).
                # Nuevo umbral: 0.02 (verdadero extremo donde el book
                # sí está vacío). 0.05 ahora vende normalmente.
                if action == "TP_HIT" and current >= 0.98:
                    print(f"[PRICE_CHECK_{agent_id}] bt#{bt_id} TP_HIT current="
                          f"{current:.4f} ≥ 0.98 → HOLD para reconciler")
                    continue
                if action == "SL_HIT" and current <= 0.02:
                    print(f"[PRICE_CHECK_{agent_id}] bt#{bt_id} SL_HIT current="
                          f"{current:.4f} ≤ 0.02 → HOLD para reconciler")
                    continue

                # 1) Cierre normal vía orderbook (toma liquidez al best_bid)
                result = close_position(bt_id)
                if result.get("status") == "ok":
                    if action == "TP_HIT": closed_tp += 1
                    else: closed_sl += 1
                    print(f"[PRICE_CHECK_{agent_id}] bt#{bt_id} closed: {action} "
                          f"price={result.get('close_price')}")
                    continue

                # 2) FALLBACK AGRESIVO (anti agujero-negro 2026-05-21):
                #    close_position revierte a 'open' al fallar. Intentamos
                #    publicar LIMIT SELL propia al precio TP/SL para HACER mercado.
                #    Si alguien compra a ese precio → cierre garantizado.
                target_price = tp_price if action == "TP_HIT" else sl_price
                print(f"[PRICE_CHECK_{agent_id}] bt#{bt_id} {action} fallback: "
                      f"intentando LIMIT SELL @ {target_price:.4f}")
                try:
                    fb = _post_limit_sell_for_close(bt_id, token_id, entry,
                                                     side, target_price)
                    if fb.get("status") == "posted":
                        print(f"[PRICE_CHECK_{agent_id}] bt#{bt_id} LIMIT posted "
                              f"order_id={fb.get('order_id','?')[:16]}…  "
                              f"trade sigue 'open' hasta fill o resolver")
                    elif fb.get("status") == "filled":
                        if action == "TP_HIT": closed_tp += 1
                        else: closed_sl += 1
                        print(f"[PRICE_CHECK_{agent_id}] bt#{bt_id} fallback FILLED")
                    else:
                        # Logear msg además de reason para diagnóstico
                        _detail = fb.get('msg') or fb.get('reason') or '?'
                        print(f"[PRICE_CHECK_{agent_id}] bt#{bt_id} fallback no posted: "
                              f"{fb.get('reason','?')} ({str(_detail)[:180]}) — reconciler maneja resolución")
                except Exception as e:
                    print(f"[PRICE_CHECK_{agent_id}] bt#{bt_id} fallback err: {_sanitize(e)}")
        except Exception as e:
            print(f"[PRICE_CHECK_{agent_id}] bt#{bt_id} error: {_sanitize(e)}")
            continue

    return {"checked": checked, "closed_tp": closed_tp, "closed_sl": closed_sl,
            "agent": agent_id, "tp_mul": tp_mul, "sl_mul": sl_mul}


def auto_transfer_pending_redeems(threshold_usdc: float = 50.0) -> dict:
    """
    Fase 4.1 — Si hay >= threshold_usdc de shares ganadoras sin redimir en
    deposit wallet, dispara safeBatchTransferFrom hacia la proxy legacy
    para que Polymarket auto-redime allí.

    Returns dict {transferred, total_pending, threshold, tx_hash, msg}.
    """
    try:
        from web3 import Web3 as _W3
    except ImportError:
        return {"transferred": False, "msg": "web3 not installed"}

    _w3 = _W3(_W3.HTTPProvider("https://polygon-bor-rpc.publicnode.com"))
    CTF = _W3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
    dw  = _W3.to_checksum_address(config.DEPOSIT_WALLET_ADDRESS or "")
    abi = [{"name":"balanceOf","type":"function","inputs":[
            {"name":"o","type":"address"},{"name":"i","type":"uint256"}],
           "outputs":[{"name":"","type":"uint256"}],"stateMutability":"view"}]
    ctf = _w3.eth.contract(address=CTF, abi=abi)

    # Sumar shares ganadoras pendientes en deposit wallet (DISTINCT token_id)
    conn = database.get_connection()
    won_rows = conn.execute("""
        SELECT DISTINCT token_id FROM bot_trades
        WHERE paper_trade=0 AND status='closed' AND outcome='won'
          AND token_id IS NOT NULL
    """).fetchall()
    conn.close()

    total_pending = 0.0
    for (tok,) in won_rows:
        try:
            bal = ctf.functions.balanceOf(dw, int(tok)).call()
            total_pending += bal / 1e6
        except Exception:
            pass

    if total_pending < threshold_usdc:
        return {"transferred": False, "total_pending": round(total_pending, 4),
                "threshold": threshold_usdc,
                "msg": f"pendiente ${total_pending:.2f} < threshold ${threshold_usdc} — no se hace transfer"}

    # Disparar el script de transfer
    print(f"[AUTO-TRANSFER] Pendiente redeem ${total_pending:.2f} >= ${threshold_usdc} — "
          f"disparando safeBatchTransferFrom dw→proxy")
    try:
        import subprocess
        import sys as _sys
        result = subprocess.run(
            [_sys.executable, "tools/transfer_shares_to_proxy.py", "--execute"],
            cwd=str(Path(__file__).resolve().parent),
            capture_output=True, text=True, timeout=60,
        )
        out = (result.stdout or "") + (result.stderr or "")
        tx_hash = None
        for line in out.splitlines():
            if "txHash:" in line:
                tx_hash = line.split("txHash:")[-1].strip()
                break
        ok = result.returncode == 0 and ("STATE_MINED" in out or "STATE_CONFIRMED" in out)
        print(f"[AUTO-TRANSFER] {'OK' if ok else 'FAIL'} · tx={tx_hash}")
        return {"transferred": ok, "total_pending": round(total_pending, 4),
                "threshold": threshold_usdc, "tx_hash": tx_hash,
                "msg": f"transfer {'ok' if ok else 'failed'} - ${total_pending:.2f} movidos a proxy"}
    except Exception as e:
        print(f"[AUTO-TRANSFER] excepción: {_sanitize(e)}")
        return {"transferred": False, "total_pending": round(total_pending, 4),
                "threshold": threshold_usdc, "msg": f"error: {_sanitize(e)}"}


if __name__ == "__main__":
    print("executor.py no debe ejecutarse directamente.")
    print("Para tests: usar tests/test_executor.py")
    sys.exit(1)
