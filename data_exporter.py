"""
data_exporter.py — Exporta datos reales de la BD a web/data.json
=================================================================
La web lee este archivo cada 30s para mostrar el dashboard en tiempo real.
Se actualiza cada 60 segundos en un thread daemon (ver main.py).

Schema real de la BD:
  user_feedback  → trade_id, action, niche, confidence, outcome, wallet, timestamp
  detected_trades→ id, wallet_address, market_question, side, entry_price, market_id
  bot_trades     → detected_trade_id, pnl_usdc, outcome, executed_at
  wallets        → address, username, win_rate, profit_factor, closed_total, insider_score
  trade_evaluations → confidence_score, verdict, summary

═══════════════════════════════════════════════════════════════════════
FUENTES DE VERDAD POR MÉTRICA (Fase 1.2 — REGLAS INMUTABLES)
═══════════════════════════════════════════════════════════════════════
Tres universos coexisten y NO son aditivos:

1. `my_wallet.*`          → REALIDAD on-chain (oficial Polymarket)
                            FUENTE DE VERDAD para PnL real, WR histórico,
                            posiciones abiertas/cerradas reales.

2. `stats.bot_pnl_30d`    → AGREGADO SINTÉTICO de actividad del bot+manual,
   `stats.won/lost`         filtrado a 30 días. PnL ficticio:
   `stats.win_rate`         - bot_trades.pnl_usdc (real para bot)
                            - manual_trades convertido a $10/trade sintético
                            NO comparable con `my_wallet.total_pnl`.

3. `manual.pnl_synthetic_10usdc` → SUBCONJUNTO sintético del usuario.
                                    Asume tamaño fijo $10. NO refleja la
                                    inversión real (que sí está en my_wallet).

Las posiciones que `manual_trades` registra están TAMBIÉN reflejadas en
my_wallet (son la misma transacción on-chain). Sumar manual + my_wallet
es DOBLE-CONTEO.
═══════════════════════════════════════════════════════════════════════
"""

import json
import os
from datetime import datetime, timedelta

import database
import my_wallet_tracker
import my_behavior_analyzer
import paper_executor
import auto_tuner
import config

# ── Path del JSON de salida ────────────────────────────────────────────────────
_BASE = os.path.dirname(os.path.abspath(__file__))
DATA_JSON_PATH = os.path.join(_BASE, "web", "data.json")

# ── Traders de confianza (hardcoded para el dashboard) ────────────────────────
TRUSTED_DISPLAY = {
    "0xee613b3fc183ee44f9da9c05f53e2da107e3debf": {
        "name": "sovereign2013", "emoji": "👑",
        "real_wr": None, "real_pnl": 1045, "niche": "sports",
    },
    "0x2d4bf8f846bf68f43b9157bf30810d334ac6ca7a": {
        "name": "Gohst", "emoji": "👻",
        "real_wr": None, "real_pnl": 12487, "niche": "politics",
    },
    "0xbaa2bcb543dde9bf6ded15b26b7dca6fe0aad6a9": {
        "name": "denizz", "emoji": "🧠",
        "real_wr": None, "real_pnl": None, "niche": "geopolitics",
    },
}

_NICHE_LABEL = {
    "sports": "Deportes", "esports": "Esports",
    "crypto": "Crypto", "politics": "Política",
    "geopolitics": "Geopolítica", "other": "Economía",
}

_ACTION_LABEL = {
    "copied": "Registrado", "ignored": "Ignorado",
    "copied_won": "Ganado", "copied_lost": "Perdido",
    "ignored_would_have_won": "Habría ganado",
    "ignored_good_decision": "Bien ignorado",
}


def _build_scale_phase_block():
    """data.scale_phase — estado de la fase de escalado (Fase A).
    Lee de config (que a su vez lee .env) para hot-reload sin restart."""
    from datetime import datetime as _dt
    if not config.SCALE_PHASE or config.SCALE_PHASE == "OFF":
        return {"active": False, "phase": "OFF"}

    conn = database.get_connection()
    c = conn.cursor()
    started = config.SCALE_PHASE_STARTED_AT or ""
    # Trades cerrados con tamaño actual desde el inicio de la fase
    c.execute("""SELECT COUNT(*), COALESCE(SUM(pnl_usdc),0),
                        SUM(CASE WHEN pnl_usdc>0 THEN 1 ELSE 0 END)
                 FROM bot_trades
                 WHERE paper_trade=0 AND status='closed'
                   AND pnl_usdc IS NOT NULL
                   AND (? = '' OR executed_at >= ?)""",
              (started, started))
    n, pnl, wins = c.fetchone()
    n = n or 0; pnl = float(pnl or 0); wins = wins or 0
    ppt = (pnl / n) if n else None
    wr = (wins / n * 100) if n else None

    # PnL últimas 24h — solo trades ejecutados (excluye failed/rejected)
    c.execute("""SELECT COALESCE(SUM(pnl_usdc), 0) FROM bot_trades
                 WHERE paper_trade=0 AND pnl_usdc IS NOT NULL
                   AND status NOT IN ('failed','rejected')
                   AND executed_at >= datetime('now','-24 hours')""")
    pnl_24h = float(c.fetchone()[0] or 0.0)

    # Días en fase
    days = None
    if started:
        try:
            days = (_dt.utcnow() - _dt.fromisoformat(started.replace("Z",""))).total_seconds() / 86400
        except Exception:
            pass

    conn.close()

    # Triggers para promover a siguiente fase
    triggers_ok = bool(n and n >= config.SCALE_TARGET_TRADES and ppt is not None and ppt >= 1.20)

    return {
        "active": True,
        "phase": config.SCALE_PHASE,
        "trade_size_usdc": config.MAX_TRADE_SIZE_USDC,
        "started_at": started,
        "days_in_phase": round(days, 2) if days is not None else None,
        "target_trades": config.SCALE_TARGET_TRADES,
        "trades_done": n,
        "ppt": round(ppt, 3) if ppt is not None else None,
        "win_rate": round(wr, 1) if wr is not None else None,
        "pnl_phase": round(pnl, 2),
        "pnl_24h": round(pnl_24h, 2),
        "rollback_threshold_24h": config.SCALE_24H_LOSS_THRESHOLD,
        "rollback_distance": round(pnl_24h - config.SCALE_24H_LOSS_THRESHOLD, 2),
        "promote_ready": triggers_ok,
    }


def _build_real_copy_block():
    """Stats de bot_trades reales del agente DEF_COPY para el dashboard.

    Capital en juego = valor actual de posiciones abiertas desde la funder wallet
    (Polymarket data-api). Fallback: size_usdc de open trades en BD.
    Unrealized = valor actual - coste de entrada.
    """
    try:
        conn = database.get_connection()
        c = conn.cursor()
        REAL_COPY_SINCE = '2026-06-13T17:00:00'
        # Abiertas: todas (sin filtro de fecha — las posiciones previas siguen activas)
        c.execute("""SELECT id, status, outcome, pnl_usdc, size_usdc, entry_price,
                            market_id, executed_at, size_filled, token_id
                     FROM bot_trades WHERE agent_id='DEF_COPY' AND paper_trade=0
                       AND status='open'""")
        open_trades = c.fetchall()
        # Cerradas: solo desde REAL_COPY_SINCE (ventana limpia de PnL)
        c.execute("""SELECT id, status, outcome, pnl_usdc, size_usdc, entry_price,
                            market_id, executed_at, size_filled, token_id
                     FROM bot_trades WHERE agent_id='DEF_COPY' AND paper_trade=0
                       AND status='closed' AND COALESCE(closed_at, executed_at) >= ?""", (REAL_COPY_SINCE,))
        closed_trades = c.fetchall()
        conn.close()
        wins     = sum(1 for r in closed_trades if r[2] == 'won')
        losses   = sum(1 for r in closed_trades if r[2] in ('lost', 'stop_loss'))
        n_closed = len(closed_trades)
        pnl      = round(sum(float(r[3] or 0) for r in closed_trades), 2)
        wr       = round(100 * wins / n_closed, 1) if n_closed else 0

        # capital_open = cost_basis (lo que realmente pusimos en USDC).
        # NO usamos curPrice de la positions API para este valor porque:
        #   1. curPrice es teórico/modelado, no el precio real de venta (bid).
        #   2. En mercados ilíquidos curPrice puede ser 0.62 cuando el bid es 0.01.
        #   3. Mostrarlo como "capital en juego" confunde el valor real arriesgado.
        # El unrealized PnL (teórico) se calcula por separado usando curPrice
        # solo si el precio es creíble (>0.02 — evita que resoluciones a 0
        # contaminen el cálculo con PnL de -100% sobre posiciones ya cerradas).
        # r[8] = size_filled (capital realmente ejecutado on-chain).
        # r[4] = size_usdc (lo que se intentó — incluye órdenes rechazadas/no ejecutadas).
        # Usar size_usdc inflaba artificialmente el capital cuando size_filled < size_usdc.
        cost_basis = round(sum(float(r[8] or 0) for r in open_trades), 2)
        open_value = cost_basis  # siempre igual a lo que pusimos — sin sorpresas

        unrealized = 0.0
        try:
            from dotenv import load_dotenv as _lde
            from pathlib import Path as _P
            import requests as _rq
            _lde(dotenv_path=_P(__file__).resolve().parent / ".env", override=True)
            funder = os.environ.get("POLY_FUNDER_ADDRESS", "").strip()
            if funder and open_trades:
                resp = _rq.get(
                    f"https://data-api.polymarket.com/positions",
                    params={"user": funder, "limit": 500}, timeout=8)
                pm_positions = {}  # asset_id → {curPrice, size}
                for p in (resp.json() or []):
                    asset = str(p.get('asset') or '')
                    if asset:
                        pm_positions[asset] = {
                            'cur': float(p.get('curPrice') or 0),
                            'size': float(p.get('size') or 0),
                        }

                # Unrealized teórico — solo si curPrice es creíble (>0.02)
                for row in open_trades:
                    token_id = str(row[9] or '')
                    ep = float(row[5] or 0)
                    sf = float(row[8] or 0)  # size_filled = coste real ejecutado
                    if token_id and token_id in pm_positions and ep > 0 and sf > 0:
                        pos = pm_positions[token_id]
                        cur = pos['cur']
                        if cur > 0.02:
                            # Shares propias = lo que realmente ejecutamos
                            # NO usar pos['size'] — incluye shares pre-existentes
                            # de otras posiciones o REINFORCEs no trackeados.
                            our_shares = sf / ep
                            cur_val = cur * our_shares
                            unrealized += cur_val - sf
        except Exception as _e:
            print(f"[EXPORT] real_copy positions API err: {_e}")
            unrealized = 0.0  # si falla la API, no inventamos PnL

        open_value = round(open_value, 2)
        unrealized = round(unrealized, 2)

        return {
            'open_count':      len(open_trades),
            'closed_count':    n_closed,
            'wins':            wins,
            'losses':          losses,
            'wr':              wr,
            'pnl_realized':    pnl,
            'unrealized_pnl':  unrealized,  # teórico (curPrice) — puede diferir del vendible
            'equity':          round(pnl + unrealized, 2),
            'capital_open':    open_value,  # cost basis — lo que realmente pusimos
            'cost_basis':      cost_basis,
        }
    except Exception as e:
        print(f"[EXPORT] real_copy block err: {e}")
        return {'open_count': 0, 'closed_count': 0, 'wins': 0, 'losses': 0,
                'wr': 0, 'pnl_realized': 0, 'unrealized_pnl': 0, 'equity': 0,
                'capital_open': 0, 'cost_basis': 0}


