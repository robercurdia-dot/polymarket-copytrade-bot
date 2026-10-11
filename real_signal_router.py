"""
real_signal_router.py — Capa "Agente → Real Trading" (Fase 3.1)
=================================================================
Decide si una señal del experimento multi-agente debe ejecutarse en REAL.

Mecanismo: filtro automático. NO depende de Telegram.

El agente que decide trading real es configurable vía config.REAL_TRADING_AGENT
("A", "B", "C" o "NONE"). Solo la decisión de ESE agente sobre cada señal
determina si se ejecuta real. El resto de agentes siguen siendo paper-only.

Cadena de guardas (todas deben pasar para ejecutar real):
  1. REAL_TRADING_AGENT != "NONE"
  2. PAPER_TRADING == False
  3. REAL_TRADING_ENABLED == True
  4. El agente elegido decidió "open" sobre esta señal
  5. risk_manager.can_trade(MAX_TRADE_SIZE_USDC) == True
  → entonces se llama executor.execute_trade con los parámetros del agente.

Integración: paper_executor.evaluate_signal_for_paper_trade() llama a
route_signal_to_real() DESPUÉS de crear los paper_trades de los 3 agentes.
"""

import os
import threading
import time
from pathlib import Path
from dotenv import load_dotenv

import config

# ── Fix 1 & 2 (2026-06-10): anti-lookback-storm guards ───────────────────────
# Contador en memoria para el circuit breaker (Fix 2).
# Lista de timestamps (unix) de las últimas ejecuciones reales DEF_COPY.
_COPY_REAL_EXEC_TIMES: list = []
_COPY_EXEC_LOCK = threading.Lock()
COPY_CB_WINDOW_SECS  = 60   # ventana del circuit breaker
COPY_CB_MAX_TRADES   = 3    # máximo de reales por ventana

# C-3 FIX (2026-06-10): sentinel para señalar al finally que NO actualice _COPY_LAST_AGG.
# Cuando risk_manager bloquea un trade, no queremos consumir el bloque $300.
class _RiskManagerBlock(Exception):
    """Lanzada internamente para indicar bloqueo por risk_manager (no error real)."""

# Set de mercados "en ejecución" para evitar race condition de duplicados.
# Cuando route_signal_to_real_copy está ejecutando un trade en market X,
# cualquier otra señal para market X se descarta inmediatamente.
_COPY_IN_PROGRESS: set = set()
_COPY_MARKET_LOCK = threading.Lock()

# L81 (2026-06-10) — Dict persistente: market_id → agg_at_last_open.
# Sustituye el set de bloqueo total (L80). Permite REINFORCE escalado:
#   - Primera entrada: no hay entrada en el dict → pasa libremente.
#   - REINFORCE: si agg - last_agg >= DEFINITIVE_THRESHOLD ($300) → entra,
#                actualiza el dict. Si no → skip (bloque no completado aún).
# Ventaja: Swisstony puede añadir otro $300 al mismo mercado y nuestro bot
# refuerza proporcionalmente, sin abrir a cada +$3 (bug de esta mañana).
# Se limpia SOLO al reiniciar el proceso.
_COPY_LAST_AGG: dict = {}  # market_id → {"agg": float, "token_id": str}
# Nota: incluye token_id para bloquear entradas en el lado contrario del mismo mercado.
# Swisstony opera ambos lados (OVER+UNDER) en un mismo partido; sin este lock
# el bot copiaba ambos lados y perdía en uno. Fix: una vez intentado token X en
# market Y, cualquier señal con token_id != X para ese market se descarta.

_ENV_PATH = Path(__file__).resolve().parent / ".env"


def _populate_copy_last_agg_from_db() -> None:
    """M-2 FIX (2026-06-10): pre-popula _COPY_LAST_AGG al arrancar el proceso.
    Sin esto, en el primer ciclo tras un restart el dict está vacío → la Capa B
    (delta check) se salta para todos los mercados con posición abierta → riesgo
    de REINFORCE aunque Swisstony no haya añadido nada nuevo.
    Poblamos con el agg actual de detected_trades para cada market_id que tenga
    un bot_trade DEF_COPY abierto. Así el primer REINFORCE post-restart también
    requiere delta >= $300 desde el agg actual.
    """
    try:
        load_dotenv(dotenv_path=_ENV_PATH, override=True)
        threshold = float(os.getenv("DEFINITIVE_THRESHOLD", "300"))
        import database
        SWIS_ADDR = "0x204f72f35326db932158cba6adff0b9a1da95e14"
        conn = database.get_connection()
        # Obtener market_ids con DEF_COPY abiertos
        open_markets = conn.execute(
            """SELECT DISTINCT market_id FROM bot_trades
               WHERE agent_id='DEF_COPY' AND paper_trade=0
                 AND status IN ('pending','open','closing')"""
        ).fetchall()
        for (mid,) in open_markets:
            row = conn.execute(
                "SELECT COALESCE(size_usdc,0) FROM detected_trades "
                "WHERE LOWER(wallet_address)=? AND market_id=? "
                "ORDER BY detected_at DESC LIMIT 1",
                (SWIS_ADDR, mid)).fetchone()
            agg = float(row[0] or 0)
            # También recuperar el token_id del trade abierto para el token-lock.
            tok_row = conn.execute(
                "SELECT token_id FROM bot_trades "
                "WHERE market_id=? AND agent_id='DEF_COPY' AND paper_trade=0 "
                "  AND status IN ('pending','open','closing') LIMIT 1",
                (mid,)).fetchone()
            tok = tok_row[0] if tok_row else None
            if agg >= threshold:
                _COPY_LAST_AGG[mid] = {"agg": agg, "token_id": tok}
        conn.close()
        if _COPY_LAST_AGG:
            print(f"[ROUTER_COPY] _COPY_LAST_AGG pre-populado: "
                  f"{len(_COPY_LAST_AGG)} mercados abiertos → REINFORCE requiere +${threshold:.0f}")
    except Exception as _e:
        print(f"[ROUTER_COPY] _populate_copy_last_agg_from_db warn: {_e}")


