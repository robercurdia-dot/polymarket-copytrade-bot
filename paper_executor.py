"""
paper_executor.py — Paper trading automático (Fase 2.3)
=========================================================
Cada señal que pasa los filtros del monitor abre 1 paper_trade simulado.
Sin riesgo monetario. Valida reglas TP/SL antes de pasar a ejecución real.

Reglas:
  - Tamaño fijo: PAPER_SIZE_USDC ($10 por defecto)
  - TP: precio alcanza entry * PAPER_TP_MULTIPLIER (1.40 → +40%)
  - SL: precio cae a entry * PAPER_SL_MULTIPLIER (0.65 → -35%)
  - Resolución: market.closed=True con outcome_price 1.0 (win) o 0 (loss)
  - Disputed: closed=True con precio intermedio
  - Timeout: PAPER_TIMEOUT_DAYS sin tocar TP/SL ni resolver

NO interfiere con executor real (bot_trades), manual_trades, my_wallet.
"""

import time
import threading
import requests
from datetime import datetime, timedelta
from collections import Counter

import config
import database

# Race-condition fix (2026-06-10): in-memory lock de apertura para DEF agents.
# fast_loop (15s) + slow_loop (60s) procesaban la misma señal simultáneamente
# → 2 trades idénticos a 0ms de diferencia, volumen duplicado en cada entrada.
# Mismo patrón que _COPY_IN_PROGRESS en real_signal_router.py.
_PAPER_DEF_OPENING: set = set()       # "agent_id:market_id" en vuelo
_PAPER_DEF_OPEN_LOCK = threading.Lock()
# Delta-check idempotencia REINFORCEs (mirror de _COPY_LAST_AGG en real_signal_router).
# Clave: "agent_id:wallet:market_id" → {"agg": float, "token_id": str}
# REINFORCE solo se ejecuta si agg_actual - last_agg >= THRESHOLD.
# token_id guardado para TOKEN LOCK: bloquea el lado contrario del mismo mercado.
_PAPER_COPY_LAST_AGG: dict = {}

_CLOB_API = "https://clob.polymarket.com"
_TIMEOUT = 10