def _build_real_trading_block(my_wallet_data=None, agent_id='B'):
    """data.real_trading_{agent_id} — estado de trading real por agente.

    2026-05-28: dividido por `agent_id` para soportar B (TP/SL) y D (hold).
    Por defecto 'B' para retro-compat. agent_id='D' filtra solo trades D.
    """
    import glob, re as _re
    conn = database.get_connection()
    c = conn.cursor()

    # Posiciones reales abiertas — enriquecidas con precio actual y valor
    c.execute("""SELECT id, market_id, side, size_usdc, size_filled, entry_price,
                        order_id, executed_at, status, token_id
                 FROM bot_trades WHERE paper_trade=0 AND status='open'
                   AND COALESCE(agent_id,'B')=?
                 ORDER BY id DESC""", (agent_id,))
    raw_positions = c.fetchall()
    positions = []
    open_value_total = 0.0
    open_unrealized_total = 0.0
    try:
        import executor as _exec
    except Exception:
        _exec = None

    # L46 (2026-06-02) — pre-cargar Polymarket currentValue como fuente primaria
    # de valoración de open positions. El best_bid resulta ultra-conservador
    # en mercados con libros ilíquidos (ej. Heilbronn Galan: best_bid 0.05
    # pero currentValue $4.48). Polymarket usa midpoint o último precio real,
    # mucho más cercano al valor justo de las shares.
    import requests as _rq_l46
    _pm_position_value = {}  # token_id → currentValue Polymarket
    try:
        _r46 = _rq_l46.get(
            f"https://data-api.polymarket.com/positions?user={config.DEPOSIT_WALLET_ADDRESS}&limit=500",
            timeout=8)
        for _p in _r46.json() or []:
            _t = _p.get('asset')
            _cv = float(_p.get('currentValue', 0) or 0)
            if _t and _cv > 0:
                _pm_position_value[_t] = _cv
    except Exception as _e_l46:
        print(f"[EXPORT] L46 PM pre-load err: {_e_l46}")

    def _sell_side_price(_tok):
        """L46: prioridad — (1) Polymarket currentValue / shares, (2) best_bid CLOB,
        (3) midpoint CLOB."""
        if not _tok:
            return None
        if _exec is None:
            return None
        # CLOB best_bid (rápido, fallback robusto)
        try:
            client = _exec.get_client()
            book = client.get_order_book(token_id=_tok)
            bids = book.bids if hasattr(book, 'bids') else book.get('bids', [])
            if bids:
                def _px(b):
                    return float(getattr(b, 'price', None) or b.get('price'))
                return max(_px(b) for b in bids)
        except Exception:
            pass
        try:
            return _exec.get_current_price(_tok)
        except Exception:
            return None

    # On-chain balance check para detectar fantasmas (open + 0 shares)
    try:
        from web3 import Web3 as _W3p
        _w3p = None
        for _u in ["https://polygon-bor-rpc.publicnode.com",
                   "https://polygon-rpc.com", "https://rpc.ankr.com/polygon"]:
            try:
                _w3p = _W3p(_W3p.HTTPProvider(_u, request_kwargs={"timeout": 5}))
                _w3p.eth.block_number; break
            except Exception:
                _w3p = None
        _CTFp = _W3p.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045") if _w3p else None
        _dwp  = _W3p.to_checksum_address(config.DEPOSIT_WALLET_ADDRESS or "0x0") if _w3p else None
        _pxp  = _W3p.to_checksum_address(config.POLY_WALLET_ADDRESS or "0x0") if _w3p else None
        _abip = [{"name":"balanceOf","type":"function","inputs":[
                  {"name":"a","type":"address"},{"name":"i","type":"uint256"}],
                 "outputs":[{"name":"","type":"uint256"}],"stateMutability":"view"}]
        _ctfp = _w3p.eth.contract(address=_CTFp, abi=_abip) if _w3p else None
    except Exception:
        _ctfp = None
        _dwp = _pxp = None

    def _ctf_balance(tok):
        if _ctfp is None or not tok:
            return None
        try:
            return (_ctfp.functions.balanceOf(_dwp, int(tok)).call()
                    + _ctfp.functions.balanceOf(_pxp, int(tok)).call()) / 1e6
        except Exception:
            return None

    # Helpers para clasificar trades sin shares on-chain.
    # 3 estados (mutuamente excluyentes):
    #   pending_buy        — orden LIVE esperando match
    #   settlement_pending — orden MATCHED reciente, shares aún no propagan
    #   phantom            — sin shares + sin orden viva + grace expirada
    def _order_state(order_id):
        if not order_id or _exec is None:
            return None
        try:
            o = _exec.get_client().get_order(order_id)
            if not isinstance(o, dict):
                return None
            return {"status": (o.get('status') or '').upper(),
                    "matched": float(o.get('size_matched') or 0),
                    "original": float(o.get('original_size') or 0)}
        except Exception:
            return None

    from datetime import datetime as _dt2
    GRACE_MIN = 30

    phantom_count = 0
    pending_buy_count = 0
    settlement_pending_count = 0
    for r in raw_positions:
        cost = float(r[4] or 0)       # size_filled (lo que costó nominal)
        entry = float(r[5] or 0)
        tok = r[9]
        order_id = r[6]
        exec_at = r[7]
        shares = (cost / entry) if entry > 0 else 0
        cur_price = _sell_side_price(tok)
        onchain_shares = _ctf_balance(tok)
        # Fix L20: dust-aware. <5% de shares esperadas → considerar vendido.
        expected_shares = shares if shares > 0 else 0
        dust_threshold = max(0.5, 0.05 * expected_shares) if expected_shares > 0 else 0.5
        zero_shares = (onchain_shares is not None and onchain_shares < dust_threshold)

        is_pending_buy = False
        is_settlement_pending = False
        is_phantom = False
        if zero_shares:
            state = _order_state(order_id)
            if state and state["status"] == "LIVE":
                is_pending_buy = True
            else:
                try:
                    age_min = (_dt2.utcnow() - _dt2.fromisoformat(exec_at)).total_seconds() / 60
                except Exception:
                    age_min = 9999
                if age_min < GRACE_MIN:
                    is_settlement_pending = True
                else:
                    is_phantom = True
        # L46: si Polymarket data-api reporta currentValue > 0 para este token,
        # úsalo como valor (es más fiable que best_bid en mercados ilíquidos).
        # Si no hay valor PM, fallback a shares × best_bid.
        pm_cv = _pm_position_value.get(tok)
        if pm_cv is not None and pm_cv > 0:
            cur_value = round(pm_cv, 2)
        else:
            cur_value = round(shares * cur_price, 2) if cur_price is not None else None
        unrealized = round(cur_value - cost, 2) if cur_value is not None else None
        if cur_value is not None:
            open_value_total += cur_value
        if unrealized is not None:
            open_unrealized_total += unrealized
        if is_phantom:
            phantom_count += 1
        if is_pending_buy:
            pending_buy_count += 1
        if is_settlement_pending:
            settlement_pending_count += 1
        # L34 — HOLD-resolución: posición con shares on-chain y current_price
        # ≤ 0.05 (zona de resolución). `executor.fast_price_check_b` ya
        # dispara SL conceptualmente pero ejecuta HOLD para no vender en
        # libro vacío. Exponemos el flag para que la dashboard lo muestre.
        is_hold_resolution = (
            not is_phantom and not is_pending_buy
            and onchain_shares is not None and onchain_shares > 0
            and cur_price is not None and cur_price <= 0.05
        )
        positions.append({
            "id": r[0], "market_id": r[1], "side": r[2],
            "size_usdc": r[3], "size_filled": r[4], "entry_price": r[5],
            "order_id": r[6], "executed_at": r[7], "status": r[8],
            "token_id": tok,
            "shares": round(shares, 4),
            "onchain_shares": round(onchain_shares, 4) if onchain_shares is not None else None,
            "phantom": is_phantom,
            "pending_buy": is_pending_buy,
            "settlement_pending": is_settlement_pending,
            "hold_resolution": is_hold_resolution,
            "current_price": round(cur_price, 4) if cur_price is not None else None,
            "current_value_usdc": cur_value,
            "unrealized_pnl_usdc": unrealized,
        })
    open_value_total = round(open_value_total, 2)
    open_unrealized_total = round(open_unrealized_total, 2)

    # PnL realizado + desglose won/lost para WR.
    # 'closed' = posición que llegó a resolución on-chain (won o lost).
    # 'failed' / 'rejected' / 'close_failed' NO cuentan en WR (no hubo posición real).
    c.execute("""SELECT
                   COALESCE(SUM(pnl_usdc), 0),
                   COUNT(*),
                   COALESCE(SUM(CASE WHEN outcome='won'  THEN 1 ELSE 0 END), 0),
                   COALESCE(SUM(CASE WHEN outcome='lost' THEN 1 ELSE 0 END), 0)
                 FROM bot_trades
                 WHERE paper_trade=0
                   AND status='closed'
                   AND outcome IN ('won','lost')
                   AND COALESCE(agent_id,'B')=?""", (agent_id,))
    row = c.fetchone()
    pnl_realized = round(row[0] or 0, 2)
    closed_n = row[1] or 0
    wins_n   = row[2] or 0
    losses_n = row[3] or 0
    wr_pct   = round(wins_n / closed_n * 100, 1) if closed_n else None

    # Últimos 10 trades reales del agente
    c.execute("""SELECT id, market_id, side, size_usdc, entry_price, status,
                        pnl_usdc, executed_at
                 FROM bot_trades WHERE paper_trade=0
                   AND COALESCE(agent_id,'B')=?
                 ORDER BY id DESC LIMIT 10""", (agent_id,))
    recent = [{"id": r[0], "market_id": r[1], "side": r[2], "size_usdc": r[3],
               "entry_price": r[4], "status": r[5], "pnl_usdc": r[6],
               "executed_at": r[7]} for r in c.fetchall()]
    conn.close()

    # Señales filtradas por el router en últimas 24h (parse del log, sin schema)
    filtered = {"niche_excluded": 0, "risk_blocked": 0, "other": 0, "total": 0}
    try:
        log_file = sorted(glob.glob(os.path.join(_BASE, "logs", "bot-*.log")))[-1]
        cutoff = (datetime.now() - timedelta(hours=24))
        with open(log_file, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if "[ROUTER]" in line and "niche" in line and "REAL_EXCLUDED" in line:
                    filtered["niche_excluded"] += 1; filtered["total"] += 1
                elif "[REAL_ROUTER]" in line and "bloqueada por risk_manager" in line:
                    filtered["risk_blocked"] += 1; filtered["total"] += 1
    except Exception:
        pass

    # Balance USDC: primero intentamos CLOB get_balance_allowance,
    # con fallback a query on-chain directa si falla (Fase 4.4 fix).
    # On-chain query es más confiable: el CLOB SDK timeoutea esporádicamente.
    balance = None
    try:
        import executor
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        cl = executor.get_client()
        r = cl.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        raw = r.get("balance") if isinstance(r, dict) else None
        if raw is not None:
            balance = round(float(raw) / 1_000_000, 2)
    except Exception as e:
        print(f"[EXPORT] balance_usdc CLOB error: {e} — fallback on-chain")

    if balance is None:
        # Fallback: retry CLOB con un segundo intento antes del caché.
        # El on-chain directo no sirve: el USDC está dentro del CTF de Polymarket,
        # no en la wallet directamente — siempre daría $0 aunque haya fondos.
        try:
            import executor as _exec_fb
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            _cl_fb = _exec_fb.get_client()
            _r_fb = _cl_fb.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            _raw_fb = _r_fb.get("balance") if isinstance(_r_fb, dict) else None
            if _raw_fb is not None:
                balance = round(float(_raw_fb) / 1_000_000, 2)
                print(f"[EXPORT] balance_usdc CLOB retry: ${balance:.2f}")
        except Exception as e:
            print(f"[EXPORT] balance_usdc CLOB retry error: {e}")

    if balance is None:
        # Last-known-good cache: leer data.json anterior y usar su balance
        try:
            import json as _json
            prev = _json.load(open(DATA_JSON_PATH))
            cached = prev.get("real_trading", {}).get("balance_usdc")
            if cached is not None:
                balance = cached
                print(f"[EXPORT] balance_usdc usando last-known-good cache: ${balance}")
        except Exception:
            pass

    # ── Equity total: pUSD libre + capital en posiciones abiertas (a coste)
    # + payoff pendiente de redeem (shares de ganadoras todavía on-chain).
    # capital_open: suma size_filled de open trades (cost USDC nominal).
    # pending_redeem: para closed-won con shares on-chain > 0, valor = shares × 1.
    capital_open = 0.0
    pending_redeem = 0.0
    try:
        from web3 import Web3 as _W3
        # Probar varios RPCs hasta que uno responda
        _w3 = None
        for _rpc in ["https://polygon-bor-rpc.publicnode.com",
                     "https://polygon-rpc.com",
                     "https://rpc.ankr.com/polygon"]:
            try:
                _w3 = _W3(_W3.HTTPProvider(_rpc, request_kwargs={"timeout": 5}))
                _w3.eth.block_number  # ping
                break
            except Exception:
                _w3 = None
        if _w3 is None:
            raise RuntimeError("todos los RPCs fallaron")
        _CTF = _W3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
        _dw = _W3.to_checksum_address(config.DEPOSIT_WALLET_ADDRESS or "")
        _abi = [{"name":"balanceOf","type":"function","inputs":[
                  {"name":"o","type":"address"},{"name":"i","type":"uint256"}],
                 "outputs":[{"name":"","type":"uint256"}],"stateMutability":"view"}]
        _ctf = _w3.eth.contract(address=_CTF, abi=_abi)

        # capital_open: directo desde DB
        _conn = database.get_connection()
        _r = _conn.execute("""SELECT COALESCE(SUM(size_filled),0)
                              FROM bot_trades WHERE paper_trade=0 AND status='open'""").fetchone()
        capital_open = float(_r[0] or 0)

        # pending_redeem: shares × 1 para won con balance on-chain > 0.
        # DISTINCT token_id evita doble conteo si dos trades comparten el mismo
        # outcome on-chain (mismo positionId; ej. dos BUYs sobre Tundra en
        # Tundra vs BetBoom). El balance on-chain es uno solo, no la suma.
        # También verificamos balance tanto en deposit wallet como en proxy
        # legacy (post-transfer manual para claim vía UI).
        #
        # L33 (2026-05-31) — fix doble conteo crítico:
        # si el mismo token también tiene una posición OPEN (ej. bt#168 BUY
        # a 0.06 sobre el outcome perdedor del mismo mercado donde bt#163
        # ganó), el balance on-chain pertenece a la open, NO a redeem.
        # Restamos las shares "reservadas" para opens antes de valuar como
        # redeem.
        # L41 (2026-06-02) — Verificación VIA POLYMARKET API. Cap shares×$1
        # del L33v2 fallaba cuando las shares on-chain pertenecían a outcomes
        # que LOSING (Polymarket data-api currentValue=$0). El bot histórico
        # marcó esos trades como 'won' porque hicieron TP_HIT antes de la
        # resolución, pero el TOKEN específico ahora vale $0 porque el OTRO
        # outcome ganó. Solo cuentan como pending_redeem los tokens cuyo
        # market resolvió y NUESTRO outcome ganó (currentValue > 0).
        # L33 v2 (2026-05-31) — cap por shares ESPERADAS de los wins.
        won_expected_shares = {}
        for (tok, sf, ep) in _conn.execute("""
                SELECT token_id, COALESCE(size_filled,0), entry_price
                  FROM bot_trades
                 WHERE paper_trade=0 AND status='closed'
                   AND outcome='won' AND token_id IS NOT NULL
                   AND entry_price>0""").fetchall():
            if not tok or not ep: continue
            won_expected_shares[tok] = (
                won_expected_shares.get(tok, 0.0) + float(sf or 0) / float(ep)
            )
        # Shares y coste esperados en posiciones OPEN del MISMO token.
        open_shares_by_tok = {}
        open_cost_by_tok = {}  # token_id → sum(size_filled) para abiertas
        for (tok, sf, ep) in _conn.execute("""
                SELECT token_id, COALESCE(size_filled,0), entry_price
                  FROM bot_trades
                 WHERE paper_trade=0 AND status='open'
                   AND token_id IS NOT NULL AND entry_price>0""").fetchall():
            if not tok or not ep:
                continue
            open_shares_by_tok[tok] = (
                open_shares_by_tok.get(tok, 0.0) + float(sf or 0) / float(ep)
            )
            open_cost_by_tok[tok] = open_cost_by_tok.get(tok, 0.0) + float(sf or 0)
        _conn.close()
        _proxy = _W3.to_checksum_address(config.POLY_WALLET_ADDRESS or "0x0")
        # L41 — pre-cargar Polymarket positions del DW y proxy para chequear
        # currentValue real (excluye shares de outcomes perdedores).
        import requests as _rq
        _pm_curval = {}   # token_id → face value ($1/share)
        _pm_sz_all = {}   # token_id → shares (para lag de resolución)
        for _addr in (_dw, _proxy):
            try:
                _r = _rq.get(f"https://data-api.polymarket.com/positions?user={_addr}&limit=500", timeout=8)
                for _p in _r.json() or []:
                    _t = _p.get('asset')
                    _cv = float(_p.get('currentValue', 0) or 0)
                    _sz = float(_p.get('size', 0) or 0)
                    _face = _sz if _sz > 0.01 else _cv
                    if _t and _sz > 0.01:
                        _pm_sz_all[_t] = _pm_sz_all.get(_t, 0) + _sz
                    if _t and _cv > 0.01:
                        _pm_curval[_t] = _pm_curval.get(_t, 0) + _face
            except Exception:
                pass

        for tok, face in _pm_curval.items():
            if tok in open_shares_by_tok:
                continue  # ya contado en cap_open
            pending_redeem += face

        # Lag fix: si el mercado acaba de resolver, PM API puede mostrar cv=0
        # temporalmente aunque las shares siguen en DW. Para tokens con trade
        # closed/won en DB y shares visibles (sz>0), añadir face value aunque cv=0.
        for tok in won_expected_shares:
            if tok in open_shares_by_tok:
                continue  # open trade, ya en cap_open
            if tok in _pm_curval:
                continue  # ya contado arriba con cv>0
            sz = _pm_sz_all.get(tok, 0)
            if sz > 0.01:
                pending_redeem += sz  # $1/share face value
    except Exception as e:
        print(f"[EXPORT] equity calc error: {e}")

    # ── Saldos extra (Fase 4.5): que se vea TODO el dinero, no solo pUSD-DW
    # Suma USDC nativo en DW + pUSD/USDC en proxy. Útil para detectar dinero
    # "olvidado" entre bridges/redeems/withdraws.
    extras = {"usdc_dw": 0.0, "pusd_proxy": 0.0, "usdc_proxy": 0.0}
    try:
        from web3 import Web3 as _W3x
        _RPCSx = [
            "https://polygon-bor-rpc.publicnode.com",
            "https://polygon-rpc.com",
            "https://rpc.ankr.com/polygon",
            f"https://polygon-mainnet.g.alchemy.com/v2/{config.ALCHEMY_API_KEY}" if config.ALCHEMY_API_KEY else None,
        ]
        _RPCSx = [r for r in _RPCSx if r]
        _w3x = None
        for _u in _RPCSx:
            try:
                _w3x = _W3x(_W3x.HTTPProvider(_u, request_kwargs={"timeout": 5}))
                _w3x.eth.block_number
                break
            except Exception:
                _w3x = None
        if _w3x is not None:
            _ERC20 = [{"name":"balanceOf","type":"function","inputs":[
                       {"name":"o","type":"address"}],
                      "outputs":[{"name":"","type":"uint256"}],"stateMutability":"view"}]
            # USDC.PoS (bridged legacy 0x2791) + USDC native (Circle 0x3c49) —
            # los redeems de Polymarket llegan como USDC native, no PoS.
            _USDC_POS    = _W3x.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
            _USDC_NATIVE = _W3x.to_checksum_address("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359")
            _PUSD = _W3x.to_checksum_address("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")
            _dwx = _W3x.to_checksum_address(config.DEPOSIT_WALLET_ADDRESS or "0x0")
            _pxx = _W3x.to_checksum_address(config.POLY_WALLET_ADDRESS or "0x0")
            def _bal(token, owner):
                try:
                    return _w3x.eth.contract(address=token, abi=_ERC20)\
                              .functions.balanceOf(owner).call() / 1_000_000
                except Exception:
                    return 0.0
            extras["usdc_dw"]    = round(_bal(_USDC_POS, _dwx) + _bal(_USDC_NATIVE, _dwx), 2)
            extras["pusd_proxy"] = round(_bal(_PUSD,     _pxx), 2)
            extras["usdc_proxy"] = round(_bal(_USDC_POS, _pxx) + _bal(_USDC_NATIVE, _pxx), 2)
    except Exception as e:
        print(f"[EXPORT] extras balance error: {e}")

    # L81: pusd_proxy venía de un RPC poco fiable que devolvía $221 incorrecto.
    # Solo contamos lo verificado on-chain (usdc_dw) + lo que devuelve CLOB API.
    extras_total = round(extras["usdc_dw"] + extras["usdc_proxy"], 2)
    # NOTA F2 (2026-06-18): equity_total NO se calcula aquí.
    # La única fuente de verdad de equity es la sección canónica DEF_COPY
    # (más abajo en export_data()) que usa PM API para cap_def y red_pm.
    # Este bloque solo provee: balance, extras, capital_open (para cap abierto
    # preliminar), pending_redeem (on-chain fallback). No calcular equity aquí
    # evita tener dos fórmulas divergentes.
    equity_total = None  # no usado — valor real lo escribe la sección canonical

    # L46c (2026-06-02) — snapshot DESACTIVADO desde aquí.
    # Movido al main export_data() después del L46b combine B+D, para que
    # el snapshot refleje la equity total (incluyendo open_value de D).
    # Antes el snapshot usaba solo equity de B → series temporal infravalorada.
    if False and agent_id == "B":
        try:
            _conn_s = database.get_connection()
            # Sanity check: compara contra el último snapshot conocido.
            # Si pending_redeem cayó >$10 a casi 0, es probablemente RPC fail.
            prev = _conn_s.execute("""SELECT pending_redeem, open_value
                FROM equity_snapshots ORDER BY ts DESC LIMIT 1""").fetchone()
            rpc_suspect = False
            if prev:
                prev_redeem, prev_open = prev
                # Caso A (original): TODO se cae a 0 (pending_redeem + open_value)
                if (float(prev_redeem or 0) > 10
                    and pending_redeem < 1
                    and open_market_value < 1):
                    rpc_suspect = True
                # Caso B (L39b): solo pending_redeem se cae mientras open
                # sigue. Pasa cuando el CTF RPC falla pero CLOB API responde.
                # Si pending_redeem cayó >$20 vs previo y ahora es <$1, suspect.
                elif (float(prev_redeem or 0) > 20
                      and pending_redeem < 1):
                    rpc_suspect = True
            if rpc_suspect:
                print(f"[EXPORT] snapshot equity SKIPPED — RPC fail probable "
                      f"(prev redeem ${prev_redeem:.2f} → ahora $0)")
            else:
                _ts = datetime.utcnow().replace(microsecond=0).isoformat()
                _conn_s.execute("""INSERT OR REPLACE INTO equity_snapshots
                    (ts, equity_total, pusd_libre, open_value,
                     pending_redeem, extras_total, pnl_unrealized)
                    VALUES (?,?,?,?,?,?,?)""",
                    (_ts, equity_total, round(balance or 0, 2),
                     round(open_market_value, 2), round(pending_redeem, 2),
                     round(extras_total, 2), round(open_unrealized_total, 2)))
                # Retención: borrar > 30 días
                _conn_s.execute("""DELETE FROM equity_snapshots
                    WHERE ts < datetime('now','-30 days')""")
                _conn_s.commit()
            _conn_s.close()
        except Exception as _e:
            print(f"[EXPORT] snapshot equity err: {_e}")

    estado = "BLOQUEADO"
    if not config.PAPER_TRADING and config.REAL_TRADING_ENABLED:
        estado = "ACTIVO"

    return {
        "estado": estado,
        "config": {
            "agent": config.REAL_TRADING_AGENT,
            "trade_size_usdc": config.MAX_TRADE_SIZE_USDC,
            "max_slippage": config.MAX_SLIPPAGE,
            "excluded_niches": config.REAL_EXCLUDED_NICHES,
            "max_open_positions": config.MAX_OPEN_POSITIONS,
        },
        "balance_usdc": balance,
        "capital_open_usdc": round(capital_open, 2),        # cost basis (lo invertido)
        "open_value_usdc": open_value_total,                # valor de mercado actual
        "open_unrealized_pnl_usdc": open_unrealized_total,  # PnL no realizado
        "phantom_open_count": phantom_count,                 # alerta defensa
        "pending_buy_count": pending_buy_count,              # BUY GTC live
        "settlement_pending_count": settlement_pending_count, # match reciente
        "pending_redeem_usdc": round(pending_redeem, 2),
        "extras_usdc": extras,
        "extras_total_usdc": extras_total,
        "equity_total_usdc": equity_total,
        "open_positions": positions,
        "open_count": len(positions),
        "pnl_realized": pnl_realized,
        "closed_count": closed_n,
        "wins": wins_n,
        "losses": losses_n,
        "wr": wr_pct,
        "recent_trades": recent,
        "filtered_24h": filtered,
    }


def _build_experiment_block():
    """Construye data.experiment con stats por agente + pnl_curve."""
    conn = database.get_connection()
    c = conn.cursor()

    # Agentes activos (excluye A_legacy oculto)
    c.execute("""SELECT id, name, description, tp_multiplier, sl_multiplier,
                        timeout_seconds, created_at, config_frozen
                 FROM agents WHERE active=1 AND id != 'A_legacy'
                 ORDER BY id""")
    agent_rows = c.fetchall()

    # Started_at del experimento = primer paper_trade no-legacy
    c.execute("""SELECT MIN(entry_at) FROM paper_trades
                 WHERE agent_id NOT IN ('A_legacy')""")
    started_at = (c.fetchone() or [None])[0]

    agents_out = []
    pnl_curve = {}
    # L63 (2026-06-03) — Reset Experimental Paper desde ahora.
    # Histórico se conserva en DB. Card y curve solo cuentan post-cutoff.
    # Tamaño nominal post-cutoff: $5/trade (matchea real Trading 1).
    EXP_CUTOFF = "2026-06-03T17:00:00"
    for r in agent_rows:
        ag_id, name, desc, tp, sl, to_s, created_at, frozen = r
        # Config descriptor
        if tp is not None and sl is not None:
            cfg_str = f"TP {tp}x · SL {sl}x · timeout {to_s//86400}d"
        else:
            cfg_str = f"Sin TP/SL · timeout {to_s//86400}d"

        # Stats — para agente B (Experimental Paper) aplicar cutoff selectivo:
        #   - "open" cuenta TODAS las abiertas (incluido pre-cutoff)
        #   - cerradas solo si exit_at >= cutoff (cerraron tras reset)
        # L63b — extrapolar pnl de trades pre-cutoff $100 → $5 (×0.05) para
        # mantener escala consistente con los nuevos $5/trade.
        if ag_id == 'B':
            c.execute("""SELECT status, COUNT(*),
              COALESCE(SUM(
                CASE WHEN entry_at < ? THEN realized_pnl_net * 0.05
                     ELSE realized_pnl_net END), 0)
              FROM paper_trades
              WHERE agent_id=?
                AND (status='open' OR (exit_at IS NOT NULL AND exit_at >= ?))
              GROUP BY status""", (EXP_CUTOFF, ag_id, EXP_CUTOFF))
        else:
            c.execute("""SELECT status, COUNT(*), COALESCE(SUM(realized_pnl_net), 0)
                         FROM paper_trades WHERE agent_id=? GROUP BY status""", (ag_id,))
        by_status = {row[0]: (row[1], row[2]) for row in c.fetchall()}

        n_open  = by_status.get("open", (0, 0))[0]
        tp_hit  = by_status.get("tp_hit", (0, 0))[0]
        sl_hit  = by_status.get("sl_hit", (0, 0))[0]
        rwin    = by_status.get("resolved_win", (0, 0))[0]
        rloss   = by_status.get("resolved_loss", (0, 0))[0]
        disp    = by_status.get("disputed", (0, 0))[0]
        timo    = by_status.get("timeout", (0, 0))[0]
        n_closed = tp_hit + sl_hit + rwin + rloss + disp + timo
        wins = tp_hit + rwin
        wr = round(wins / n_closed * 100, 1) if n_closed else 0.0
        pnl_total = round(sum(v[1] for k, v in by_status.items() if k != "open"), 2)
        pnl_per   = round(pnl_total / n_closed, 2) if n_closed else 0.0

        agents_out.append({
            "id": ag_id, "name": name, "description": desc,
            "config": cfg_str, "frozen": bool(frozen),
            "tp_multiplier": tp, "sl_multiplier": sl, "timeout_days": to_s // 86400,
            "open": n_open, "closed": n_closed,
            "tp_hit": tp_hit, "sl_hit": sl_hit,
            "resolved_win": rwin, "resolved_loss": rloss, "disputed": disp,
            "timeout": timo, "wins": wins,
            "wr": wr, "pnl_total_net": pnl_total, "pnl_per_trade_net": pnl_per,
        })

        # Curva acumulada: cada cierre añade un punto, ordenado cronológicamente
        # L63: para Experimental Paper (B) cuentan cierres con exit_at >= cutoff
        # (incluye pre-cutoff abiertos que cerraron después del reset).
        # L63b: pnl extrapolado a escala $5 (×0.05) si abrió pre-cutoff $100.
        if ag_id == 'B':
            c.execute("""SELECT exit_at,
              CASE WHEN entry_at < ? THEN realized_pnl_net * 0.05
                   ELSE realized_pnl_net END as scaled_pnl
              FROM paper_trades
              WHERE agent_id=? AND status != 'open' AND exit_at IS NOT NULL
                AND exit_at >= ?
              ORDER BY exit_at""", (EXP_CUTOFF, ag_id, EXP_CUTOFF))
        else:
            c.execute("""SELECT exit_at, realized_pnl_net FROM paper_trades
                         WHERE agent_id=? AND status != 'open' AND exit_at IS NOT NULL
                         ORDER BY exit_at""", (ag_id,))
        closes = c.fetchall()
        cum = 0.0
        pts = []
        for exit_at, pnl in closes:
            cum += float(pnl or 0)
            pts.append({"t": exit_at, "cum": round(cum, 2)})
        # Punto final "now" para que la curva no se corte hasta el último evento
        if pts:
            pts.append({"t": datetime.now().isoformat(), "cum": pts[-1]["cum"]})
        pnl_curve[ag_id] = pts

    # ── Series REAL por agente (B, D) — bot_trades cerrados ──────────────
    # 2026-05-28: dividido por agent_id para mostrar curvas separadas.
    # Mantenemos pnl_curve.REAL como alias retro-compat = REAL_B.
    for _agent_key in ('B', 'D'):
        c.execute("""SELECT executed_at, pnl_usdc, error
                     FROM bot_trades
                     WHERE paper_trade=0 AND status='closed'
                       AND COALESCE(agent_id,'B')=?
                     ORDER BY executed_at""", (_agent_key,))
        pts_agent = []
        for exec_at, pnl, err in c.fetchall():
            ts = exec_at
            if err and "resolved_at=" in err:
                try:
                    start = err.index("resolved_at=") + len("resolved_at=")
                    ts_candidate = err[start:start+26].split(" ")[0]
                    datetime.fromisoformat(ts_candidate.replace("Z", ""))
                    ts = ts_candidate
                except Exception:
                    pass
            pts_agent.append({"t": ts, "pnl": float(pnl or 0)})
        pts_agent.sort(key=lambda p: p["t"])
        cum_x = 0.0
        for p in pts_agent:
            cum_x += p["pnl"]
            p["cum"] = round(cum_x, 4)
            del p["pnl"]
        if pts_agent:
            pts_agent.append({"t": datetime.now().isoformat(),
                              "cum": pts_agent[-1]["cum"]})
        pnl_curve[f"REAL_{_agent_key}"] = pts_agent
    # Alias retro-compat
    pnl_curve["REAL"] = pnl_curve["REAL_B"]

    # ── L55 (2026-06-03) — Paper B Mirror @ $5/trade ────────────────────
    # Sustituye al antiguo agente C en cards y chart. Solo cuenta paper
    # trades del agente B cuyo detected_trade_id también ejecutó en real B
    # (mirror perfecto post-L52). Escala el PnL por 0.05 ($5 real / $100 paper)
    # para que las cifras del card coincidan con lo que veríamos en real.
    #
    # L55b: cutoff = momento del deploy L52 (2026-06-02 22:00 UTC). Antes
    # de esa hora, paper B y real B usaban filtros divergentes y los
    # "pares" históricos NO eran mirror reales. Solo contamos desde que
    # el mirror filter realmente está sincronizado.
    SCALE = 5.0 / float(config.PAPER_SIZE_USDC or 100.0)  # 0.05 por defecto
    MIRROR_CUTOFF = "2026-06-02T22:00:00"

    # L65 — escalado dinámico por tamaño del paper trade.
    c.execute("""SELECT pt.exit_at, pt.realized_pnl_net, pt.status, pt.entry_size_usdc
                 FROM paper_trades pt
                 JOIN bot_trades bt ON bt.detected_trade_id=pt.detected_trade_id
                                    AND bt.paper_trade=0 AND bt.agent_id='B'
                 WHERE pt.agent_id='B' AND pt.status != 'open'
                   AND pt.exit_at IS NOT NULL
                   AND pt.entry_at >= ?
                 ORDER BY pt.exit_at""", (MIRROR_CUTOFF,))
    rows_mirror = c.fetchall()
    cum = 0.0
    pts = []
    by_status_m = {"tp_hit": 0, "sl_hit": 0, "resolved_win": 0,
                    "resolved_loss": 0, "disputed": 0, "timeout": 0}
    pnl_total_m = 0.0
    for exit_at, pnl, st, sz in rows_mirror:
        # Si paper se abrió a $5 ya está en escala — no aplicar SCALE.
        # Si se abrió a $100 (legacy) aplicar 5/100=0.05 para extrapolar.
        actual_scale = 1.0 if (sz and abs(float(sz) - 5.0) < 0.1) else SCALE
        scaled = float(pnl or 0) * actual_scale
        cum += scaled
        pnl_total_m += scaled
        pts.append({"t": exit_at, "cum": round(cum, 2)})
        if st in by_status_m:
            by_status_m[st] += 1
    if pts:
        pts.append({"t": datetime.now().isoformat(), "cum": pts[-1]["cum"]})
    pnl_curve["B_MIRROR"] = pts

    # Stats agregadas (mismo formato que agents_out para sustituir card C)
    n_closed_m = sum(by_status_m.values())
    wins_m = by_status_m["tp_hit"] + by_status_m["resolved_win"]
    wr_m = round(wins_m / n_closed_m * 100, 1) if n_closed_m else 0.0
    pnl_per_m = round(pnl_total_m / n_closed_m, 2) if n_closed_m else 0.0
    # Trades B paper open que tienen su par real B (post-L52)
    c.execute("""SELECT COUNT(*) FROM paper_trades pt
                 JOIN bot_trades bt ON bt.detected_trade_id=pt.detected_trade_id
                                    AND bt.paper_trade=0 AND bt.agent_id='B'
                                    AND bt.status='open'
                 WHERE pt.agent_id='B' AND pt.status='open'""")
    n_open_m = c.fetchone()[0] or 0

    mirror_card = {
        "id": "PAPER_MIRROR_1",
        "name": "Paper Mirror 1",
        "description": "Espejo de Real Trading 1 (Lily) · $5/trade",
        "config": "TP 1.7x · SL 0.5x · solo pares con real",
        "frozen": False,
        "tp_multiplier": 1.7, "sl_multiplier": 0.5, "timeout_days": 7,
        "open": n_open_m, "closed": n_closed_m,
        "tp_hit": by_status_m["tp_hit"],
        "sl_hit": by_status_m["sl_hit"],
        "resolved_win": by_status_m["resolved_win"],
        "resolved_loss": by_status_m["resolved_loss"],
        "disputed": by_status_m["disputed"],
        "timeout": by_status_m["timeout"],
        "wins": wins_m,
        "wr": wr_m,
        "pnl_total_net": round(pnl_total_m, 2),
        "pnl_per_trade_net": pnl_per_m,
    }

    # ── L62 (2026-06-03) — Paper Mirror 2 = espejo Real Trading 2 (D) ──
    # Escala $3/trade (igual que real D).
    # L65 (2026-06-04) — cutoff FIJO. Antes era datetime.now() que se evaluaba
    # cada export → query siempre vacía (entry_at >= now nunca se cumple).
    SCALE_D = 3.0 / float(config.PAPER_SIZE_USDC or 100.0)
    MIRROR2_CUTOFF = "2026-06-03T17:00:00"
    # L65 — mismo fix para Mirror 2: detectar si paper ya está a $3 (L61)
    c.execute("""SELECT pt.exit_at, pt.realized_pnl_net, pt.status, pt.entry_size_usdc
                 FROM paper_trades pt
                 JOIN bot_trades bt ON bt.detected_trade_id=pt.detected_trade_id
                                    AND bt.paper_trade=0 AND bt.agent_id='D'
                 WHERE pt.agent_id='D' AND pt.status != 'open'
                   AND pt.exit_at IS NOT NULL
                   AND pt.entry_at >= ?
                 ORDER BY pt.exit_at""", (MIRROR2_CUTOFF,))
    rows_m2 = c.fetchall()
    cum2 = 0.0
    pts2 = []
    by_status_m2 = {"tp_hit": 0, "sl_hit": 0, "resolved_win": 0,
                     "resolved_loss": 0, "disputed": 0, "timeout": 0}
    pnl_total_m2 = 0.0
    for exit_at, pnl, st, sz in rows_m2:
        actual_scale = 1.0 if (sz and abs(float(sz) - 3.0) < 0.1) else SCALE_D
        scaled = float(pnl or 0) * actual_scale
        cum2 += scaled
        pnl_total_m2 += scaled
        pts2.append({"t": exit_at, "cum": round(cum2, 2)})
        if st in by_status_m2:
            by_status_m2[st] += 1
    if pts2:
        pts2.append({"t": datetime.now().isoformat(), "cum": pts2[-1]["cum"]})
    pnl_curve["PAPER_MIRROR_2"] = pts2

    n_closed_m2 = sum(by_status_m2.values())
    wins_m2 = by_status_m2["tp_hit"] + by_status_m2["resolved_win"]
    wr_m2 = round(wins_m2 / n_closed_m2 * 100, 1) if n_closed_m2 else 0.0
    pnl_per_m2 = round(pnl_total_m2 / n_closed_m2, 2) if n_closed_m2 else 0.0
    # L65b — el conteo de open NO debe filtrar por cutoff porque las posiciones
    # abiertas reales que tienen paper counterpart (aunque sea pre-cutoff) son
    # parte del estado vivo de Mirror 2. Solo el closed y curve respetan cutoff.
    c.execute("""SELECT COUNT(*) FROM paper_trades pt
                 JOIN bot_trades bt ON bt.detected_trade_id=pt.detected_trade_id
                                    AND bt.paper_trade=0 AND bt.agent_id='D'
                                    AND bt.status='open'
                 WHERE pt.agent_id='D' AND pt.status='open'""")
    n_open_m2 = c.fetchone()[0] or 0

    mirror2_card = {
        "id": "PAPER_MIRROR_2",
        "name": "Paper Mirror 2",
        "description": "Espejo de Real Trading 2 (swisstony) · $3/trade",
        "config": "Hold-to-resolve · solo pares con real",
        "frozen": False,
        "tp_multiplier": None, "sl_multiplier": None, "timeout_days": 60,
        "open": n_open_m2, "closed": n_closed_m2,
        "tp_hit": by_status_m2["tp_hit"],
        "sl_hit": by_status_m2["sl_hit"],
        "resolved_win": by_status_m2["resolved_win"],
        "resolved_loss": by_status_m2["resolved_loss"],
        "disputed": by_status_m2["disputed"],
        "timeout": by_status_m2["timeout"],
        "wins": wins_m2,
        "wr": wr_m2,
        "pnl_total_net": round(pnl_total_m2, 2),
        "pnl_per_trade_net": pnl_per_m2,
    }

    # ── Rebranding L62 — renames y filtrado ──
    # B → Experimental Paper
    # C → eliminado (research deprecated)
    # D → eliminado (no mirror, redundante con Real Trading 2)
    # Añadir Paper Mirror 1 (sustituye al C) y Paper Mirror 2 (al final)
    # Excluir C, D y los 3 shadows DEF_* (estos últimos van en sección propia)
    agents_out = [a for a in agents_out if a.get("id") not in ("C", "D", "DEF_TP", "DEF_HOLD", "DEF_COPY", "DEF_COPY_05")]
    for a in agents_out:
        if a.get("id") == "B":
            a["name"] = "Experimental Paper"
            a["description"] = "Paper B sin mirror · $5/trade · todas las wallets"
        elif a.get("id") == "DEFINITIVE":
            a["name"] = "Definitive Paper"
            a["description"] = "Copia escalada de Lily · agregado >=$50 · ratio 0.78%"
    # Insertar Paper Mirror 1 tras B (Experimental Paper) y Paper Mirror 2 después
    insert_at = next((i+1 for i, a in enumerate(agents_out) if a.get("id") == "B"), len(agents_out))
    agents_out.insert(insert_at, mirror_card)
    # Paper Mirror 2 al final (no hay D ya)
    agents_out.append(mirror2_card)

    # ── L67 (2026-06-04) — Definitive shadow table ──
    # 3 agentes (DEF_TP, DEF_HOLD, DEF_COPY) operan en paralelo cada señal Lily.
    # Construimos tabla comparativa lado-a-lado para el dashboard.
    # L69 (2026-06-04) — Multi-trader: Lily + Swisstony en paralelo.
    DEF_AGENTS = ('DEF_TP', 'DEF_COPY_05', 'DEF_COPY')
    DEF_TRADERS = {
        '0x01c78f8873c0c86d6b6b92ff627e3802237ee995': 'Lily',
        '0x204f72f35326db932158cba6adff0b9a1da95e14': 'Swisstony',
    }
    c.execute("""SELECT detected_trade_id, agent_id, status, entry_at, entry_price,
                     entry_size_usdc, exit_price, realized_pnl_net, market_question,
                     exit_reason, wallet_copied
                 FROM paper_trades
                 WHERE agent_id IN ('DEF_TP','DEF_COPY_05','DEF_COPY')
                   AND status != 'cancelled'
                 ORDER BY entry_at DESC LIMIT 400""")
    by_dt = {}
    for r in c.fetchall():
        dt_id, ag, st, eat, ep, sz, exp, pnl, mkt, exr, wc = r
        trader = DEF_TRADERS.get((wc or '').lower(), '?')
        if dt_id not in by_dt:
            by_dt[dt_id] = {
                'detected_trade_id': dt_id,
                'entry_at': eat, 'entry_price': ep, 'size': sz,
                'market': (mkt or '')[:60],
                'trader': trader,
                'DEF_TP': None, 'DEF_COPY_05': None, 'DEF_COPY': None,
            }
        by_dt[dt_id][ag] = {
            'status': st, 'exit_price': exp, 'pnl': pnl, 'exit_reason': exr,
        }

    def _stats_for(agent_id, wallet_filter=None):
        # Filtro de escala: solo trades ≤$15 para excluir herencia del experimento $100
        scale_filter = "AND entry_size_usdc <= 25"
        if wallet_filter:
            q = f"""SELECT status, COUNT(*),
                   ROUND(COALESCE(SUM(realized_pnl_net),0),2)
                   FROM paper_trades WHERE agent_id=? AND LOWER(wallet_copied)=?
                     AND status != 'cancelled' {scale_filter}
                   GROUP BY status"""
            c.execute(q, (agent_id, wallet_filter))
        else:
            c.execute(f"""SELECT status, COUNT(*),
                         ROUND(COALESCE(SUM(realized_pnl_net),0),2)
                         FROM paper_trades WHERE agent_id=?
                           AND status != 'cancelled' {scale_filter} GROUP BY status""",
                      (agent_id,))
        by_st = {r[0]: (r[1], r[2]) for r in c.fetchall()}
        n_open = by_st.get('open', (0, 0))[0]
        n_closed = sum(v[0] for k, v in by_st.items() if k != 'open')
        wins = (by_st.get('tp_hit', (0, 0))[0] + by_st.get('resolved_win', (0, 0))[0])
        pnl = round(sum(v[1] for k, v in by_st.items() if k != 'open'), 2)
        wr = round(100 * wins / n_closed, 1) if n_closed else 0
        return {
            'open': n_open, 'closed': n_closed, 'wins': wins,
            'wr': wr, 'pnl_total': pnl,
            'avg_per_trade': round(pnl / n_closed, 2) if n_closed else 0,
        }

    # ── Ventanas de conectividad (detección automática) ──────────────────────
    # Analiza detected_trades de las últimas 72h y detecta gaps >15 min.
    # clean_since = fin del último gap = inicio de la última ventana estable.
    def _detect_connectivity_gaps(min_gap_minutes=15, lookback_days=3):
        rows = c.execute("""
            SELECT detected_at FROM detected_trades
            WHERE detected_at >= datetime('now', ?)
            ORDER BY detected_at
        """, (f'-{lookback_days} days',)).fetchall()
        gaps = []
        for i in range(1, len(rows)):
            prev = rows[i-1][0]
            curr = rows[i][0]
            try:
                from datetime import datetime as _dt
                t1 = _dt.fromisoformat(prev.split('.')[0])
                t2 = _dt.fromisoformat(curr.split('.')[0])
                gap_min = (t2 - t1).total_seconds() / 60
                if gap_min >= min_gap_minutes:
                    gaps.append({
                        "start": prev[:19],
                        "end":   curr[:19],
                        "min":   round(gap_min),
                        "cause": "wifi_drop" if gap_min < 60 else "extended_outage",
                    })
            except Exception:
                continue
        clean_since = gaps[-1]["end"] if gaps else (rows[0][0][:19] if rows else None)
        return gaps, clean_since

    CONNECTIVITY_GAPS, _auto_clean = _detect_connectivity_gaps()

    # clean_since = inicio del experimento actual (primer trade post-reset),
    # no el fin del último gap. Los gaps son informativos pero no resetean
    # el punto de referencia del experimento.
    _exp_start = c.execute("""SELECT MIN(entry_at) FROM paper_trades
        WHERE agent_id IN ('DEF_COPY','DEF_COPY_05','DEF_TP')
          AND status != 'cancelled'""").fetchone()[0]
    CLEAN_SINCE = _exp_start[:19] if _exp_start else _auto_clean

    def _stats_for_window(agent_id, since, wallet_filter=None):
        """Stats filtrados a partir de 'since' — para métricas de ventana limpia."""
        scale_filter = "AND entry_size_usdc <= 25"
        # M-5 FIX (2026-06-10): si since=None (no hay trades aún), usar parámetro
        # SQL enlazado en vez de f-string. f"AND entry_at >= 'None'" es SQL inválido
        # que devuelve 0 filas silenciosamente (SQLite compara fecha contra string 'None').
        if since is None:
            extra = ""  # sin filtro temporal — todos los trades
        else:
            extra = f"AND entry_at >= '{since}'"
        if wallet_filter:
            c.execute(f"""SELECT status, COUNT(*),
                         ROUND(COALESCE(SUM(realized_pnl_net),0),2)
                         FROM paper_trades WHERE agent_id=? AND LOWER(wallet_copied)=?
                           AND status != 'cancelled' {scale_filter} {extra}
                         GROUP BY status""", (agent_id, wallet_filter))
        else:
            c.execute(f"""SELECT status, COUNT(*),
                         ROUND(COALESCE(SUM(realized_pnl_net),0),2)
                         FROM paper_trades WHERE agent_id=?
                           AND status != 'cancelled' {scale_filter} {extra}
                         GROUP BY status""", (agent_id,))
        by_st = {r[0]: (r[1], r[2]) for r in c.fetchall()}
        n_open   = by_st.get('open', (0, 0))[0]
        n_closed = sum(v[0] for k, v in by_st.items() if k != 'open')
        wins     = by_st.get('tp_hit', (0, 0))[0] + by_st.get('resolved_win', (0, 0))[0]
        pnl      = round(sum(v[1] for k, v in by_st.items() if k != 'open'), 2)
        wr       = round(100 * wins / n_closed, 1) if n_closed else 0
        return {
            'open': n_open, 'closed': n_closed, 'wins': wins,
            'wr': wr, 'pnl_total': pnl,
            'avg_per_trade': round(pnl / n_closed, 2) if n_closed else 0,
            'since': since,
        }

    shadow_stats = {}
    for ag in DEF_AGENTS:
        shadow_stats[ag] = {
            'total': _stats_for(ag),
            'by_trader': {
                trader_name: _stats_for(ag, wallet_filter=addr)
                for addr, trader_name in DEF_TRADERS.items()
            },
        }

    # L72 (2026-06-04) — Bloque equity DEF para dimensionar capital real necesario.
    # Por cada shadow + trader: capital actual bloqueado, PnL realizado,
    # unrealizado (last_check_price - entry), y pico histórico de capital
    # simultáneo (telling para saber cuánto USDC necesitas para sostenerlo).
    def _equity_for(agent_id, wallet_filter=None):
        # Filtro de escala: solo trades ≤$15 para excluir herencia del experimento $100
        scale_filter = "AND entry_size_usdc <= 25"
        if wallet_filter:
            q_open = f"""SELECT id, entry_at, exit_at, entry_size_usdc, entry_shares,
                              entry_price, last_check_price, realized_pnl_net, status
                       FROM paper_trades WHERE agent_id=? AND LOWER(wallet_copied)=?
                         AND status != 'cancelled' {scale_filter}"""
            c.execute(q_open, (agent_id, wallet_filter))
        else:
            c.execute(f"""SELECT id, entry_at, exit_at, entry_size_usdc, entry_shares,
                              entry_price, last_check_price, realized_pnl_net, status
                       FROM paper_trades WHERE agent_id=?
                         AND status != 'cancelled' {scale_filter}""", (agent_id,))
        rows = c.fetchall()
        live_cap = 0.0
        realized = 0.0
        unrealized = 0.0
        events = []
        for _id, eat, xat, sz, shares, ep, lcp, pnl, st in rows:
            sz = float(sz or 0)
            if st == 'open':
                live_cap += sz
                # unrealized via last_check_price si disponible.
                # Skip curPrice < 0.02: mercado resolviendo a 0, precio teórico
                # no refleja valor vendible real (mismo filtro que real DEF_COPY).
                if lcp is not None and shares and ep is not None and float(lcp) > 0.02:
                    unrealized += float(shares) * (float(lcp) - float(ep))
            else:
                realized += float(pnl or 0)
            # event para peak concurrent
            events.append((eat, +sz))
            events.append((xat or '9999', -sz))
        events.sort(key=lambda x: x[0])
        peak = 0.0; running = 0.0
        for _t, delta in events:
            running += delta
            if running > peak: peak = running
        return {
            'live_capital': round(live_cap, 2),
            'realized_pnl': round(realized, 2),
            'unrealized_pnl': round(unrealized, 2),
            'equity': round(realized + unrealized, 2),
            'peak_capital': round(peak, 2),
        }

    equity_block = {}
    for ag in DEF_AGENTS:
        equity_block[ag] = {
            'total': _equity_for(ag),
            'by_trader': {
                trader_name: _equity_for(ag, wallet_filter=addr)
                for addr, trader_name in DEF_TRADERS.items()
            },
        }
    # Combined peak: para una estimación TOTAL del capital necesario para sostener
    # los 3 shadows simultáneamente, recalcular usando TODOS los DEF juntos.
    c.execute("""SELECT entry_at, exit_at, entry_size_usdc
                 FROM paper_trades WHERE agent_id IN ('DEF_TP','DEF_COPY_05','DEF_COPY')
                   AND status != 'cancelled'""")
    events = []
    for eat, xat, sz in c.fetchall():
        sz = float(sz or 0)
        events.append((eat, +sz))
        events.append((xat or '9999', -sz))
    events.sort(key=lambda x: x[0])
    peak_all = 0.0; running = 0.0
    live_all = 0.0
    for _t, delta in events:
        running += delta
        if running > peak_all: peak_all = running
    # capital vivo combinado
    c.execute("""SELECT COALESCE(SUM(entry_size_usdc),0) FROM paper_trades
                 WHERE agent_id IN ('DEF_TP','DEF_COPY_05','DEF_COPY')
                   AND status='open'""")
    live_all = float(c.fetchone()[0] or 0)
    c.execute("""SELECT COALESCE(SUM(realized_pnl_net),0) FROM paper_trades
                 WHERE agent_id IN ('DEF_TP','DEF_COPY_05','DEF_COPY')
                   AND status NOT IN ('open','cancelled')""")
    realized_all = float(c.fetchone()[0] or 0)

    equity_summary = {
        'live_capital_total': round(live_all, 2),
        'realized_pnl_total': round(realized_all, 2),
        'peak_capital_combined': round(peak_all, 2),
        'recommended_real_capital': round(peak_all * 1.3, 2),  # +30% buffer
    }

    # Stats ventana limpia (sin gaps de conectividad)
    clean_stats = {}
    for ag in DEF_AGENTS:
        clean_stats[ag] = {
            'total': _stats_for_window(ag, CLEAN_SINCE),
            'by_trader': {
                trader_name: _stats_for_window(ag, CLEAN_SINCE, wallet_filter=addr)
                for addr, trader_name in DEF_TRADERS.items()
            },
        }

    definitive_shadows_block = {
        'stats': shadow_stats,
        'clean_window_stats': clean_stats,
        'connectivity_gaps': CONNECTIVITY_GAPS,
        'clean_since': CLEAN_SINCE,
        'trades': list(by_dt.values()),
        'traders': list(DEF_TRADERS.values()),
        'equity': equity_block,
        'equity_summary': equity_summary,
    }

    # ── L34 — Equity all-time desde daily_sessions (snapshot 22:00 diario) ──
    try:
        # Puntos diarios (all-time)
        daily_pts = []
        for row in c.execute("""
            SELECT date, start_ts, equity_start, pnl_day, trades_won, trades_lost, equity_end
            FROM daily_sessions ORDER BY date
        """).fetchall():
            date, start_ts, eq_start, pnl_day, won, lost, eq_end = row
            daily_pts.append({
                "t":    start_ts or (date + "T22:00:00"),
                "eq":   round(float(eq_start or 0), 2),
                "pnl":  round(float(pnl_day or 0), 2),
                "won":  int(won or 0),
                "lost": int(lost or 0),
            })
        # Añadir punto actual (equity en tiempo real como último punto)
        last_snap = c.execute(
            "SELECT equity_total FROM equity_snapshots ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if last_snap:
            import datetime as _dt
            daily_pts.append({
                "t":   _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "eq":  round(float(last_snap[0] or 0), 2),
                "pnl": None,
                "won": None,
                "lost": None,
            })
        pnl_curve["EQUITY"] = daily_pts

        # Mantener también curva intraday (últimas 48h) para zoom
        intraday = []
        for ts, eq, free, open_v, redeem, upnl in c.execute("""
            SELECT ts, equity_total, pusd_libre, open_value, pending_redeem, pnl_unrealized
            FROM equity_snapshots WHERE ts >= datetime('now','-48 hours') ORDER BY ts
        """).fetchall():
            intraday.append({
                "t":    ts,
                "eq":   round(float(eq or 0), 2),
                "free": round(float(free or 0), 2),
                "open": round(float(open_v or 0), 2),
                "rdm":  round(float(redeem or 0), 2),
                "upnl": round(float(upnl or 0), 2),
            })
        pnl_curve["EQUITY_INTRADAY"] = intraday
    except Exception as e:
        print(f"[EXPORT] equity_curve err: {e}")
        pnl_curve["EQUITY"] = []
        pnl_curve["EQUITY_INTRADAY"] = []

    conn.close()
    return {
        "started_at": started_at,
        "min_trades_target":      config.EXPERIMENT_MIN_TRADES_TARGET,
        "min_trades_hold_target": config.EXPERIMENT_MIN_TRADES_HOLD_TARGET,
        "duration_target_days":   config.EXPERIMENT_DURATION_DAYS_TARGET,
        "agents":     agents_out,
        "pnl_curve":  pnl_curve,
        "definitive_shadows": definitive_shadows_block,
    }


def export_data():
    """Genera web/data.json con datos reales de la BD."""
    try:
        # Asegurar que cualquier override del auto-tuner está reflejado en
        # config.* antes de leer (evita que un export desde proceso fresh
        # muestre defaults env-based en vez del valor real activo).
        try:
            import auto_tuner
            auto_tuner.apply_runtime_overrides()
        except Exception:
            pass

        conn = database.get_connection()
        c = conn.cursor()

        # ── 1. STATS GENERALES (últimos 30 días) ──────────────────────────────

        # Señales totales detectadas
        c.execute("""
            SELECT COUNT(*) FROM detected_trades
            WHERE detected_at > datetime('now', '-30 days')
        """)
        signals_total = c.fetchone()[0]

        # Registradas vs ignoradas
        c.execute("""
            SELECT action, COUNT(*) FROM user_feedback
            WHERE timestamp > datetime('now', '-30 days')
            GROUP BY action
        """)
        action_counts = dict(c.fetchall())
        registered = action_counts.get("copied", 0)
        ignored    = action_counts.get("ignored", 0)

        # Outcomes resueltos — bot_trades reales (paper_trade=0)
        c.execute("""
            SELECT bt.outcome, bt.pnl_usdc
            FROM bot_trades bt
            WHERE bt.paper_trade = 0
              AND bt.outcome IN ('won', 'lost')
              AND bt.executed_at > datetime('now', '-30 days')
        """)
        resolved_bots = c.fetchall()
        bot_won  = [r for r in resolved_bots if r[0] == "won"]
        bot_lost = [r for r in resolved_bots if r[0] == "lost"]
        bot_pnl  = round(sum(r[1] or 0 for r in resolved_bots), 2)

        # Outcomes de user_feedback resueltos (feedback automático sobre bot_trades)
        c.execute("""
            SELECT outcome, confidence FROM user_feedback
            WHERE outcome IN ('copied_won','copied_lost')
              AND timestamp > datetime('now', '-30 days')
        """)
        fb_resolved = c.fetchall()
        fb_won  = [r for r in fb_resolved if r[0] == "copied_won"]
        fb_lost = [r for r in fb_resolved if r[0] == "copied_lost"]

        # Outcomes de manual_trades — fuente principal de verdad del usuario
        MY_SIZE = 10.0
        c.execute("""
            SELECT status, entry_price FROM manual_trades
            WHERE status IN ('WON', 'LOST')
              AND logged_at > datetime('now', '-30 days')
        """)
        manual_resolved_rows = c.fetchall()
        manual_won_stats  = sum(1 for r in manual_resolved_rows if r[0] == "WON")
        manual_lost_stats = sum(1 for r in manual_resolved_rows if r[0] == "LOST")
        manual_pnl_stats  = round(sum(
            (MY_SIZE / float(r[1]) - MY_SIZE) if r[0] == "WON" and r[1] and float(r[1]) > 0
            else -MY_SIZE
            for r in manual_resolved_rows
        ), 2)

        # ─────────────────────────────────────────────────────────────
        # Agregado SINTÉTICO bot+manual (30d). NO es comparable con
        # my_wallet.total_pnl (oficial). Ver header del archivo.
        # OJO: si un trade entró por bot Y se registró manualmente,
        # se cuenta DOS VECES aquí. Pendiente fix en Pasada B (Fase 1.4).
        # ─────────────────────────────────────────────────────────────
        total_resolved  = (len(bot_won) + len(bot_lost)
                           + len(fb_won) + len(fb_lost)
                           + manual_won_stats + manual_lost_stats)
        total_won_count = len(bot_won) + len(fb_won) + manual_won_stats
        bot_pnl_30d_synthetic = round(bot_pnl + manual_pnl_stats, 2)
        win_rate = round(total_won_count / total_resolved * 100, 1) if total_resolved > 0 else 0
        avg_conf = 0
        if fb_resolved:
            avg_conf = round(sum(r[1] or 0 for r in fb_resolved) * 10, 1) / len(fb_resolved)

        # Evaluaciones IA (últimos 30 días)
        c.execute("""
            SELECT COUNT(*), AVG(confidence_score),
                   SUM(CASE WHEN verdict='COPY' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN verdict='SKIP' THEN 1 ELSE 0 END)
            FROM trade_evaluations
            WHERE evaluated_at > datetime('now', '-30 days')
        """)
        eval_row = c.fetchone()
        eval_total   = eval_row[0] or 0
        eval_avg_score = round(eval_row[1] or 0, 1)
        eval_copies  = eval_row[2] or 0
        eval_skips   = eval_row[3] or 0

        # ── 2. TRADES RECIENTES (últimas 50 señales) ──────────────────────────
        c.execute("""
            SELECT uf.action, uf.niche, uf.confidence, uf.outcome,
                   uf.wallet, uf.timestamp,
                   dt.market_question, dt.side, dt.entry_price,
                   dt.wallet_address,
                   bt.pnl_usdc, bt.outcome as bot_outcome,
                   te.confidence_score, te.verdict, te.summary
            FROM user_feedback uf
            LEFT JOIN detected_trades dt ON uf.trade_id = dt.id
            LEFT JOIN bot_trades bt     ON bt.detected_trade_id = dt.id
                                       AND bt.paper_trade = 0
            LEFT JOIN trade_evaluations te ON te.detected_trade_id = dt.id
            ORDER BY uf.timestamp DESC
            LIMIT 50
        """)
        rows = c.fetchall()

        trades = []
        for r in rows:
            action, niche, conf, fb_outcome, uf_wallet, ts = r[0], r[1], r[2], r[3], r[4], r[5]
            question, side, price, dt_wallet              = r[6], r[7], r[8], r[9]
            pnl_usdc, bot_out                             = r[10], r[11]
            eval_score, eval_verdict, eval_summary        = r[12], r[13], r[14]

            # Determinar resultado final
            if bot_out in ("won", "lost"):
                result = "won" if bot_out == "won" else "lost"
            elif fb_outcome == "copied_won":
                result = "won"
            elif fb_outcome == "copied_lost":
                result = "lost"
            else:
                result = None  # pendiente

            wallet_short = (dt_wallet or uf_wallet or "")
            if wallet_short:
                wallet_short = wallet_short[:8] + "…"

            trades.append({
                "market":       (question or "Mercado desconocido")[:80],
                "trader":       wallet_short,
                "side":         side or "YES",
                "entry":        round(float(price or 0), 3),
                "confidence":   round(float(conf or 0) * 10, 1),
                "action":       _ACTION_LABEL.get(action, action),
                "result":       result,
                "pnl":          round(float(pnl_usdc or 0), 2) if pnl_usdc is not None else None,
                "niche":        _NICHE_LABEL.get(niche, niche or "Otro"),
                "date":         ts,
                "eval_score":   int(eval_score) if eval_score else None,
                "eval_verdict": eval_verdict,
                "eval_summary": eval_summary,
            })

        # ── 3. TRADERS MONITOREADOS ───────────────────────────────────────────
        traders = []

        # TRUSTED (con override de datos conocidos)
        for wallet, info in TRUSTED_DISPLAY.items():
            c.execute("""
                SELECT username, win_rate, profit_factor, closed_total, insider_score
                FROM wallets WHERE address = ?
            """, (wallet,))
            row = c.fetchone()

            c.execute("""
                SELECT total_copied, total_won, total_lost, real_pnl_usdc, tier
                FROM trader_reputation WHERE wallet = ?
            """, (wallet,))
            rep = c.fetchone()

            db_wr = round(float(row[1] or 0) * 100, 1) if row else 0
            traders.append({
                "address":       wallet,
                "name":          info["name"],
                "emoji":         info["emoji"],
                "niche":         _NICHE_LABEL.get(info["niche"], info["niche"]),
                "win_rate":      db_wr,
                "profit_factor": round(float(row[2] or 0), 2) if row else 0,
                "closed_total":  int(row[3] or 0) if row else 0,
                "insider_score": round(float(row[4] or 0), 3) if row else 0,
                "known_pnl":     info.get("real_pnl"),
                "bot_copied":    int(rep[0] or 0) if rep else 0,
                "bot_won":       int(rep[1] or 0) if rep else 0,
                "bot_lost":      int(rep[2] or 0) if rep else 0,
                "bot_pnl":       round(float(rep[3] or 0), 2) if rep else 0,
                "tier":          rep[4] if rep else "NORMAL",
                "type":          "TRUSTED",
            })

        # Hot streak activos
        c.execute("""
            SELECT wallet, username, last_10_wr, last_5_wr, historical_wr,
                   total_pnl, profit_factor, detected_at, expires_at
            FROM hot_streak_traders
            WHERE status = 'ACTIVE' AND expires_at > datetime('now')
            ORDER BY total_pnl DESC LIMIT 5
        """)
        for row in c.fetchall():
            traders.append({
                "address":       row[0],
                "name":          row[1] or row[0][:10] + "…",
                "emoji":         "🔥",
                "niche":         "—",
                "win_rate":      round(float(row[4] or 0) * 100, 1),
                "last_10_wr":    round(float(row[2] or 0) * 100, 1),
                "last_5_wr":     round(float(row[3] or 0) * 100, 1),
                "known_pnl":     round(float(row[5] or 0), 0),
                "profit_factor": round(float(row[6] or 0), 2),
                "detected_at":   row[7],
                "expires_at":    row[8],
                "type":          "HOT_STREAK",
            })

        # ── 4. ACTIVIDAD POR NICHO (últimos 30 días) ──────────────────────────
        c.execute("""
            SELECT uf.niche, COUNT(*) as total,
                   SUM(CASE WHEN uf.action='copied' THEN 1 ELSE 0 END) as copied,
                   SUM(CASE WHEN uf.action='ignored' THEN 1 ELSE 0 END) as ignored
            FROM user_feedback uf
            WHERE uf.timestamp > datetime('now', '-30 days')
            GROUP BY uf.niche
            ORDER BY total DESC
        """)
        niche_rows = c.fetchall()
        niches = []
        for row in niche_rows:
            niches.append({
                "niche":   _NICHE_LABEL.get(row[0], row[0] or "Otro"),
                "total":   row[1],
                "copied":  row[2] or 0,
                "ignored": row[3] or 0,
            })

        # ── 5. TRADES MANUALES (con $10 fijos por trade) ─────────────────────
        # MY_SIZE ya definido arriba (= 10.0)
        c.execute("""
            SELECT id, market_question, side, entry_price, copied_from,
                   status, won, pnl, logged_at, resolved_at, niche
            FROM manual_trades ORDER BY id DESC
        """)
        manual_rows = c.fetchall()

        manual_trades_list = []
        manual_won = 0
        manual_lost = 0
        manual_open = 0
        manual_pnl_real = 0.0
        manual_pnl_latent = 0.0  # para los abiertos, usaremos 0 (no tenemos precio actual aquí)

        for row in manual_rows:
            tid, question, side, entry, trader, status, won_flag, pnl_db, logged, resolved, niche = row
            entry = float(entry or 0)

            if status == 'WON':
                real_pnl = round(MY_SIZE / entry - MY_SIZE, 2) if entry > 0 else 0
                manual_won += 1
                manual_pnl_real += real_pnl
            elif status == 'LOST':
                real_pnl = round(-MY_SIZE, 2)
                manual_lost += 1
                manual_pnl_real += real_pnl
            else:
                real_pnl = None
                manual_open += 1

            manual_trades_list.append({
                "id":        tid,
                "market":    (question or "?")[:80],
                "side":      side or "?",
                "entry":     round(entry, 3),
                "trader":    trader or "manual",
                "status":    status,
                "won":       bool(won_flag) if won_flag is not None else None,
                "pnl":       real_pnl,
                "my_size":   MY_SIZE,
                "logged_at": logged,
                "resolved_at": resolved,
                "niche":     _NICHE_LABEL.get(niche, niche or "Otro"),
            })

        manual_resolved = manual_won + manual_lost
        manual_wr = round(manual_won / manual_resolved * 100, 1) if manual_resolved > 0 else 0

        # ── 5b. SEÑALES RECIENTES (sin feedback — solo detectadas) ───────────
        c.execute("""
            SELECT dt.market_question, dt.side, dt.entry_price,
                   dt.confidence_score, dt.days_left, dt.wallet_address,
                   dt.detected_at, dt.retorno,
                   te.confidence_score as eval_score, te.verdict
            FROM detected_trades dt
            LEFT JOIN trade_evaluations te ON te.detected_trade_id = dt.id
            WHERE dt.detected_at > datetime('now', '-24 hours')
              AND (dt.action_taken IS NULL OR dt.action_taken = 'pending')
            ORDER BY dt.detected_at DESC LIMIT 20
        """)
        signals = []
        for row in c.fetchall():
            signals.append({
                "market":     (row[0] or "?")[:80],
                "side":       row[1],
                "entry":      round(float(row[2] or 0), 3),
                "confidence": round(float(row[3] or 0) * 100, 1),
                "days_left":  row[4],
                "wallet":     (row[5] or "")[:8] + "…" if row[5] else "?",
                "detected_at":row[6],
                "retorno":    round(float(row[7] or 0), 2) if row[7] else None,
                "eval_score": int(row[8]) if row[8] else None,
                "eval_verdict":row[9],
            })

        conn.close()

        # ── 6. MI WALLET (posiciones reales) ─────────────────────────────────
        try:
            my_wallet_data = my_wallet_tracker.get_my_wallet_data()
        except Exception as e:
            print(f"[EXPORT] ⚠️  my_wallet_tracker error: {e}")
            my_wallet_data = my_wallet_tracker._empty_result(str(e))

        # ── 6b. MI ESTILO (behavior analyzer — Fase 2.2) ─────────────────────
        # Lee el último snapshot de user_profile_snapshots. El loop diario
        # (behavior_snapshot_loop en main.py) lo refresca cada 24h.
        try:
            my_behavior = my_behavior_analyzer.get_latest_snapshot()
        except Exception as e:
            print(f"[EXPORT] ⚠️  my_behavior_analyzer error: {e}")
            my_behavior = None

        # ── 6c. PAPER TRADING (Fase 2.3) ─────────────────────────────────────
        try:
            paper_data = paper_executor.get_paper_stats()
            paper_data["comparison_14d"] = paper_executor.get_comparison_with_manual(14)
            paper_data["enabled"] = config.PAPER_TRADING_ENABLED
            paper_data["config"] = {
                "tp":          config.PAPER_TP_MULTIPLIER,
                "sl":          config.PAPER_SL_MULTIPLIER,
                "size":        config.PAPER_SIZE_USDC,
                "fee":         config.PAPER_FEES_ESTIMATED,
                "timeout_days": config.PAPER_TIMEOUT_DAYS,
            }
        except Exception as e:
            print(f"[EXPORT] ⚠️  paper_executor error: {e}")
            paper_data = {"enabled": False, "stats": {}, "recent": []}

        # ── 6d. AUTO-TUNER (Fase 2.4) ────────────────────────────────────────
        try:
            tuner_data = auto_tuner.get_dashboard_state()
        except Exception as e:
            print(f"[EXPORT] ⚠️  auto_tuner error: {e}")
            tuner_data = {"enabled": False, "params": [], "actions": []}

        # ── 6e. RISK MANAGER (Fase 2.5) ──────────────────────────────────────
        try:
            import risk_manager
            risk_data = risk_manager.get_dashboard_state()
        except Exception as e:
            print(f"[EXPORT] ⚠️  risk_manager error: {e}")
            risk_data = {"enabled": False, "active_vetoes": []}

        # ── 6f. EXPERIMENTO MULTI-AGENTE (Fase 2.6) ──────────────────────────
        try:
            experiment_data = _build_experiment_block()
        except Exception as e:
            print(f"[EXPORT] experiment error: {e}")
            experiment_data = {"agents": [], "pnl_curve": {}}

        # ── 6g. REAL TRADING DEF_COPY ────────────────────────────────────────
        try:
            real_trading_data = _build_real_trading_block(my_wallet_data, agent_id='DEF_COPY')
        except Exception as e:
            print(f"[EXPORT] real_trading B error: {e}")
            real_trading_data = {"estado": "BLOQUEADO", "open_positions": [],
                                 "recent_trades": [], "filtered_24h": {}}

        # ── 6g-D. REAL TRADING D (hold-to-resolve) ──────────────────────────
        try:
            real_trading_d_data = _build_real_trading_block(my_wallet_data, agent_id='D')
            # Override config para D (trade_size, sin TP/SL)
            real_trading_d_data["config"] = {
                "agent": "D",
                "trade_size_usdc": float(os.getenv("REAL_D_TRADE_SIZE", "3")),
                "max_slippage": config.MAX_SLIPPAGE,
                "excluded_niches": config.REAL_EXCLUDED_NICHES,
                "max_open_positions": config.MAX_OPEN_POSITIONS,
                "strategy": "hold-to-resolve",
                "entry_range": [float(os.getenv("REAL_D_ENTRY_MIN", "0.30")),
                                 float(os.getenv("REAL_D_ENTRY_MAX", "0.50"))],
                "insider_size_range": [float(os.getenv("REAL_D_INSIDER_SIZE_MIN", "100")),
                                        float(os.getenv("REAL_D_INSIDER_SIZE_MAX", "2000"))],
            }
        except Exception as e:
            print(f"[EXPORT] real_trading D error: {e}")
            real_trading_d_data = {"estado": "BLOQUEADO", "open_positions": [],
                                    "recent_trades": [], "filtered_24h": {}}

        # ── 6h. FASE DE ESCALADO (Fase A) ────────────────────────────────────
        try:
            scale_phase_data = _build_scale_phase_block()
        except Exception as e:
            print(f"[EXPORT] scale_phase error: {e}")
            scale_phase_data = {"active": False, "phase": "OFF"}

        # ── 6i. DIVERGENCE paper-vs-real (Fase 4.7) ──────────────────────────
        try:
            import safety as _safety_mod
            divergence_data = _safety_mod.divergence_monitor(
                window=20, alert_threshold_pp=25.0, notify=False)
        except Exception as e:
            print(f"[EXPORT] divergence error: {e}")
            divergence_data = {"active": False}

        # ── 7. ENSAMBLAR Y ESCRIBIR ───────────────────────────────────────────
        data = {
            "updated_at": datetime.now().isoformat(),
            # stats.* = métricas SINTÉTICAS de actividad del bot+manual (30d).
            # NO comparable con my_wallet.* (que es on-chain real).
            # El sufijo _30d_synthetic deja explícito el origen.
            "stats": {
                "signals_total_30d":    signals_total,
                "registered":           registered,
                "ignored":              ignored,
                "resolved":             total_resolved,
                "won":                  total_won_count,
                "lost":                 total_resolved - total_won_count,
                "win_rate":             win_rate,
                "bot_pnl_30d":          bot_pnl_30d_synthetic,  # NUEVO: nombre canónico
                "total_pnl":            bot_pnl_30d_synthetic,  # LEGACY alias (consumidores antiguos)
                "_pnl_source":          "synthetic_bot+manual_30d",
                "avg_confidence":       round(avg_conf, 1),
                "eval_total":           eval_total,
                "eval_avg_score":       eval_avg_score,
                "eval_copies":          eval_copies,
                "eval_skips":           eval_skips,
            },
            # manual.* = lo que el usuario REGISTRA vía /logtrade en Telegram.
            # PnL es SINTÉTICO ($10 fijo/trade), NO el tamaño real on-chain.
            # Para ver PnL real: my_wallet.summary.total_pnl (oficial Polymarket).
            "manual": {
                "trades":                  manual_trades_list,
                "trade_size":              MY_SIZE,
                "total":                   len(manual_trades_list),
                "open":                    manual_open,
                "won":                     manual_won,
                "lost":                    manual_lost,
                "resolved":                manual_resolved,
                "win_rate":                manual_wr,
                "pnl_synthetic_10usdc":    round(manual_pnl_real, 2),  # NUEVO nombre canónico
                "pnl_real":                round(manual_pnl_real, 2),  # LEGACY alias
                "_pnl_source":             "synthetic_10usdc_per_trade",
                "invested_synthetic":      round(MY_SIZE * manual_resolved, 2),
                "invested_total":          round(MY_SIZE * manual_resolved, 2),  # LEGACY alias
                "roi_pct_synthetic":       round(manual_pnl_real / (MY_SIZE * manual_resolved) * 100, 1)
                                           if manual_resolved > 0 else 0,
                "roi_pct":                 round(manual_pnl_real / (MY_SIZE * manual_resolved) * 100, 1)
                                           if manual_resolved > 0 else 0,  # LEGACY alias
            },
            "trades":  trades,
            "traders": traders,
            "niches":  niches,
            "signals":     signals,
            "my_wallet":   my_wallet_data,
            "my_behavior": my_behavior,
            "paper":       paper_data,
            "tuner":       tuner_data,
            "risk":        risk_data,
            "experiment":  experiment_data,
            "experiment_mode": config.EXPERIMENT_MODE,
            "real_trading":   real_trading_data,      # alias retro-compat = B
            "real_trading_b": real_trading_data,
            "real_trading_d": real_trading_d_data,     # agente D (desactivado)
            "real_trading_copy": _build_real_copy_block(),  # DEF_COPY real
        }
        # L46d (2026-06-02) — REVERTIDO L46b combine B+D.
        # B y D comparten la misma proxy wallet → mismo balance, mismo
        # open_value en data-api. Sumar D.open_value al equity de B doble-
        # contaba posiciones (auditoría: dash $94.77 vs real $78.45,
        # gap exactamente = D.open_value $23.61).
        # En su lugar añadimos LOCKED en open BUY orders (pUSD bloqueado
        # por órdenes BUY pendientes, no devuelto por balanceOf).
        try:
            _locked = 0.0
            try:
                import executor as _ex
                _client = _ex.get_client()
                _orders = _client.get_open_orders() or []
                for o in _orders:
                    if o.get('side') != 'BUY':
                        continue
                    orig = float(o.get('original_size', 0) or 0)
                    matched = float(o.get('size_matched', 0) or 0)
                    price = float(o.get('price', 0) or 0)
                    _locked += max(0.0, (orig - matched) * price)
            except Exception:
                pass  # CLOB transitorio — locked=0, el equity no se rompe
            data["real_trading"] = dict(data["real_trading"])
            # L46e (2026-06-02) — Equity REAL on-chain.
            # El equity per-agente B solo incluye SUS trades; ignora open
            # positions del agente D que viven en la misma proxy. Resultado:
            # dashboard subreporta (ej. $59.54 cuando real es $83+).
            # Fix: usar PM data-api currentValue de TODAS las posiciones
            # del proxy + DW como open_value canónico.
            import os as _os_mod, requests
            _open_pm = 0.0
            for _addr in (_os_mod.getenv('DEPOSIT_WALLET_ADDRESS'),
                          _os_mod.getenv('POLY_WALLET_ADDRESS')):
                if not _addr:
                    continue
                try:
                    _r = requests.get(
                        f'https://data-api.polymarket.com/positions'
                        f'?user={_addr}&limit=500&sizeThreshold=0.01',
                        timeout=10)
                    for _p in (_r.json() or []):
                        if _p.get('redeemable', False):
                            continue
                        _open_pm += float(_p.get('currentValue', 0) or 0)
                except Exception:
                    pass
            data["real_trading"]["open_value_usdc"] = round(_open_pm, 2)
            data["real_trading"]["locked_in_orders_usdc"] = round(_locked, 2)
            # L46f (2026-06-02) — usar redeemable canónico de PM API.
            # El cálculo per-agente subreporta cuando recién resuelven
            # mercados (ej. trades B cerrados +redeemable que el bot D no ve).
            _red_pm = 0.0
            # Tokens en posiciones ABIERTAS del bot — ya contados en capital_open.
            _open_toks_eq = set()
            _open_cost_eq = {}  # token_id → sum(size_filled) para detectar resueltas
            try:
                _conn_ot = database.get_connection()
                for (_ot, _sf) in _conn_ot.execute(
                    "SELECT token_id, COALESCE(size_filled,0) FROM bot_trades "
                    "WHERE paper_trade=0 AND status='open' AND token_id IS NOT NULL"
                ).fetchall():
                    if _ot:
                        _open_toks_eq.add(str(_ot))
                        _open_cost_eq[str(_ot)] = _open_cost_eq.get(str(_ot), 0) + float(_sf or 0)
                _conn_ot.close()
            except Exception:
                pass
            _resolved_open_cost = 0.0  # coste de open trades que ya resolvieron ganador
            for _addr in (_os_mod.getenv('DEPOSIT_WALLET_ADDRESS'),
                          _os_mod.getenv('POLY_WALLET_ADDRESS')):
                if not _addr: continue
                try:
                    _r = requests.get(
                        f'https://data-api.polymarket.com/positions'
                        f'?user={_addr}&limit=500&sizeThreshold=0.01',
                        timeout=10)
                    for _p in (_r.json() or []):
                        _tok = str(_p.get('asset') or '')
                        _cv  = float(_p.get('currentValue', 0) or 0)
                        # Equity total estable: valorar shares a $1/share (face value),
                        # no a currentValue (precio de mercado fluctuante).
                        # size = nº de shares on-chain; cada share ganadora vale $1.
                        _sz  = float(_p.get('size', 0) or 0)
                        _face = _sz if _sz > 0.01 else _cv  # fallback si no hay size
                        _pps = (_cv / _sz) if _sz > 0.01 else 0  # precio por share
                        _redeemable = bool(_p.get('redeemable'))
                        if _cv > 0.01 and _tok not in _open_toks_eq:
                            _red_pm += _face
                        elif _cv > 0.01 and _tok in _open_toks_eq and (_redeemable or _pps >= 0.999):
                            # Mercado resuelto ganador (redeemable=True o pps≈1.0) pero trade
                            # aún 'open' en DB. Usar face value en redeem y descontar coste.
                            # pps >= 0.95 causaba falsos positivos en posiciones de precio alto
                            # sin resolver (ej: compradas a 0.96 en mercado aún abierto).
                            _red_pm += _face
                            _resolved_open_cost += _open_cost_eq.get(_tok, 0)
                except Exception:
                    pass
            data["real_trading"]["pending_redeem_usdc"] = round(_red_pm, 2)
            _bal = float(data["real_trading"].get("balance_usdc") or 0)
            _ext = float(data["real_trading"].get("extras_total_usdc") or 0)
            # L80: equity = dinero libre + extras (proxy) + redeem + PnL realizado DEF_COPY
            # NO sumamos posiciones abiertas — ya salieron del balance cuando se compraron.
            # La equity solo sube cuando un trade cierra con ganancia.
            try:
                _conn_bt = database.get_connection()
                _pnl_row = _conn_bt.execute(
                    "SELECT COALESCE(SUM(pnl_usdc),0) FROM bot_trades "
                    "WHERE agent_id='DEF_COPY' AND paper_trade=0 AND status='closed'"
                ).fetchone()
                _conn_bt.close()
                _pnl_real = float(_pnl_row[0] or 0)
            except Exception:
                _pnl_real = 0.0
            # Equity = libre + extras + redeem + coste posiciones abiertas + PnL realizado
            try:
                _conn_bt2 = database.get_connection()
                _cap_row = _conn_bt2.execute(
                    "SELECT COALESCE(SUM(size_filled),0) FROM bot_trades "
                    "WHERE agent_id='DEF_COPY' AND paper_trade=0 AND status='open'"
                ).fetchone()
                _conn_bt2.close()
                _cap_def = float(_cap_row[0] or 0) - _resolved_open_cost
            except Exception:
                _cap_def = 0.0
            # Equity = pUSD libre + capital a coste (size_filled) + redeem
            # NO mark-to-market: equity solo cambia con PnL realizado
            _ex = data["real_trading"].get("extras_usdc", {}) or {}
            _pusd_proxy_rpc = float(_ex.get("pusd_proxy", 0) or 0)
            _usdc_dw    = float(_ex.get("usdc_dw", 0) or 0)
            _usdc_proxy = float(_ex.get("usdc_proxy", 0) or 0)
            # RPC poco fiable: caché solo cuando usdc_proxy=0 (mismo dinero).
            # Si hay USDC en proxy, las shares se redimieron a USDC → pusd_proxy=0.
            # proxy pUSD: solo confiar en RPC en vivo. El flujo proxy ya está
            # desactivado (auto_transfer eliminado) y el pUSD que pasó por proxy
            # siempre terminó en CLOB libre — usar cache causaba doble conteo.
            _pusd_proxy = _pusd_proxy_rpc  # 0 si RPC da 0, valor real si hay algo
            data["real_trading"]["capital_open_usdc"] = round(_cap_def, 2)
            _eq_computed = round(_bal + _pusd_proxy + _usdc_dw + _usdc_proxy + _cap_def + _red_pm, 2)
            data["real_trading"]["equity_total_usdc"] = _eq_computed
            _bd = {
                "pusd_libre_dw": round(_bal, 2),
                "pusd_proxy":    round(_pusd_proxy, 2),
                "usdc_libre_dw": round(_usdc_dw, 2),
                "usdc_proxy":    round(_usdc_proxy, 2),
                "posiciones_valor": round(_open_pm, 2),
                "posiciones_coste": round(_cap_def, 2),
                "redeem":        round(_red_pm, 2),
                "ordenes_pendientes": round(_locked, 2),
                "pnl_realizado_acum": round(_pnl_real, 2),
            }
            data["real_trading"]["equity_breakdown"] = _bd
            # F3 (2026-06-18) — equity_total debe = suma de los 6 componentes
            # que entran en la fórmula. Si hay divergencia alguien modificó
            # la fórmula o el breakdown sin actualizar el otro.
            _bd_sum = round(
                _bd["pusd_libre_dw"] + _bd["pusd_proxy"] + _bd["usdc_libre_dw"]
                + _bd["usdc_proxy"] + _bd["posiciones_coste"] + _bd["redeem"], 2)
            _eq_gap = round(abs(_bd_sum - _eq_computed), 4)
            if _eq_gap > 0.05:
                print(f"[EQUITY][F3-ERROR] breakdown_sum={_bd_sum} != equity={_eq_computed} gap={_eq_gap}")
                data["real_trading"]["equity_validation_error"] = _eq_gap
            else:
                data["real_trading"].pop("equity_validation_error", None)
        except Exception as _e:
            print(f"[EXPORT] L46e canonical err: {_e}")
        data["scale_phase"] = scale_phase_data
        data["divergence"] = divergence_data

        # L46c — snapshot equity DESPUÉS del combine B+D
        try:
            rt_final = data["real_trading"]
            eq_total = float(rt_final.get("equity_total_usdc") or 0)
            balance = float(rt_final.get("balance_usdc") or 0)
            open_val = float(rt_final.get("open_value_usdc") or 0)
            redeem = float(rt_final.get("pending_redeem_usdc") or 0)
            extras = float(rt_final.get("extras_total_usdc") or 0)
            upnl = float(rt_final.get("open_unrealized_pnl_usdc") or 0)
            conn_s = database.get_connection()
            # Guard L38: si pending_redeem cayó masivo, probable RPC fail
            prev = conn_s.execute("""SELECT pending_redeem FROM equity_snapshots
                ORDER BY ts DESC LIMIT 1""").fetchone()
            skip = False
            if prev and float(prev[0] or 0) > 20 and redeem < 1 and extras < 1:
                skip = True
                print(f"[EXPORT] L46c snapshot SKIPPED — RPC suspect")
            if not skip:
                ts = datetime.utcnow().replace(microsecond=0).isoformat()
                conn_s.execute("""INSERT OR REPLACE INTO equity_snapshots
                    (ts, equity_total, pusd_libre, open_value,
                     pending_redeem, extras_total, pnl_unrealized)
                    VALUES (?,?,?,?,?,?,?)""",
                    (ts, eq_total, round(balance, 2), round(open_val, 2),
                     round(redeem, 2), round(extras, 2), round(upnl, 2)))
                conn_s.execute("""DELETE FROM equity_snapshots
                    WHERE ts < datetime('now','-30 days')""")
                conn_s.commit()
            conn_s.close()
        except Exception as _e:
            print(f"[EXPORT] L46c snapshot err: {_e}")

        os.makedirs(os.path.dirname(DATA_JSON_PATH), exist_ok=True)
        with open(DATA_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str, ensure_ascii=False)

        wsum = my_wallet_data.get("summary", {})
        print(f"[EXPORT] ✅ data.json → {len(trades)} feedbacks · "
              f"{len(signals)} señales · manual: {manual_won}W/{manual_lost}L "
              f"PnL {manual_pnl_real:+.2f} USDC · "
              f"wallet: {wsum.get('open_count',0)} abiertas / "
              f"{wsum.get('closed_count',0)} cerradas")

    except Exception as e:
        import traceback
        print(f"[EXPORT] ❌ Error: {e}")
        traceback.print_exc()


if __name__ == "__main__":
    export_data()