def _wallet_whitelist() -> set:
    """Lee REAL_WALLET_WHITELIST de .env on-the-fly para que ajustes en el
    fichero surtan efecto sin tocar nada más en el process.
    Devuelve set de addresses lowercased. Vacío → sin filtro (passthrough)."""
    try:
        load_dotenv(dotenv_path=_ENV_PATH, override=True)
    except Exception:
        pass
    raw = os.getenv("REAL_WALLET_WHITELIST", "") or ""
    return {a.strip().lower() for a in raw.split(",") if a.strip()}


def _max_entry_price() -> float | None:
    """Lee el cap superior de entry para el agente B.

    Prioridad (L29 — 2026-05-31):
      1. REAL_B_ENTRY_MAX (nueva var específica de B post-reajuste)
      2. REAL_MAX_ENTRY_PRICE (legacy, kept for compat)
      3. None (sin filtro).

    Backtest paper 7d sin swisstony: bucket 0.50-0.60 +$175, bucket >0.60
    neutro/negativo. Cap actual recomendado: 0.60."""
    try:
        load_dotenv(dotenv_path=_ENV_PATH, override=True)
    except Exception:
        pass
    raw = (os.getenv("REAL_B_ENTRY_MAX", "")
           or os.getenv("REAL_MAX_ENTRY_PRICE", "")).strip()
    if not raw:
        return None
    try:
        v = float(raw)
        return v if v > 0 else None
    except Exception:
        return None