# ── Helpers ──────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def _get_clob_price(token_id: str) -> float | None:
    """Precio actual del token en CLOB (midpoint). None si falla."""
    if not token_id:
        return None
    try:
        r = requests.get(
            f"{_CLOB_API}/midpoint",
            params={"token_id": token_id},
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        mid = data.get("mid")
        return float(mid) if mid is not None else None
    except Exception:
        return None


def _get_executable_sell_price(token_id: str) -> float | None:
    """Best_bid: precio al que realmente podríamos VENDER ahora.
    Para agente B (mirror del real) usamos esto en vez de midpoint, así
    paper no infla TP_HIT con precios que el orderbook no soportaría.
    Fallback a midpoint si no hay bids.
    Fix 2026-05-23 anti-divergencia paper-vs-real."""
    if not token_id:
        return None
    try:
        r = requests.get(f"{_CLOB_API}/book",
                         params={"token_id": token_id}, timeout=_TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            bids = data.get("bids") or []
            if bids:
                # bids vienen como [{price, size}, ...]; max price = best bid
                pxs = []
                for b in bids:
                    try:
                        pxs.append(float(b.get("price")))
                    except (TypeError, ValueError):
                        continue
                if pxs:
                    return max(pxs)
    except Exception:
        pass
    return _get_clob_price(token_id)


def _get_best_ask(token_id: str) -> float | None:
    """Best_ask: precio real de compra (lo que pagaríamos en real).
    Usa el ask mínimo del orderbook — el real executor compra al ask,
    no al midpoint. Así paper replica el slippage real en entry.
    Fallback a midpoint si no hay asks (mercado sin liquidez).
    Añadido L79 (2026-06-09): paper con slippage de real."""
    if not token_id:
        return None
    try:
        r = requests.get(f"{_CLOB_API}/book",
                         params={"token_id": token_id}, timeout=_TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            asks = data.get("asks") or []
            if asks:
                # asks vienen como [{price, size}, ...]; min price = best ask
                pxs = []
                for a in asks:
                    try:
                        pxs.append(float(a.get("price")))
                    except (TypeError, ValueError):
                        continue
                if pxs:
                    return min(pxs)
    except Exception:
        pass
    return _get_clob_price(token_id)


def _get_market_state(condition_id: str) -> dict:
    """Devuelve {'closed': bool, 'tokens': [{token_id, price, outcome}]} o {}."""
    if not condition_id:
        return {}
    try:
        r = requests.get(f"{_CLOB_API}/markets/{condition_id}", timeout=_TIMEOUT)
        if r.status_code != 200:
            return {}
        m = r.json()
        return {
            "closed": bool(m.get("closed")),
            "tokens": m.get("tokens") or [],
        }
    except Exception:
        return {}


def _resolution_price_for_token(market_state: dict, token_id: str) -> float | None:
    """Precio final del outcome cuando el mercado está closed=True."""
    for tok in market_state.get("tokens") or []:
        if str(tok.get("token_id")) == str(token_id):
            p = tok.get("price")
            if p is not None:
                return float(p)
    return None


# ── 1. Apertura ──────────────────────────────────────────────────────

def _get_active_agents() -> list[dict]:
    """Devuelve los agentes activos (active=1) ordenados por id."""
    conn = database.get_connection()
    c = conn.cursor()
    c.execute("""SELECT id, name, tp_multiplier, sl_multiplier, timeout_seconds
                 FROM agents WHERE active=1 ORDER BY id""")
    rows = c.fetchall()
    conn.close()
    return [{"id": r[0], "name": r[1],
             "tp_multiplier": r[2], "sl_multiplier": r[3],
             "timeout_seconds": r[4]} for r in rows]


def _real_d_mirror_would_take(signal: dict) -> bool:
    """Espejo de route_signal_to_real_d para paper D.
    Aplica los MISMOS filtros sweet-spot que el router D real (sin tocar el
    CLOB). Garantiza que paper D y real D abren EXACTAMENTE las mismas
    señales — 1:1 perfecto para validar divergencia.
    """
    import os
    # Niche check
    niche = (signal or {}).get("niche")
    if not niche:
        return False
    if niche in (config.REAL_EXCLUDED_NICHES or []):
        return False

    # Whitelist D
    try:
        import real_signal_router
        whitelist_d = real_signal_router._d_whitelist()
    except Exception:
        whitelist_d = set()
    if not whitelist_d:
        return False
    wallet = ((signal or {}).get("wallet_address")
              or (signal or {}).get("wallet")
              or (signal or {}).get("wallet_copied") or "")
    if str(wallet).lower() not in whitelist_d:
        return False

    # Blacklist global
    bl_raw = os.getenv("REAL_WALLET_BLACKLIST", "") or ""
    blacklist = {a.strip().lower() for a in bl_raw.split(",") if a.strip()}
    if str(wallet).lower() in blacklist:
        return False

    # Sweet-spot entry
    ep = signal.get("insider_price") or signal.get("entry_price") or 0
    try: ep = float(ep)
    except: ep = 0
    emin = float(os.getenv("REAL_D_ENTRY_MIN", "0.30"))
    emax = float(os.getenv("REAL_D_ENTRY_MAX", "0.50"))
    if ep < emin or ep >= emax:
        return False

    # Sweet-spot insider size
    isize = signal.get("size_usdc") or signal.get("insider_size") or 0
    try: isize = float(isize)
    except: isize = 0
    smin = float(os.getenv("REAL_D_INSIDER_SIZE_MIN", "100"))
    smax = float(os.getenv("REAL_D_INSIDER_SIZE_MAX", "2000"))
    if isize < smin or isize > smax:
        return False

    # Risk manager
    try:
        import risk_manager
        size = float(os.getenv("REAL_D_TRADE_SIZE", "3"))
        can, _ = risk_manager.can_trade(size, agent_id="D")
        if not can:
            return False
    except Exception:
        pass
    return True


def _real_mirror_would_take(signal: dict, agent_id: str) -> bool:
    """
    Determina si el router real tomaría esta señal para `agent_id`.

    Aplica filtros signal-level (niche excluido, wallet whitelist) y
    risk_manager.can_trade — los mismos que `real_signal_router.route_signal_to_real`.

    NO mira REAL_TRADING_ENABLED ni PAPER_TRADING: paper sigue como sim
    independientemente del kill switch. El propósito es que el agente real
    en paper tenga las MISMAS señales que el real en on-chain.

    Devuelve True si el real abriría este trade; False si lo filtraría.
    """
    real_agent = (config.REAL_TRADING_AGENT or "NONE").upper()
    if real_agent == "NONE":
        return False
    if agent_id != real_agent:
        return False  # solo el agente real-mirror

    # Niche filter (REAL_EXCLUDED_NICHES) — fail-closed 2026-05-23
    # niche=None ⇒ rechazo, igual que el router real (ver real_signal_router).
    niche = (signal or {}).get("niche")
    if not niche:
        return False
    if niche in (config.REAL_EXCLUDED_NICHES or []):
        return False

    # Wallet whitelist (REAL_WALLET_WHITELIST) — vía router para hot-reload
    try:
        import real_signal_router
        whitelist = real_signal_router._wallet_whitelist()
    except Exception:
        whitelist = set()
    # L59 (2026-06-03) — extra wallets observados SOLO por paper B (no real).
    # Permite validar candidatos en simulación antes de promoverlos.
    # PAPER_B_EXTRA_WALLETS no afecta al real_signal_router → real B sigue
    # limitado a REAL_WALLET_WHITELIST. Mirror_B metric NO los considera
    # porque no hay pares real correspondientes.
    try:
        import os
        extra_raw = os.getenv("PAPER_B_EXTRA_WALLETS", "").strip()
        extra_wl = {a.strip().lower() for a in extra_raw.split(",") if a.strip()}
    except Exception:
        extra_wl = set()
    combined_wl = whitelist | extra_wl
    if combined_wl:
        wallet = ((signal or {}).get("wallet_address")
                  or (signal or {}).get("wallet")
                  or (signal or {}).get("wallet_copied") or "")
        if str(wallet).lower() not in combined_wl:
            return False

    # L52 (2026-06-02) — espejo perfecto del real router B.
    # Antes faltaban estos filtros, causando que paper B abriera trades
    # que real B rechazaba → divergencia falsa que ocultaba bugs reales.
    # Ahora paper B = real B 1:1 a nivel de señal de entrada.

    # Filtro entry_price MAX (REAL_B_ENTRY_MAX o REAL_MAX_ENTRY_PRICE)
    try:
        import os
        max_raw = (os.getenv("REAL_B_ENTRY_MAX", "")
                   or os.getenv("REAL_MAX_ENTRY_PRICE", "")).strip()
        if max_raw:
            max_entry = float(max_raw)
            ep_cand = ((signal or {}).get("insider_price")
                       or (signal or {}).get("entry_price")
                       or (signal or {}).get("price"))
            if ep_cand is not None and float(ep_cand) >= max_entry:
                return False
    except Exception:
        pass

    # Filtro entry_price MIN (REAL_B_ENTRY_MIN)
    try:
        import os
        min_raw = os.getenv("REAL_B_ENTRY_MIN", "").strip()
        if min_raw:
            min_entry = float(min_raw)
            ep_cand = ((signal or {}).get("insider_price")
                       or (signal or {}).get("entry_price")
                       or (signal or {}).get("price"))
            if ep_cand is not None and float(ep_cand) < min_entry:
                return False
    except Exception:
        pass

    # Risk manager
    try:
        import risk_manager
        can, _ = risk_manager.can_trade(config.MAX_TRADE_SIZE_USDC,
                                         agent_id=agent_id)
        if not can:
            return False
    except Exception:
        pass

    return True


def evaluate_signal_for_paper_trade(signal: dict) -> list[int]:
    """
    Llamado desde monitor.process_new_trade tras pasar todos los filtros.

    `signal` debe contener:
      detected_trade_id, wallet, market_id, token_id, question,
      outcome_name, side, niche, insider_price (precio del insider)

    Fase 2.6: por cada señal entrante crea N paper_trades (uno por agente
    activo en `agents`). Cada agente snapshotea su tp/sl/timeout al abrir.

    Devuelve lista de paper_trade IDs creados (vacía si veto / sin token / etc).
    """
    if not config.PAPER_TRADING_ENABLED:
        return []

    dt_id = signal.get("detected_trade_id")
    if not dt_id:
        return []

    # Bloquear señales antiguas del lookback de startup (mismo filtro que real router).
    # Evita la avalancha de N trades en los primeros segundos tras arranque.
    # DEF wallets: sin límite de edad — sus señales son siempre válidas (REINFORCE acumulado).
    _DEF_WALLETS_ALL = {
        '0x204f72f35326db932158cba6adff0b9a1da95e14',  # Swisstony
        '0x84dbb7103982e3617704a2ed7d5b39691952aeeb',  # Soarin22
        '0xc96e5287ab294ac0388c2ddb00180fc464cff1f9',  # wigglew
        '0xa6d9b55a6a3a54a9d50ca94c64f00af69c50fd2f',  # kinderSman
    }
    _sig_wallet = ((signal or {}).get('wallet') or '').lower()
    import time as _time
    _sig_ts = signal.get("timestamp")
    if _sig_ts and _sig_wallet not in _DEF_WALLETS_ALL:
        try:
            _age_secs = _time.time() - float(_sig_ts)
            if _age_secs > 80:
                print(f"[PAPER] SKIP señal antigua ({_age_secs:.0f}s) dt={dt_id} — lookback only")
                return []
        except (TypeError, ValueError):
            pass

    # Token id: preferir el del payload del monitor, fallback al CLOB
    token_id = signal.get("token_id")
    if not token_id:
        token_id = _resolve_token_id_from_market(signal.get("market_id"), signal.get("outcome_name"))
        if not token_id:
            print(f"[PAPER] No token_id para señal {dt_id} — skip")
            return []

    # ── L81 — REAL TRADING PRIMERO, antes de cualquier overhead de paper ──────
    # route_signal_to_real_copy tiene sus propios guards internos (kill switches,
    # risk_manager, threshold, etc.) y no depende del precio paper ni de que los
    # paper trades estén escritos. Moverlo aquí elimina ~300-500ms de latencia
    # (tiempo de _get_best_ask HTTP + DB writes × N agentes) del camino crítico.
    try:
        import real_signal_router as _rsr_early
        signal_for_copy_early = dict(signal)
        signal_for_copy_early["token_id"] = token_id
        routed_copy_early = _rsr_early.route_signal_to_real_copy(signal_for_copy_early)
        if routed_copy_early:
            print(f"[PAPER] Señal {dt_id} enviada a REAL DEF_COPY (early-path L81)")
    except Exception as _e_copy_early:
        print(f"[ROUTER_COPY] error early-path: {_e_copy_early}")

    # Risk manager check (Fase 2.5) — un solo check para todos los agentes paper
    try:
        import risk_manager
        veto, reason, countdown = risk_manager.has_veto()
        if veto:
            cd = f" (countdown={countdown}s)" if countdown else " (permanente)"
            print(f"[RISK] paper_trade NO abierto — veto activo: {reason}{cd}")
            return []
    except Exception as e:
        print(f"[RISK] check error (continuando sin veto): {e}")

    # Precio actual CLOB (entry) — best_ask para replicar slippage real (L79)
    # El real executor compra al ask, no al mid → paper usa best_ask también.
    current_price = _get_best_ask(token_id)
    if current_price is None or current_price <= 0:
        insider_price = float(signal.get("insider_price") or 0)
        if insider_price <= 0:
            print(f"[PAPER] CLOB sin precio para signal {dt_id} — skip")
            return []
        current_price = insider_price

    # Idempotencia: si ya existe paper_trade para (detected_trade_id, agent_id),
    # no duplicar. La UNIQUE original sobre detected_trade_id ya no aplica
    # (se eliminó al migrar a multi-agente — verificamos manualmente).
    agents = _get_active_agents()
    if not agents:
        print(f"[PAPER] No hay agentes activos — skip")
        return []

    conn = database.get_connection()
    c = conn.cursor()
    c.execute("SELECT agent_id FROM paper_trades WHERE detected_trade_id=?", (dt_id,))
    existing = {r[0] for r in c.fetchall()}
    conn.close()

    created = []
    agent_decisions = {}   # Fase 3.1 — decisión por agente para el router real
    real_agent_id = (config.REAL_TRADING_AGENT or "NONE").upper()
    mid_for_lock = (signal or {}).get('market_id', '')
    for ag in agents:
        if ag["id"] in existing:
            continue
        # Race-condition fix (2026-06-10): los DEF agents usan un in-memory lock
        # para evitar que fast_loop (15s) y slow_loop (60s) abran el mismo trade
        # simultáneamente (2 trades idénticos a 0ms → volumen duplicado).
        if ag["id"] in ('DEF_TP', 'DEF_HOLD', 'DEF_COPY', 'DEF_COPY_05') and mid_for_lock:
            _lock_key = f"{ag['id']}:{mid_for_lock}"
            with _PAPER_DEF_OPEN_LOCK:
                if _lock_key in _PAPER_DEF_OPENING:
                    continue  # otro thread ya está abriendo este trade — skip
                _PAPER_DEF_OPENING.add(_lock_key)
            # Abrir el trade; siempre liberar el lock al final (éxito o excepción)
            try:
                pt_id = _open_paper_trade(signal, current_price, token_id, ag)
            finally:
                with _PAPER_DEF_OPEN_LOCK:
                    _PAPER_DEF_OPENING.discard(_lock_key)
            if pt_id:
                created.append(pt_id)
                agent_decisions[ag["id"]] = {
                    "action": "open", "entry_price": current_price,
                    "size": config.PAPER_SIZE_USDC, "paper_trade_id": pt_id,
                }
            continue  # skip el pt_id = _open_paper_trade(...) de abajo
        # L60 (2026-06-03) — Mirror filter desactivado para paper B.
        # Histórico: el mirror filter restringía paper B a REAL_WALLET_WHITELIST
        # para garantizar correspondencia 1:1 con real B. Pero corté 95% del
        # volumen el 20-may (de 95 trades/día → 5). Decisión del usuario:
        # paper B vuelve a su comportamiento original (toma cualquier señal
        # del monitor) con $100 nominal. La métrica B_MIRROR sigue calculándose
        # via JOIN paper↔real → solo cuenta pares reales (no afectado).
        # Para mantener el diagnóstico mirror_B útil, se filtra por
        # REAL_WALLET_WHITELIST a nivel de check, no de captura.
        pass  # mirror filter deshabilitado — paper B abre para cualquier wallet activa
        # Paper D debe espejar Real D (sweet-spot filters). Si no pasa el
        # mirror, no abrir paper D para que ambos sean 1:1.
        if ag["id"] == "D" and not _real_d_mirror_would_take(signal):
            print(f"[PAPER:D] skip — real_d_mirror filter "
                  f"(sweet-spot) para señal {dt_id}")
            continue
        pt_id = _open_paper_trade(signal, current_price, token_id, ag)
        if pt_id:
            created.append(pt_id)
            agent_decisions[ag["id"]] = {
                "action":      "open",
                "entry_price": current_price,
                "size":        config.PAPER_SIZE_USDC,
                "paper_trade_id": pt_id,
            }

    # ── Fase 3.1 — Capa Agente → Real Trading B ─────────────────────────────
    try:
        import real_signal_router
        signal_for_router = dict(signal)
        signal_for_router["token_id"] = token_id  # asegurar token resuelto
        routed = real_signal_router.route_signal_to_real(signal_for_router, agent_decisions)
        if routed:
            print(f"[PAPER] Señal {dt_id} también enviada a REAL B "
                  f"(agente {config.REAL_TRADING_AGENT})")
    except Exception as e:
        print(f"[REAL_ROUTER] error (paper trading no afectado): {e}")

    # ── Fase D (2026-05-28) — Capa Agente → Real Trading D ───────────────
    # Router D paralelo para wallets con edge hold-to-resolve (ej. swisstony).
    # Se ejecuta INDEPENDIENTEMENTE de B — una señal puede ir a D y NO a B
    # (si está en REAL_D_WALLET_WHITELIST pero no en REAL_WALLET_WHITELIST).
    try:
        import real_signal_router as _rsr
        signal_for_d = dict(signal)
        signal_for_d["token_id"] = token_id
        routed_d = _rsr.route_signal_to_real_d(signal_for_d)
        if routed_d:
            print(f"[PAPER] Señal {dt_id} también enviada a REAL D (hold-to-resolve)")
    except Exception as e:
        print(f"[ROUTER_D] error (paper trading no afectado): {e}")

    # L81: route_signal_to_real_copy movido al inicio de esta función (early-path)
    # para eliminar latencia de _get_best_ask + DB writes del camino crítico real.
    # La llamada ya no está aquí — se ejecutó antes de _get_best_ask arriba.

    return created


def _resolve_token_id_from_market(condition_id: str, outcome_name: str) -> str | None:
    """Si el monitor no envió token_id, lo derivamos del CLOB market."""
    if not condition_id:
        return None
    state = _get_market_state(condition_id)
    target = (outcome_name or "").upper().strip()
    for tok in state.get("tokens") or []:
        if (tok.get("outcome") or "").upper().strip() == target:
            return str(tok.get("token_id"))
    return None


def _open_paper_trade(signal: dict, entry_price: float, token_id: str,
                      agent: dict) -> int | None:
    """
    Abre un paper_trade para el agente especificado.
    Snapshotea tp/sl/timeout del agente — cambios futuros en config/agents
    no mutan posiciones abiertas.
    Si tp_multiplier o sl_multiplier son None (agente hold-to-resolve),
    los precios target se persisten como NULL.
    """
    _paper_agg_key = None   # se rellena en bloque DEF_COPY si delta-check OK
    _paper_agg_val = None

    # L61 (2026-06-03) — size per-agent.
    import os as _os
    _ag_size_env = _os.getenv(f"PAPER_{agent['id']}_SIZE_USDC", "").strip()
    if _ag_size_env:
        try:
            size_usdc = float(_ag_size_env)
        except ValueError:
            size_usdc = config.PAPER_SIZE_USDC
    else:
        size_usdc = config.PAPER_SIZE_USDC

    # L76 (2026-06-05) — Agente B reciclado al modelo DEF_COPY:
    #   sin TP/SL · umbral $50 cumulado/reincidencia · exit por SELL+MERGE.
    #   Aprovecha el pool de wallets que B ya rastreaba (todas las que pasan
    #   el monitor) y aplica copia 1-1 a escala uniforme.
    if agent.get('id') == 'B':
        wallet = ((signal or {}).get('wallet') or (signal or {}).get('wallet_address') or '').lower()
        if not wallet:
            return None
        niche = (signal or {}).get('niche', '')
        # mantén filtro niche permisivo: solo descartar lo que monitor ya filtra
        mid = (signal or {}).get('market_id','')
        if not mid:
            return None
        try:
            THRESHOLD = float(_os.getenv('B_THRESHOLD','50'))
            SCALE = float(_os.getenv('B_SCALE','0.00784'))
            MAX_T = float(_os.getenv('B_MAX_PER_TRADE','10'))
            MIN_T = float(_os.getenv('B_MIN_TRADE','1'))
            conn_b = database.get_connection()
            last_open_row = conn_b.execute(
                """SELECT MAX(entry_at) FROM paper_trades
                   WHERE agent_id='B' AND LOWER(wallet_copied)=? AND market_id=?""",
                (wallet, mid)).fetchone()
            last_open_at = last_open_row[0] if last_open_row else None
            if last_open_at:
                agg_row = conn_b.execute(
                    """SELECT COALESCE(SUM(size_usdc),0) FROM detected_trades
                       WHERE LOWER(wallet_address)=? AND market_id=?
                         AND detected_at > ?""",
                    (wallet, mid, last_open_at)).fetchone()
                label = "REINFORCE"
            else:
                agg_row = conn_b.execute(
                    """SELECT COALESCE(SUM(size_usdc),0) FROM detected_trades
                       WHERE LOWER(wallet_address)=? AND market_id=?""",
                    (wallet, mid)).fetchone()
                label = "FIRST"
            conn_b.close()
            agg = float(agg_row[0] or 0)
            if agg < THRESHOLD:
                return None
            sc = min(agg * SCALE, MAX_T)
            sc = max(sc, MIN_T)
            size_usdc = round(sc, 2)
            print(f"[PAPER:B] {label} agg ${agg:.0f}×{SCALE}=${size_usdc} · wallet={wallet[:10]}… mkt={mid[:14]}… [{niche}]")
        except Exception as _e:
            print(f"[PAPER:B] err: {_e}")
            return None

    # L67 (2026-06-04) — DEFINITIVE shadows: 3 agentes paralelos por señal copy-trader.
    # L69 (2026-06-04) — Multi-trader: ahora Lily + Swisstony con config dedicada por wallet.
    #   DEF_TP   = TP 1.7x / SL 0.5x (B-style)
    #   DEF_HOLD = sin TP/SL, espera resolución
    #   DEF_COPY = sin TP/SL, sale cuando el trader vende (background loop)
    # Cada trader tiene su propia escala basada en su bankroll proxy estimado.
    # L86 (2026-06-06) — Paper es la VERDAD. Paper abre siempre según su lógica;
    # real intenta mirrorearlo pero puede fallar técnicamente. NO se cancela
    # el paper por fallo del real.
    if agent.get('id') in ('DEF_TP', 'DEF_HOLD', 'DEF_COPY', 'DEF_COPY_05', 'DEF_WIGGLEW', 'DEF_KINDER'):
        # L74 (2026-06-05) — Umbral único $50 siempre, sin first-touch:
        #   - Toda entrada exige agg ≥ DEFINITIVE_THRESHOLD ($50).
        #   - Si no hay open previo: agg = suma de TODAS las detected_trades del
        #     wallet en el mercado (incluye señal actual).
        #   - Si ya abrimos antes: agg = suma desde el último open (REINFORCE).
        #   - Size siempre proporcional: agg × scale, capado [$1, $10].
        # L77 (2026-06-06) — DEF_COPY solo Swisstony; DEF_COPY_05 Lily+Swisstony.
        DEF_TRADERS_FULL = {
            '0x204f72f35326db932158cba6adff0b9a1da95e14': {
                'name': 'Swisstony',
                'scale': float(_os.getenv('DEFINITIVE_SWIS_SCALE','0.00784')),
            },
            '0x84dbb7103982e3617704a2ed7d5b39691952aeeb': {
                'name': 'Soarin22',
                'scale': float(_os.getenv('DEFINITIVE_SOARIN_SCALE','0.005')),
            },
            '0xc96e5287ab294ac0388c2ddb00180fc464cff1f9': {
                'name': 'wigglew',
                'scale': float(_os.getenv('DEFINITIVE_WIGGLEW_SCALE','0.02')),
            },
            '0xa6d9b55a6a3a54a9d50ca94c64f00af69c50fd2f': {
                'name': 'kinderSman',
                'scale': float(_os.getenv('DEFINITIVE_KINDER_SCALE','0.01')),
            },
        }
        # Routing: cada agente DEF_* copia solo su trader asignado
        _AGENT_WALLET = {
            'DEF_COPY_05':  '0x84dbb7103982e3617704a2ed7d5b39691952aeeb',
            'DEF_WIGGLEW':  '0xc96e5287ab294ac0388c2ddb00180fc464cff1f9',
            'DEF_KINDER':   '0xa6d9b55a6a3a54a9d50ca94c64f00af69c50fd2f',
        }
        _assigned = _AGENT_WALLET.get(agent.get('id'))
        if _assigned:
            DEF_TRADERS = {_assigned: DEF_TRADERS_FULL[_assigned]}
        else:
            # DEF_COPY / DEF_TP / DEF_HOLD → Swisstony
            DEF_TRADERS = {
                '0x204f72f35326db932158cba6adff0b9a1da95e14': DEF_TRADERS_FULL['0x204f72f35326db932158cba6adff0b9a1da95e14'],
            }
        wallet = ((signal or {}).get('wallet') or (signal or {}).get('wallet_address') or '').lower()
        cfg = DEF_TRADERS.get(wallet)
        if not cfg:
            return None
        niche = (signal or {}).get('niche', '') or 'sports'
        mid = (signal or {}).get('market_id','')
        if not mid:
            return None
        try:
            _AGENT_THRESHOLD = {'DEF_COPY_05': 500.0, 'DEF_WIGGLEW': 300.0, 'DEF_KINDER': 500.0}
            _AGENT_MAX = {'DEF_COPY_05': 30.0, 'DEF_WIGGLEW': 20.0, 'DEF_KINDER': 20.0}
            THRESHOLD = _AGENT_THRESHOLD.get(agent.get('id'), float(_os.getenv('DEFINITIVE_THRESHOLD','300')))
            MAX_T = _AGENT_MAX.get(agent.get('id'), float(_os.getenv('DEFINITIVE_MAX_PER_TRADE','20')))
            MIN_T = float(_os.getenv('DEFINITIVE_MIN_TRADE','1'))
            conn_a = database.get_connection()
            # Solo considerar trades NO cancelados como "ya abiertos".
            # Trades cancelados por reset no deben bloquear re-entrada.
            last_open_row = conn_a.execute(
                """SELECT MAX(entry_at) FROM paper_trades
                   WHERE agent_id=? AND LOWER(wallet_copied)=? AND market_id=?
                     AND status != 'cancelled'""",
                (agent['id'], wallet, mid)).fetchone()
            last_open_at = last_open_row[0] if last_open_row else None

            # Fix C1/C2 (2026-06-18): usar acumulado REAL desde detected_trades en BD.
            # El acumulador in-memory (display_size en monitor) se pierde en cada
            # restart → señales pequeñas post-restart nunca superaban el threshold.
            # También recupera señales llegadas durante downtime (causa 2):
            # basta con que llegue cualquier señal posterior y se comprueba el total.
            signal_size = float((signal or {}).get('size_usdc') or (signal or {}).get('size') or 0)
            try:
                _since = last_open_at or '2000-01-01'
                _agg_row = conn_a.execute("""
                    SELECT COALESCE(MAX(size_usdc), 0)
                    FROM detected_trades
                    WHERE wallet_address=? AND market_id=? AND detected_at >= ?
                """, (wallet, mid, _since)).fetchone()
                # MAX(size_usdc) es el acumulado más reciente (cada fila guarda el total hasta ese momento)
                agg = max(float(_agg_row[0] or 0), signal_size)
            except Exception:
                agg = signal_size
            conn_a.close()
            label = "REINFORCE" if last_open_at else "FIRST"
            if agg < THRESHOLD:
                return None
            # Delta-check REINFORCE: solo re-entramos cuando el acumulado ha crecido
            # en al menos REINFORCE_THRESHOLD desde la última apertura.
            # Fix C3 (2026-06-18): REINFORCE_THRESHOLD = THRESHOLD/2 ($250) para
            # capturar señales medianas de Swis ($200-$400) que acumulan volumen real
            # pero no alcanzaban el delta de $500 entre re-entradas.
            REINFORCE_THRESHOLD = THRESHOLD / 2
            _agg_key = f"{agent['id']}:{wallet}:{mid}"
            _last_entry = _PAPER_COPY_LAST_AGG.get(_agg_key)
            _incoming_token = token_id or (signal or {}).get("token_id") or ""
            if _last_entry is not None:
                _last_token = _last_entry["token_id"] if isinstance(_last_entry, dict) else ""
                _last_agg = _last_entry["agg"] if isinstance(_last_entry, dict) else float(_last_entry)
                if _last_token and _incoming_token and _last_token == _incoming_token:
                    # Mismo token: REINFORCE — requiere delta >= REINFORCE_THRESHOLD
                    if agg - _last_agg < REINFORCE_THRESHOLD:
                        return None  # REINFORCE insuficiente — silencioso
                # Token distinto (cobertura): tratar como posición nueva — pasa
            # Delta check OK — guardar para actualizar dict al final si el trade abre
            _paper_agg_key = _agg_key
            _paper_agg_val = {"agg": agg, "token_id": token_id or _incoming_token}
            # Guard: no abrir si el mercado ya está prácticamente resuelto
            # >0.97: resolución inminente (mercado ya cerró o precio final),
            #        sin liquidez, ganancia máxima <3% — no vale el gas.
            # <0.05: mercado casi perdido, sin liquidez al otro lado.
            # NOTA (2026-06-10): subido 0.90→0.97 porque Swisstony usa "No" bets
            # en partidos de Copa del Mundo a 88-97¢ (alta confianza, correcto
            # precio de mercado). El guard 0.90 bloqueaba estas entradas.
            _ep = float(entry_price or 0)
            if _ep < 0.05 or _ep > 0.95:
                print(f"[PAPER:{agent['id']}] RESOLVED-SKIP p={_ep:.4f} · {mid[:14]}… [{niche}]")
                return None
            sc = min(agg * cfg['scale'], MAX_T)
            sc = max(sc, MIN_T)
            size_usdc = round(sc, 2)
            # L89 (2026-06-07) — Test paper · min 5.5 shares para simular el
            # mínimo de Polymarket (Size >= 5). Si el size en USDC no llega
            # a 5.5 shares, lo subimos hasta 5.5 shares (con tope MAX_T).
            try:
                _ep = float((signal or {}).get('insider_price') or 0) or float(entry_price or 0)
                if _ep > 0:
                    min_usdc_5sh = round(5.5 * _ep, 2)
                    if size_usdc < min_usdc_5sh:
                        bumped_from = size_usdc
                        size_usdc = min(min_usdc_5sh, MAX_T)
                        print(f"[PAPER:{agent['id']}] {cfg['name']} {label} agg ${agg:.0f}"
                              f"×{cfg['scale']}=${bumped_from} · BUMP→${size_usdc} "
                              f"(min 5.5sh @ {_ep:.3f}) · {mid[:14]}… [{niche}]")
                    else:
                        print(f"[PAPER:{agent['id']}] {cfg['name']} {label} agg ${agg:.0f}"
                              f"×{cfg['scale']}=${size_usdc} · {mid[:14]}… [{niche}]")
                else:
                    print(f"[PAPER:{agent['id']}] {cfg['name']} {label} agg ${agg:.0f}"
                          f"×{cfg['scale']}=${size_usdc} · {mid[:14]}… [{niche}]")
            except Exception:
                print(f"[PAPER:{agent['id']}] {cfg['name']} {label} agg ${agg:.0f}"
                      f"×{cfg['scale']}=${size_usdc} · {mid[:14]}… [{niche}]")
        except Exception as _e:
            print(f"[PAPER:{agent['id']}] err: {_e}")
            return None
    tp_mul    = agent.get("tp_multiplier")
    sl_mul    = agent.get("sl_multiplier")
    timeout_s = agent.get("timeout_seconds", 7 * 86400)
    # L64 (2026-06-03) — fees proporcionales al tamaño, no fijo $0.30.
    # Antes: PAPER_FEES_ESTIMATED=$0.30 fijo. Para trades de $100 era 0.3%
    # razonable. Para $5 era 6% — absurdo. Entries cerca de resolución
    # (>0.90) resultaban en "wins" negativos de céntimos porque fees fantasma
    # se comían el gross. Ahora 2% del cost (típico Polymarket maker).
    fees      = round(size_usdc * 0.02, 4)

    # Para hold-to-resolve (tp_mul=None) almacenamos 0.0 en columnas NOT NULL.
    # La lógica de check diferencia por la columna is_hold_to_resolve.
    is_h2r = (tp_mul is None or sl_mul is None)
    tp_price_db = round(entry_price * tp_mul, 6) if tp_mul else 0.0
    sl_price_db = round(entry_price * sl_mul, 6) if sl_mul else 0.0
    tp_mul_db   = tp_mul if tp_mul is not None else 0.0
    sl_mul_db   = sl_mul if sl_mul is not None else 0.0
    shares   = round(size_usdc / entry_price, 6) if entry_price > 0 else 0

    conn = database.get_connection()
    c = conn.cursor()
    c.execute("""
        INSERT INTO paper_trades (
            detected_trade_id, wallet_copied, market_id, token_id,
            market_question, outcome_name, side, niche,
            entry_at, entry_price, entry_size_usdc, entry_shares,
            tp_target_price, sl_target_price, tp_multiplier, sl_multiplier,
            status, fees_estimated, agent_id, timeout_seconds, is_hold_to_resolve
        ) VALUES (?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?, 'open', ?, ?, ?, ?)
    """, (
        signal["detected_trade_id"], signal.get("wallet"),
        signal.get("market_id"), token_id,
        (signal.get("question") or "")[:200], signal.get("outcome_name"),
        signal.get("side"), signal.get("niche"),
        _now_iso(), entry_price, size_usdc, shares,
        tp_price_db, sl_price_db, tp_mul_db, sl_mul_db,
        fees, agent["id"], timeout_s, 1 if is_h2r else 0,
    ))
    pt_id = c.lastrowid
    conn.commit()
    conn.close()

    if not is_h2r:
        print(f"[PAPER][{agent['id']}] Paper #{pt_id} abierto · entry {entry_price:.4f} · "
              f"TP {tp_price_db:.4f} · SL {sl_price_db:.4f}")
    else:
        print(f"[PAPER][{agent['id']}] Paper #{pt_id} abierto · entry {entry_price:.4f} · "
              f"hold-to-resolve (timeout {timeout_s//86400}d)")

    # Actualizar delta-check (mirror _COPY_LAST_AGG de real_signal_router).
    if _paper_agg_key is not None and _paper_agg_val is not None:
        _PAPER_COPY_LAST_AGG[_paper_agg_key] = _paper_agg_val

    return pt_id


# ── 2. Loop de comprobación cada 60s ─────────────────────────────────

def check_open_paper_trades(only_agent: str = None) -> dict:
    """
    Para cada paper abierto, respetando reglas por agente:
      1. Si market closed → resolve.
      2. Si timeout (entry_at + timeout_seconds) → close as 'timeout'.
      3. Si tp/sl_target_price NO es NULL → checkear TP/SL.
         Si NULL (agente hold-to-resolve) → solo resolución o timeout.
      4. Else: actualizar last_check_*.

    Si `only_agent` se pasa (ej. 'B'), solo procesa los trades de ese agente.
    Útil para correr el mirror del agente real a la misma cadencia que el
    fast_price_check del executor real (Fase L28 — 2026-05-30).
    """
    conn = database.get_connection()
    c = conn.cursor()
    if only_agent:
        c.execute("""
            SELECT id, market_id, token_id, entry_at, entry_price, entry_shares,
                   tp_target_price, sl_target_price, side, fees_estimated,
                   agent_id, timeout_seconds, is_hold_to_resolve
            FROM paper_trades WHERE status = 'open' AND agent_id = ?
        """, (only_agent,))
    else:
        c.execute("""
            SELECT id, market_id, token_id, entry_at, entry_price, entry_shares,
                   tp_target_price, sl_target_price, side, fees_estimated,
                   agent_id, timeout_seconds, is_hold_to_resolve
            FROM paper_trades WHERE status = 'open'
        """)
    rows = c.fetchall()
    conn.close()

    stats = Counter()
    stats["checked"] = len(rows)

    for r in rows:
        (pt_id, market_id, token_id, entry_at, entry_price,
         shares, tp_price, sl_price, side, fees,
         agent_id, timeout_seconds, is_h2r) = r

        # 1) ¿market resuelto?
        market_state = _get_market_state(market_id)
        if market_state.get("closed"):
            res_price = _resolution_price_for_token(market_state, token_id)
            if res_price is not None:
                _resolve_paper_trade(pt_id, res_price, entry_price, shares, fees)
                if res_price >= 0.99:
                    stats["resolved_win"] += 1
                elif res_price <= 0.01:
                    stats["resolved_loss"] += 1
                else:
                    stats["disputed"] += 1
                continue

        # 2) Timeout — usa el snapshot por trade (timeout_seconds del agente)
        try:
            entry_dt = datetime.fromisoformat(entry_at)
        except Exception:
            entry_dt = datetime.utcnow()
        # Fallback al global si la columna no existía (legacy)
        to_s = timeout_seconds if timeout_seconds else config.PAPER_TIMEOUT_DAYS * 86400
        if (datetime.utcnow() - entry_dt).total_seconds() >= to_s:
            cur = _get_clob_price(token_id) or entry_price
            _close_paper_trade(pt_id, cur, "timeout", "timeout", entry_price, shares, fees)
            stats["timeout"] += 1
            continue

        # 3) TP/SL — solo si NO es hold-to-resolve
        if is_h2r:
            cur = _get_clob_price(token_id)
            if cur is not None:
                _update_last_check(pt_id, cur)
            continue

        # Para el agente real-mirror (B por defecto) usamos best_bid (precio
        # ejecutable al vender) en vez de midpoint. Así paper no declara
        # TP_HIT a 0.62 cuando el orderbook solo paga 0.05. Fix divergencia
        # 2026-05-23. Otros agentes (A, C) siguen con midpoint — son
        # simulaciones puras, no se ejecutan en real.
        real_agent = (config.REAL_TRADING_AGENT or "NONE").upper()
        if agent_id == real_agent and agent_id != "NONE":
            cur = _get_executable_sell_price(token_id)
        else:
            cur = _get_clob_price(token_id)

        if cur is None:
            continue
        if cur >= tp_price:
            _close_paper_trade(pt_id, cur, "tp_hit", "tp_hit", entry_price, shares, fees)
            stats["tp_hit"] += 1
        elif cur <= sl_price:
            _close_paper_trade(pt_id, cur, "sl_hit", "sl_hit", entry_price, shares, fees)
            stats["sl_hit"] += 1
        else:
            _update_last_check(pt_id, cur)

    stats["closed"] = (stats["tp_hit"] + stats["sl_hit"] +
                       stats["resolved_win"] + stats["resolved_loss"] +
                       stats["disputed"] + stats["timeout"])
    return dict(stats)


def _close_paper_trade(pt_id, exit_price, status, reason,
                       entry_price, shares, fees) -> None:
    gross = round(shares * (exit_price - entry_price), 4)
    net   = round(gross - (fees or 0), 4)
    conn = database.get_connection()
    c = conn.cursor()
    # H-3 FIX (2026-06-10): guard atómico anti-double-close.
    # paper_monitor_loop (60s) y paper_b_fast_loop (15s) pueden llamar a
    # check_open_paper_trades simultáneamente. Sin este guard, ambos leen
    # status='open', ambos calculan PnL, y el segundo UPDATE escribe un
    # PnL incorrecto (las shares ya fueron "vendidas" por el primero).
    # Solución: añadir AND status='open' al WHERE. SQLite garantiza atomicidad
    # del UPDATE — solo uno tendrá rowcount=1, el otro rowcount=0.
    c.execute("""
        UPDATE paper_trades
        SET status=?, exit_at=?, exit_price=?, exit_reason=?,
            realized_pnl_gross=?, realized_pnl_net=?,
            last_check_at=?, last_check_price=?
        WHERE id = ? AND status = 'open'
    """, (status, _now_iso(), exit_price, reason, gross, net,
          _now_iso(), exit_price, pt_id))
    affected = c.rowcount
    conn.commit()
    conn.close()
    if affected == 0:
        # Ya fue cerrado por otro path concurrente — silencioso, no es un error
        return
    print(f"[PAPER] {status.upper():12s} #{pt_id} @ {exit_price:.4f} → "
          f"PnL gross ${gross:+.2f} · net ${net:+.2f}")


def _resolve_paper_trade(pt_id, res_price, entry_price, shares, fees) -> None:
    if res_price >= 0.99:
        status, reason = "resolved_win", "resolved_win"
    elif res_price <= 0.01:
        status, reason = "resolved_loss", "resolved_loss"
    else:
        status, reason = "disputed", f"disputed_at_{res_price:.3f}"
    _close_paper_trade(pt_id, res_price, status, reason,
                       entry_price, shares, fees)


def _update_last_check(pt_id, cur) -> None:
    conn = database.get_connection()
    c = conn.cursor()
    c.execute("""UPDATE paper_trades
                 SET last_check_at=?, last_check_price=? WHERE id=?""",
              (_now_iso(), cur, pt_id))
    conn.commit()
    conn.close()


# ── 3. Stats para dashboard ──────────────────────────────────────────

def _avg(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 1) if xs else None


def _hours_between(iso_a: str, iso_b: str) -> float | None:
    if not iso_a or not iso_b:
        return None
    try:
        a = datetime.fromisoformat(iso_a)
        b = datetime.fromisoformat(iso_b)
        return max(0.0, (b - a).total_seconds() / 3600)
    except Exception:
        return None


def get_paper_stats(window_days: int | None = None) -> dict:
    """Stats agregadas para el dashboard."""
    conn = database.get_connection()
    c = conn.cursor()
    where = ""
    args = []
    if window_days:
        cutoff = (datetime.utcnow() - timedelta(days=window_days)).isoformat()
        where = "WHERE entry_at >= ?"
        args = [cutoff]
    # Excluir agent_id='A_legacy' del listado del experimento
    legacy_filter = "agent_id != 'A_legacy'"
    where_full = f"WHERE {legacy_filter}" + (f" AND entry_at >= ?" if window_days else "")
    c.execute(f"""SELECT id, status, entry_at, exit_at, entry_price, exit_price,
                         realized_pnl_gross, realized_pnl_net, fees_estimated,
                         market_question, wallet_copied, side, outcome_name,
                         tp_target_price, sl_target_price, agent_id
                  FROM paper_trades {where_full}
                  ORDER BY entry_at DESC""", args)
    rows = c.fetchall()
    conn.close()

    by_status = Counter(r[1] for r in rows)
    closed_statuses = {"tp_hit","sl_hit","resolved_win","resolved_loss","disputed","timeout"}
    win_statuses    = {"tp_hit","resolved_win"}
    closed_rows = [r for r in rows if r[1] in closed_statuses]
    win_rows    = [r for r in rows if r[1] in win_statuses]

    pnl_gross = sum((r[6] or 0) for r in closed_rows)
    pnl_net   = sum((r[7] or 0) for r in closed_rows)
    fees_sum  = sum((r[8] or 0) for r in closed_rows)

    # Tiempos por categoría
    times_tp        = [_hours_between(r[2], r[3]) for r in rows if r[1]=="tp_hit"]
    times_sl        = [_hours_between(r[2], r[3]) for r in rows if r[1]=="sl_hit"]
    times_resolved  = [_hours_between(r[2], r[3]) for r in rows if r[1] in ("resolved_win","resolved_loss","disputed")]
    times_timeout   = [_hours_between(r[2], r[3]) for r in rows if r[1]=="timeout"]

    total_closed = len(closed_rows)

    stats = {
        "total":         len(rows),
        "open":          by_status.get("open", 0),
        "tp_hit":        by_status.get("tp_hit", 0),
        "sl_hit":        by_status.get("sl_hit", 0),
        "resolved_win":  by_status.get("resolved_win", 0),
        "resolved_loss": by_status.get("resolved_loss", 0),
        "disputed":      by_status.get("disputed", 0),
        "timeout":       by_status.get("timeout", 0),
        "closed":        total_closed,
        "wins":          len(win_rows),
        "win_rate":      round(len(win_rows) / total_closed * 100, 1) if total_closed else 0,
        "pnl_gross":     round(pnl_gross, 2),
        "pnl_net":       round(pnl_net, 2),
        "fees_sum":      round(fees_sum, 2),
        "fee_impact_pct": (
            round(abs(fees_sum / pnl_gross) * 100, 1)
            if pnl_gross else None
        ),
        "avg_time_to_tp_hours":         _avg(times_tp),
        "avg_time_to_sl_hours":         _avg(times_sl),
        "avg_time_to_resolution_hours": _avg(times_resolved),
        "avg_time_to_timeout_hours":    _avg(times_timeout),
    }

    # Tabla recent (últimos 60 — más espacio para filtrar por agente)
    recent = []
    for r in rows[:60]:
        recent.append({
            "id":          r[0],
            "status":      r[1],
            "entry_at":    r[2],
            "exit_at":     r[3],
            "entry_price": r[4],
            "exit_price":  r[5],
            "pnl_net":     r[7],
            "market":      (r[9] or "?")[:60],
            "wallet":      (r[10] or "")[:8] + "…" if r[10] else "?",
            "side":        r[11],
            "outcome":     r[12],
            "tp":          r[13],
            "sl":          r[14],
            "agent_id":    r[15],
        })

    return {
        "stats":   stats,
        "recent":  recent,
        "by_status": dict(by_status),
    }


def get_comparison_with_manual(window_days: int = 14) -> dict:
    """
    Compara paper_trades vs manual_trades resueltos en mismo periodo,
    pero solo el subset de manual_trades que también tienen paper_trade
    asociado por detected_trade_id (A/B real, misma muestra).
    """
    conn = database.get_connection()
    c = conn.cursor()
    cutoff = (datetime.utcnow() - timedelta(days=window_days)).isoformat()
    # Paper closed en ventana
    c.execute("""SELECT id, detected_trade_id, status, realized_pnl_net
                 FROM paper_trades
                 WHERE entry_at >= ?
                   AND status IN ('tp_hit','sl_hit','resolved_win','resolved_loss','disputed','timeout')""",
              (cutoff,))
    paper = c.fetchall()
    paper_dt_ids = {r[1] for r in paper}

    # Manual resueltos en mismo periodo + intersección por detected_trade_id
    # manual_trades no tiene FK directa a detected_trades, pero tiene
    # `copied_from` (wallet) — comparamos todos los manual_trades resueltos
    # en la ventana como referencia.
    c.execute("""SELECT id, status, pnl, entry_price
                 FROM manual_trades
                 WHERE logged_at >= ? AND status IN ('WON','LOST')""",
              (cutoff,))
    manual = c.fetchall()
    conn.close()

    paper_wins = sum(1 for r in paper if r[2] in ("tp_hit","resolved_win"))
    paper_pnl  = sum((r[3] or 0) for r in paper)
    manual_wins = sum(1 for r in manual if r[1]=="WON")
    manual_pnl  = sum((r[2] or 0) for r in manual)

    p_wr = paper_wins / len(paper) * 100 if paper else 0
    m_wr = manual_wins / len(manual) * 100 if manual else 0
    p_avg = paper_pnl / len(paper) if paper else 0
    m_avg = manual_pnl / len(manual) if manual else 0

    return {
        "window_days": window_days,
        "paper":  {"trades": len(paper),  "wr": round(p_wr,1), "pnl_per_trade": round(p_avg,2), "pnl_total": round(paper_pnl,2)},
        "manual": {"trades": len(manual), "wr": round(m_wr,1), "pnl_per_trade": round(m_avg,2), "pnl_total": round(manual_pnl,2)},
        "paper_dt_overlap": len(paper_dt_ids),
    }


# ── 4. Métricas para snapshot diario ─────────────────────────────────

def get_paper_metrics_for_snapshot() -> dict:
    s = get_paper_stats()["stats"]
    total_closed = s["closed"]
    return {
        "n_trades":     s["total"],
        "n_closed":     total_closed,
        "wr":           s["win_rate"] / 100.0 if s["win_rate"] else 0,
        "pnl_net":      s["pnl_net"],
        "tp_hit_pct":   round(s["tp_hit"] / total_closed * 100, 1) if total_closed else 0,
        "sl_hit_pct":   round(s["sl_hit"] / total_closed * 100, 1) if total_closed else 0,
        "timeout_pct":  round(s["timeout"] / total_closed * 100, 1) if total_closed else 0,
        "resolved_pct": round((s["resolved_win"]+s["resolved_loss"]+s["disputed"]) / total_closed * 100, 1) if total_closed else 0,
    }


# ── Test standalone ──────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    print("=== get_paper_stats() ===")
    print(json.dumps(get_paper_stats(), indent=2, default=str))
    print("\n=== get_comparison_with_manual(14) ===")
    print(json.dumps(get_comparison_with_manual(14), indent=2, default=str))