def route_signal_to_real(signal_data: dict, agent_decisions: dict) -> bool:
    """
    Evalúa si una señal debe ejecutarse en real y, si procede, la ejecuta.

    Args:
        signal_data: la señal original (mismo objeto que va a paper_executor).
            Campos usados: detected_trade_id, market_id, token_id, side,
            outcome_name, question.
        agent_decisions: dict {"A": {...}, "B": {...}, "C": {...}} con la
            decisión de cada agente. Cada valor debe tener al menos:
              "action": "open" | "skip"
              "entry_price": float   (precio de entrada que usó el agente)
              "size": float          (tamaño; se ignora — se usa MAX_TRADE_SIZE_USDC)

    Returns:
        True  → se ejecutó (o intentó ejecutar) la orden real.
        False → no se ejecutó (cualquier guarda falló).
    """
    # Guarda 1 — agente configurado
    agent_id = config.REAL_TRADING_AGENT
    if agent_id == "NONE":
        return False

    # Guarda 2/3 — kill switches hot-reload (igual que route_signal_to_real_copy).
    # config.PAPER_TRADING/REAL_TRADING_ENABLED son valores de startup; usamos
    # os.getenv para que cambios en .env surtan efecto sin reiniciar el bot.
    try:
        load_dotenv(dotenv_path=_ENV_PATH, override=True)
        _paper_b = os.getenv("PAPER_TRADING",        "true").strip().lower() != "false"
        _real_b  = os.getenv("REAL_TRADING_ENABLED", "false").strip().lower() == "true"
    except Exception:
        _paper_b = config.PAPER_TRADING
        _real_b  = config.REAL_TRADING_ENABLED
    if _paper_b or not _real_b:
        return False

    # Guarda 3b — nicho excluido / desconocido (Fase 3.1b, fail-closed 2026-05-23)
    # Fail-closed: rechazamos también niche=None. El clasificador ha demostrado
    # perder términos (uranium, Iranian) que dejarían pasar geopolítica a real.
    # Coste: ~3% trades sports mal clasificados se pierden; mitigable
    # extendiendo NICHE_KEYWORDS en leaderboard_analyzer.py.
    niche = (signal_data or {}).get("niche")
    if not niche:
        print(f"[ROUTER] Señal descartada: niche=None (fail-closed)")
        return False
    if niche in config.REAL_EXCLUDED_NICHES:
        print(f"[ROUTER] Señal descartada: niche={niche} excluida en Fase 3a")
        return False

    # Guarda 3c — wallet whitelist (hot-reloadable desde .env).
    # Vacía → passthrough (no aplica filtro, mantiene comportamiento legacy).
    whitelist = _wallet_whitelist()
    if whitelist:
        # El campo origen puede llamarse 'wallet_address' (detected_trades),
        # 'wallet' o 'wallet_copied' según la rama del pipeline.
        wallet = ((signal_data or {}).get("wallet_address")
                  or (signal_data or {}).get("wallet")
                  or (signal_data or {}).get("wallet_copied")
                  or "")
        wallet_lc = str(wallet).lower()
        if wallet_lc not in whitelist:
            print(f"[ROUTER] Señal descartada: wallet no en whitelist "
                  f"(W={wallet[:10] if wallet else 'unknown'}…)")
            return False

    # Guarda 3d — entry_price máximo (anti señales sin asimetría 2026-05-22).
    # Backtest sobre 82 trades reales: entry < 0.50 → ppt +$1.03 (+72% vs total).
    # Si REAL_MAX_ENTRY_PRICE no definida → sin filtro (passthrough).
    max_entry = _max_entry_price()
    if max_entry is not None:
        # Priorizar entry_price del agente (precio que vio al decidir).
        # Fallback al insider/entry del signal_data.
        decision_for_price = (agent_decisions or {}).get(agent_id) or {}
        ep_candidate = (decision_for_price.get("entry_price")
                        or (signal_data or {}).get("insider_price")
                        or (signal_data or {}).get("entry_price"))
        try:
            ep_val = float(ep_candidate) if ep_candidate else None
        except (TypeError, ValueError):
            ep_val = None
        if ep_val is not None and ep_val >= max_entry:
            print(f"[ROUTER] Señal descartada: entry_price={ep_val:.4f} >= "
                  f"max={max_entry:.4f}")
            return False

    # L43 (2026-06-02) — Piso mínimo de entry para agente B.
    # Bucket <0.10 real: 4 trades, 0 wins, -$29 (orderbook casi vacío
    # cuando precio en zona resolución → SLs se saltan brutalmente).
    # Mantiene Geneva-style winners (0.18-0.20).
    try:
        import os
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=_ENV_PATH, override=True)
        min_entry_raw = os.getenv("REAL_B_ENTRY_MIN", "").strip()
        if min_entry_raw:
            min_entry = float(min_entry_raw)
            decision_for_price = (agent_decisions or {}).get(agent_id) or {}
            ep_candidate = (decision_for_price.get("entry_price")
                            or (signal_data or {}).get("insider_price")
                            or (signal_data or {}).get("entry_price"))
            try:
                ep_val = float(ep_candidate) if ep_candidate else None
            except (TypeError, ValueError):
                ep_val = None
            if ep_val is not None and ep_val < min_entry:
                print(f"[ROUTER] Señal descartada: entry_price={ep_val:.4f} < "
                      f"min={min_entry:.4f} (L43 piso)")
                return False
    except Exception as _e:
        pass

    # Guarda 4 — el agente elegido decidió abrir
    decision = (agent_decisions or {}).get(agent_id)
    if not decision or decision.get("action") != "open":
        return False

    # Guarda 5 — risk manager
    import risk_manager
    can, reason = risk_manager.can_trade(config.MAX_TRADE_SIZE_USDC, agent_id=agent_id)
    if not can:
        print(f"[REAL_ROUTER] Señal del agente {agent_id} bloqueada por "
              f"risk_manager: {reason}")
        return False

    # Todas las guardas pasaron → ejecutar real
    import executor
    entry_price = decision.get("entry_price")
    if entry_price is None:
        entry_price = signal_data.get("insider_price") or signal_data.get("entry_price")
    if not entry_price or entry_price <= 0:
        print(f"[REAL_ROUTER] Señal del agente {agent_id} sin entry_price válido — skip")
        return False

    print(f"[REAL_ROUTER] Ejecutando REAL — agente {agent_id} decidió 'open' · "
          f"market={str(signal_data.get('market_id'))[:12]} · "
          f"size={config.MAX_TRADE_SIZE_USDC}")

    result = executor.execute_trade(
        detected_trade_id=signal_data.get("detected_trade_id"),
        market_id=signal_data.get("market_id"),
        token_id=signal_data.get("token_id"),
        side=signal_data.get("side"),
        size_usdc=config.MAX_TRADE_SIZE_USDC,
        entry_price=float(entry_price),
    )
    print(f"[REAL_ROUTER] Resultado execute_trade: {result.get('status') if result else None}")
    return True


def _d_whitelist() -> set:
    """Whitelist exclusiva del agente D (hold-to-resolve). Hot-reload .env."""
    try:
        load_dotenv(dotenv_path=_ENV_PATH, override=True)
    except Exception:
        pass
    raw = os.getenv("REAL_D_WALLET_WHITELIST", "") or ""
    return {a.strip().lower() for a in raw.split(",") if a.strip()}


def _d_trade_size() -> float:
    try:
        load_dotenv(dotenv_path=_ENV_PATH, override=True)
    except Exception:
        pass
    raw = os.getenv("REAL_D_TRADE_SIZE", "3").strip()
    try:
        v = float(raw)
        return v if v > 0 else 3.0
    except Exception:
        return 3.0


def _d_enabled() -> bool:
    try:
        load_dotenv(dotenv_path=_ENV_PATH, override=True)
    except Exception:
        pass
    return os.getenv("REAL_D_ENABLED", "1").strip() in ("1", "true", "True")


def route_signal_to_real_d(signal_data: dict) -> bool:
    """
    Router del agente D (REAL hold-to-resolve, sin TP/SL).
    Versión 2026-05-28 — solo para wallets con edge hold (ej. swisstony).

    Diferencias vs router B:
      - Whitelist propio (REAL_D_WALLET_WHITELIST)
      - Trade size propio (REAL_D_TRADE_SIZE, default $3)
      - NO aplica REAL_MAX_ENTRY_PRICE (acepta cualquier entry)
      - Marca el trade con agent_id='D' para que fast_price_check_b lo ignore
      - SOLO cierre por market resolution (reconciler Pase 1)

    Guards:
      1. REAL_D_ENABLED == True
      2. PAPER_TRADING == False
      3. REAL_TRADING_ENABLED == True
      4. niche válido (no None, no excluido)
      5. wallet en REAL_D_WALLET_WHITELIST
      6. wallet NO en blacklist global
      7. risk_manager.can_trade(size)
    """
    if not _d_enabled():
        return False
    # Kill switches hot-reload (coherencia con router DEF_COPY).
    try:
        load_dotenv(dotenv_path=_ENV_PATH, override=True)
        _paper_d = os.getenv("PAPER_TRADING",        "true").strip().lower() != "false"
        _real_d  = os.getenv("REAL_TRADING_ENABLED", "false").strip().lower() == "true"
    except Exception:
        _paper_d = config.PAPER_TRADING
        _real_d  = config.REAL_TRADING_ENABLED
    if _paper_d or not _real_d:
        return False

    # L81 — Guard anti-doble-routing: Swisstony está en REAL_D_WALLET_WHITELIST
    # Y en el router DEF_COPY. Si REAL_COPY_ENABLED=true, DEF_COPY lo gestiona
    # con lógica más sofisticada (copy-trader-exit, scaling, REINFORCE delta).
    # Dejar que ambos ejecuten simultáneamente abre 2 trades reales por señal.
    SWIS = "0x204f72f35326db932158cba6adff0b9a1da95e14"
    _w_check = ((signal_data or {}).get("wallet_address")
                or (signal_data or {}).get("wallet")
                or (signal_data or {}).get("wallet_copied") or "")
    if str(_w_check).lower() == SWIS:
        try:
            load_dotenv(dotenv_path=_ENV_PATH, override=True)
        except Exception:
            pass
        if os.getenv("REAL_COPY_ENABLED", "false").strip().lower() == "true":
            return False  # DEF_COPY router tiene prioridad sobre D para Swisstony

    # Niche check
    niche = (signal_data or {}).get("niche")
    if not niche:
        print(f"[ROUTER_D] descartada: niche=None")
        return False
    if niche in config.REAL_EXCLUDED_NICHES:
        print(f"[ROUTER_D] descartada: niche={niche} excluida")
        return False

    # Whitelist D específica
    whitelist = _d_whitelist()
    if not whitelist:
        return False  # D no configurado
    wallet = ((signal_data or {}).get("wallet_address")
              or (signal_data or {}).get("wallet")
              or (signal_data or {}).get("wallet_copied") or "")
    wallet_lc = str(wallet).lower()
    if wallet_lc not in whitelist:
        return False

    # Blacklist global
    blacklist_raw = os.getenv("REAL_WALLET_BLACKLIST", "") or ""
    blacklist = {a.strip().lower() for a in blacklist_raw.split(",") if a.strip()}
    if wallet_lc in blacklist:
        print(f"[ROUTER_D] descartada: wallet en blacklist global")
        return False

    # ── Sweet-spot filters (2026-05-28 análisis empírico paper C) ─────
    # Datos sobre 798 trades cerrados de swisstony en sports:
    #   entry 0.30-0.50  → ppt $+25/$100 (EL EDGE)
    #   entry 0.50-0.80  → ppt $+1/$100 (ruido)
    #   entry < 0.20     → ppt $-62/$100 (sangra)
    # Insider size:
    #   $100-500   → ppt $+7.42/$100 (edge real)
    #   $500-2k    → ppt $-1.66/$100
    #   $2k+       → ppt $-14.76/$100 (hedges/cobertura, no edge)
    # Configurable vía env para experimentar.
    ep = signal_data.get("insider_price") or signal_data.get("entry_price") or 0
    try: ep = float(ep)
    except: ep = 0
    d_entry_min = float(os.getenv("REAL_D_ENTRY_MIN", "0.30"))
    d_entry_max = float(os.getenv("REAL_D_ENTRY_MAX", "0.50"))
    if ep < d_entry_min or ep >= d_entry_max:
        print(f"[ROUTER_D] descartada: entry_price={ep:.3f} fuera de "
              f"sweet spot [{d_entry_min}, {d_entry_max})")
        return False

    insider_size = signal_data.get("size_usdc") or signal_data.get("insider_size") or 0
    try: insider_size = float(insider_size)
    except: insider_size = 0
    d_size_min = float(os.getenv("REAL_D_INSIDER_SIZE_MIN", "100"))
    d_size_max = float(os.getenv("REAL_D_INSIDER_SIZE_MAX", "2000"))
    if insider_size < d_size_min or insider_size > d_size_max:
        print(f"[ROUTER_D] descartada: insider_size=${insider_size:.0f} fuera de "
              f"sweet spot [${d_size_min}, ${d_size_max}]")
        return False

    # Risk manager
    size_usdc = _d_trade_size()
    import risk_manager
    can, reason = risk_manager.can_trade(size_usdc, agent_id="D")
    if not can:
        print(f"[ROUTER_D] descartada por risk_manager: {reason}")
        return False

    entry_price = (signal_data.get("insider_price")
                   or signal_data.get("entry_price"))
    if not entry_price or float(entry_price) <= 0:
        print(f"[ROUTER_D] sin entry_price válido")
        return False

    print(f"[ROUTER_D] EJECUTANDO REAL D — wallet={wallet_lc[:10]}… "
          f"market={str(signal_data.get('market_id'))[:12]} · "
          f"size=${size_usdc} · entry={entry_price} (HOLD-TO-RESOLVE)")

    import executor
    result = executor.execute_trade(
        detected_trade_id=signal_data.get("detected_trade_id"),
        market_id=signal_data.get("market_id"),
        token_id=signal_data.get("token_id"),
        side=signal_data.get("side"),
        size_usdc=size_usdc,
        entry_price=float(entry_price),
        agent_id="D",   # IMPORTANTE: marca el trade como D
    )
    print(f"[ROUTER_D] Resultado: {result.get('status') if result else None}")
    return True


def _copy_enabled() -> bool:
    try:
        load_dotenv(dotenv_path=_ENV_PATH, override=True)
    except Exception:
        pass
    return os.getenv("REAL_COPY_ENABLED", "false").strip().lower() == "true"


def _copy_trade_size() -> float:
    try:
        load_dotenv(dotenv_path=_ENV_PATH, override=True)
    except Exception:
        pass
    raw = os.getenv("REAL_COPY_TRADE_SIZE", "5").strip()
    try:
        v = float(raw)
        return v if v > 0 else 5.0
    except Exception:
        return 5.0


def route_signal_to_real_copy(signal_data: dict) -> bool:
    """
    Router DEF_COPY real (L81 2026-06-10).
    Replica la lógica copy-trader-exit de DEF_COPY en real.

    Guards:
      1. REAL_COPY_ENABLED == true
      2. PAPER_TRADING == false
      3. REAL_TRADING_ENABLED == true
      4. niche in (sports, esports)
      5. wallet == Swisstony hardcoded (0x204f…)
      6. insider agg >= DEFINITIVE_THRESHOLD ($300)
      7. idempotencia triple:
         a. _COPY_IN_PROGRESS (concurrencia en vuelo)
         b. _COPY_LAST_AGG delta — si ya operamos este mercado, solo re-entra
            cuando agg - last_agg >= threshold (otro bloque de $300 completo).
            Previene la avalancha de REINFORCE (+$3, +$8…) sin bloquear los
            REINFORCE legítimos (+$300 real). Fix del bug que causó 97 trades.
         c. BD: status IN ('pending','open') — doble seguro para reinicios.
      8. entry_price en rango [0.05, 0.97]
      9. risk_manager.can_trade(size)

    El cierre lo gestiona definitive_copy_watcher cuando Swisstony vende/mergea.

    L81 (2026-06-10):
      - _COPY_DONE_MARKETS (set, bloqueo total) → _COPY_LAST_AGG (dict de delta).
      - Eliminado guard is_reinforce: la lógica de delta lo reemplaza correctamente.
      - Eliminado circuit breaker REAL_COPY_MAX_OPEN del risk_manager.
    """
    if not _copy_enabled():
        return False
    # Hot-reload .env — permite cambiar PAPER_TRADING y REAL_TRADING_ENABLED
    # en caliente sin reiniciar el proceso (igual que los otros routers).
    try:
        load_dotenv(dotenv_path=_ENV_PATH, override=True)
    except Exception:
        pass
    # L80-fix ALTO: usar os.getenv consistente con el hot-reload de arriba.
    # Los otros routers usan config.PAPER_TRADING (valor de arranque); aquí
    # leemos os.getenv para que el hot-reload funcione también en este router.
    # Importante: el default es "true" (safe-fail) para que si falta la var
    # el router no ejecute trades reales.
    if os.getenv("PAPER_TRADING", "true").strip().lower() != "false":
        return False
    if os.getenv("REAL_TRADING_ENABLED", "false").strip().lower() != "true":
        return False
    if os.getenv("REAL_COPY_ENABLED", "false").strip().lower() != "true":
        return False  # triple kill-switch: también verificar aquí explícitamente

    # ── Fix 1 (2026-06-10): bloquear señales antiguas — anti-lookback-storm ──
    # Si la señal tiene más de 40 segundos de antigüedad, no ejecutar en real.
    # Evita que un restart con lookback 1h dispare decenas de trades reales.
    # 40s da ~5x margen sobre detección típica (3-8s) y cubre lentitudes de API.
    _MAX_SIGNAL_AGE_MIN = 80 / 60
    _sig_ts = signal_data.get("timestamp") or signal_data.get("detected_at")
    _sig_age_min = None
    if _sig_ts is None:
        # Sin timestamp → bloquear por seguridad
        print("[ROUTER_COPY] SKIP señal sin timestamp — solo paper")
        return False
    try:
        from datetime import datetime as _dt_sig, timezone as _tz_sig
        _now_utc = _dt_sig.now(_tz_sig.utc)
        # Puede ser unix timestamp (int/float) o ISO string
        if isinstance(_sig_ts, (int, float)):
            _sig_dt = _dt_sig.fromtimestamp(float(_sig_ts), tz=_tz_sig.utc)
        else:
            _sig_str = str(_sig_ts).strip().replace("Z", "+00:00")
            _sig_dt = _dt_sig.fromisoformat(_sig_str)
            if _sig_dt.tzinfo is None:
                _sig_dt = _sig_dt.replace(tzinfo=_tz_sig.utc)
        _sig_age_min = (_now_utc - _sig_dt).total_seconds() / 60
        if _sig_age_min > _MAX_SIGNAL_AGE_MIN:
            print(f"[ROUTER_COPY] SKIP señal antigua ({_sig_age_min:.1f}min) — solo paper")
            return False
    except Exception as _e_ts:
        # Si no podemos parsear el timestamp → bloquear por seguridad
        print(f"[ROUTER_COPY] SKIP señal con timestamp no parseable ({_sig_ts!r}): {_e_ts} — solo paper")
        return False

    niche = (signal_data or {}).get("niche") or "sports"
    if niche in config.REAL_EXCLUDED_NICHES:
        return False

    SWIS = "0x204f72f35326db932158cba6adff0b9a1da95e14"
    wallet = ((signal_data or {}).get("wallet_address")
              or (signal_data or {}).get("wallet")
              or (signal_data or {}).get("wallet_copied") or "")
    if str(wallet).lower() != SWIS:
        return False

    import database
    market_id = signal_data.get("market_id", "")

    # Guard umbral de agregado — mismo que DEF_COPY paper.
    # Fix C1 (2026-06-18): usar MAX(size_usdc) de detected_trades como acumulado real.
    # MAX es correcto (cada fila guarda el total hasta ese momento, no el delta individual).
    # SUM sería incorrecto. El timestamp guard (<80s) impide que señales antiguas
    # disparen trades reales — este MAX solo sirve para conocer el acumulado real.
    try:
        load_dotenv(dotenv_path=_ENV_PATH, override=True)
    except Exception:
        pass
    threshold = float(os.getenv("DEFINITIVE_THRESHOLD", "300"))  # mismo default que paper_executor
    signal_size = float(signal_data.get("size_usdc") or signal_data.get("size") or 0)
    try:
        _conn_agg = database.get_connection()
        _last_open_real = _conn_agg.execute(
            """SELECT MAX(executed_at) FROM bot_trades
               WHERE agent_id='DEF_COPY' AND market_id=? AND paper_trade=0
                 AND status IN ('open','closed')""",
            (market_id,)).fetchone()
        _since_real = (_last_open_real[0] or '2000-01-01') if _last_open_real else '2000-01-01'
        _agg_row_real = _conn_agg.execute(
            """SELECT COALESCE(MAX(size_usdc), 0) FROM detected_trades
               WHERE wallet_address=? AND market_id=? AND detected_at >= ?""",
            (SWIS, market_id, _since_real)).fetchone()
        _conn_agg.close()
        agg = max(float(_agg_row_real[0] or 0), signal_size)
    except Exception:
        agg = signal_size
    if agg < threshold:
        return False

    # incoming_token definido antes del circuit breaker para que esté disponible
    # en el dict que guarda _COPY_LAST_AGG (también usado más abajo en Capa B).
    incoming_token = signal_data.get("token_id") or ""

    # ── Fix 2 (2026-06-10): circuit breaker — máximo 3 trades reales por minuto ──
    _now_mono = time.monotonic()
    with _COPY_EXEC_LOCK:
        _COPY_REAL_EXEC_TIMES[:] = [
            t for t in _COPY_REAL_EXEC_TIMES
            if _now_mono - t < COPY_CB_WINDOW_SECS
        ]
        _n_recent = len(_COPY_REAL_EXEC_TIMES)
        if _n_recent >= COPY_CB_MAX_TRADES:
            print(f"[ROUTER_COPY] CIRCUIT BREAKER — {_n_recent} trades en 60s, pausando")
            with _COPY_MARKET_LOCK:
                if market_id not in _COPY_LAST_AGG:
                    _COPY_LAST_AGG[market_id] = {"agg": agg, "token_id": incoming_token}
            return False

    # Guard idempotencia TRIPLE (L81).
    # Capa A: _COPY_IN_PROGRESS — bloquea concurrencia en vuelo (mismo thread/async).
    # Capa B: _COPY_LAST_AGG — dict persistente en sesión.
    #   Sin entrada previa → primera vez en este mercado → pasa.
    #   Con entrada previa → REINFORCE: solo pasa si agg - last_agg >= threshold
    #     (Swisstony añadió otro bloque de $300 desde nuestro último open).
    #     Así: +$8 de REINFORCE no dispara nada; +$300 sí → REINFORCE legítimo.
    # Capa C: BD status IN ('pending','open') — seguro extra para reinicios.
    # H-4 FIX (2026-06-10): Capa B (in-memory) dentro del lock, Capa C (DB) FUERA.
    # La query SQLite dentro del lock bloqueaba todo el procesamiento de señales DEF_COPY
    # mientras la BD respondía (hasta 50-100ms). Ahora: lock solo para estructuras
    # in-memory, DB query en paralelo (safe porque el lock protege la decisión final).
    with _COPY_MARKET_LOCK:
        if market_id in _COPY_IN_PROGRESS:
            return False  # otro thread ya está ejecutando este mercado
        # Capa B — delta check por threshold ($500).
        # El TOKEN LOCK (bloqueo por token distinto) fue eliminado: Swisstony hace
        # coberturas legítimas comprando ambos lados del mismo mercado y debemos copiarlas.
        last_entry = _COPY_LAST_AGG.get(market_id)
        last_agg = None
        if last_entry is not None:
            last_token = last_entry["token_id"] if isinstance(last_entry, dict) else ""
            last_agg = last_entry["agg"] if isinstance(last_entry, dict) else float(last_entry)
            if last_token and incoming_token and last_token == incoming_token:
                # Mismo token: REINFORCE — delta >= threshold/2 (Fix C3 2026-06-18)
                delta = agg - last_agg
                if delta < threshold / 2:
                    return False
            # Token distinto (cobertura) o token desconocido: tratar como posición nueva,
            # solo verificar que Swisstony tiene >= threshold en ese lado (ya validado arriba)
        # Reservar provisionalmente (se libera si Capa C falla)
        _COPY_IN_PROGRESS.add(market_id)

    # Capa C — evitar duplicado exacto: mismo market + mismo token ya abierto.
    # No bloquea token distinto (permite coberturas de Swisstony).
    try:
        conn = database.get_connection()
        active = conn.execute(
            """SELECT COUNT(*) FROM bot_trades
               WHERE market_id=? AND agent_id='DEF_COPY'
                 AND token_id=? AND paper_trade=0
                 AND status IN ('pending','open')""",
            (market_id, incoming_token)).fetchone()
        conn.close()
        if active and active[0] > 0:
            with _COPY_MARKET_LOCK:
                _COPY_IN_PROGRESS.discard(market_id)
                if last_agg is None:
                    _COPY_LAST_AGG[market_id] = {"agg": agg, "token_id": incoming_token}
            return False
    except Exception:
        with _COPY_MARKET_LOCK:
            _COPY_IN_PROGRESS.discard(market_id)
        return False

    # CRÍTICO: a partir de aquí _COPY_IN_PROGRESS tiene market_id.
    # CUALQUIER return sin pasar por finally dejaría el mercado atascado en
    # _COPY_IN_PROGRESS para siempre (hasta reinicio del bot).
    # Solución: TODO el bloque post-lock va dentro de try/finally que garantiza
    # cleanup de _COPY_IN_PROGRESS y actualización CONDICIONAL de _COPY_LAST_AGG.
    #
    # C-3 FIX (2026-06-10): flag _should_update_agg controla si el bloque $300 se
    # consume. Si risk_manager bloquea (veto, daily_loss, etc.), NO consumimos el
    # bloque — el próximo REINFORCE puede reintentar inmediatamente.
    _should_update_agg = True  # default: consumir el bloque (CLOB éxito/fallo/price guard)
    try:
        # Misma escala que DEF_COPY paper: agg × DEFINITIVE_SWIS_SCALE, capado [$1, $20]
        # L81 FIX: max_t inicializado antes del try interior para evitar NameError
        # si load_dotenv o float() fallan y max_t se necesita en el bump de min_shares.
        max_t = 20.0  # safe default — sobrescrito por el try interior si todo va bien
        try:
            load_dotenv(dotenv_path=_ENV_PATH, override=True)
            scale = float(os.getenv('DEFINITIVE_SWIS_SCALE', '0.00784'))
            max_t = float(os.getenv('DEFINITIVE_MAX_PER_TRADE', '20'))
            min_t = float(os.getenv('DEFINITIVE_MIN_TRADE', '1'))
            size_usdc = round(min(max(agg * scale, min_t), max_t), 2)
        except Exception:
            size_usdc = _copy_trade_size()

        import risk_manager
        can, reason = risk_manager.can_trade(size_usdc, agent_id="DEF_COPY")
        if not can:
            print(f"[ROUTER_COPY] bloqueada por risk_manager: {reason}")
            # C-3 FIX: no consumir el bloque $300 cuando risk_manager bloquea.
            # Ejemplo: daily loss limit activo → Swisstony sigue comprando →
            # el próximo +$300 de agg debe poder reintentar en cuanto el límite se libere.
            _should_update_agg = False
            return False

        entry_price = signal_data.get("insider_price") or signal_data.get("entry_price")
        if not entry_price or float(entry_price) <= 0:
            print(f"[ROUTER_COPY] sin entry_price válido")
            # No consumir el bloque $300 — fallo de datos, no de mercado
            _should_update_agg = False
            return False

        # Guard precio [0.05, 0.97] — subido 0.90→0.97 (2026-06-10).
        # Swisstony usa "No" bets en Copa del Mundo a 88-97¢ (estrategia válida).
        # El guard 0.90 bloqueaba completamente esas entradas — corregido.
        _ep = float(entry_price)
        if _ep < 0.05 or _ep > 0.95:
            print(f"[ROUTER_COPY] RESOLVED-SKIP p={_ep:.4f} — mercado ya resuelto, skip real")
            _should_update_agg = False
            return False

        # L81 — Bump mínimo de shares (igual que paper_executor L89).
        # Polymarket CLOB rechaza órdenes < 5 shares. Si el size calculado no
        # llega a 5.5 shares al precio actual, lo subimos hasta ahí (con tope MAX_T).
        # Sin este bump: agg $300 × 0.00784 = $2.35 @ entry 0.55 = 4.3 shares → CLOB rechaza.
        _min_usdc_5sh = round(5.5 * _ep, 2)
        if size_usdc < _min_usdc_5sh:
            _bumped_from = size_usdc
            size_usdc = min(_min_usdc_5sh, max_t)
            print(f"[ROUTER_COPY] min_shares_bump ${_bumped_from}→${size_usdc} (5.5sh @ {_ep:.3f})")

        print(f"[ROUTER_COPY] EJECUTANDO REAL DEF_COPY — "
              f"market={str(market_id)[:14]} · size=${size_usdc} · "
              f"entry={entry_price} · agg=${agg:.0f}")

        import executor
        try:
            result = executor.execute_trade(
                detected_trade_id=signal_data.get("detected_trade_id"),
                market_id=market_id,
                token_id=signal_data.get("token_id"),
                side=signal_data.get("side"),
                size_usdc=size_usdc,
                entry_price=float(entry_price),
                agent_id="DEF_COPY",
            )
        except Exception as _exc_trade:
            # Si execute_trade lanza excepción (network, timeout, etc.),
            # NO consumir el bloque — la siguiente señal de Swis puede reintentar.
            print(f"[ROUTER_COPY] execute_trade excepción: {_exc_trade} — bloque NO consumido")
            _should_update_agg = False
            return False
        real_status = (result or {}).get('status') if result else None
        real_reason = (result or {}).get('reason') if result else None
        print(f"[ROUTER_COPY] Resultado: {real_status}")
        # Si el executor bloqueó por razones transitorias (slippage, resolved_skip
        # al momento de ejecutar, min_shares ya bumpeado) NO consumimos el bloque
        # para que la siguiente señal de Swisstony pueda reintentarlo.
        # Si fue ok, error CLOB o bloqueado por risk_manager → ya manejado arriba.
        if real_status == 'blocked' and real_reason in (
                'slippage_too_high', 'resolved_skip_clob'):
            _should_update_agg = False
        else:
            # Fix 2: registrar ejecución exitosa en el circuit breaker
            with _COPY_EXEC_LOCK:
                _COPY_REAL_EXEC_TIMES.append(time.monotonic())
        return True

    finally:
        # Siempre: liberar _COPY_IN_PROGRESS.
        # Actualizar _COPY_LAST_AGG SOLO si _should_update_agg=True (no risk block).
        # Guardar token_id junto al agg para el TOKEN LOCK de futuros signals.
        with _COPY_MARKET_LOCK:
            _COPY_IN_PROGRESS.discard(market_id)
            if _should_update_agg:
                _COPY_LAST_AGG[market_id] = {"agg": agg, "token_id": incoming_token}
