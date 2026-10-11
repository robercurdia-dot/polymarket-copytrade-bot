"""
reputation_tracker.py — Resolución automática de trades y actualización de reputación.
Se ejecuta cada hora desde main.py.
"""
import asyncio
import requests
from datetime import datetime
import database

CLOB_URL  = "https://clob.polymarket.com/markets/"
GAMMA_URL = "https://gamma-api.polymarket.com/markets?conditionId="

def get_market_result(market_id, side, token_id=None):
    """
    Consulta si el mercado resolvió.
    Devuelve:
      'won'   si el lado elegido ganó
      'lost'  si perdió
      'open'  si aún no resolvió
    """
    try:
        # Intentar CLOB primero
        r = requests.get(CLOB_URL + market_id, timeout=10)
        if r.status_code == 200:
            data = r.json()
            tokens = data.get("tokens", [])
            for token in tokens:
                tid = token.get("token_id", "")
                winner = token.get("winner", False)
                outcome = token.get("outcome", "").upper()
                # Comparar por token_id si lo tenemos
                if token_id and tid == token_id:
                    if winner is True:
                        return "won"
                    elif winner is False and data.get("resolved", False):
                        return "lost"
                # Fallback por side/outcome
                elif not token_id:
                    if side.upper() in ["BUY", "YES"] and outcome == "YES" and winner is True:
                        return "won"
                    elif side.upper() in ["SELL", "NO"] and outcome == "NO" and winner is True:
                        return "won"
    except Exception as e:
        print(f"[TRACKER] Error CLOB {market_id[:12]}: {e}")

    # Fallback: Gamma API
    try:
        r = requests.get(GAMMA_URL + market_id, timeout=10)
        if r.status_code == 200:
            data = r.json()
            markets = data if isinstance(data, list) else [data]
            for m in markets:
                if not m.get("closed", False) and not m.get("resolved", False):
                    return "open"
                outcomes = m.get("outcomes", "[]")
                winner_idx = m.get("winnerIndex")
                if winner_idx is None:
                    return "open"
                # Determinar si nuestro lado ganó
                if isinstance(outcomes, str):
                    import json
                    try:
                        outcomes = json.loads(outcomes)
                    except Exception:
                        outcomes = []
                if side.upper() in ["BUY", "YES"] and winner_idx == 0:
                    return "won"
                elif side.upper() in ["SELL", "NO"] and winner_idx == 1:
                    return "won"
                return "lost"
    except Exception as e:
        print(f"[TRACKER] Error Gamma {market_id[:12]}: {e}")

    return "open"

def calc_pnl(trade, result):
    """Calcula PnL aproximado de un trade dado el resultado."""
    size   = float(trade.get("size_usdc") or 0) or 1.0
    price  = float(trade.get("entry_price") or 0) or 0.5
    if price <= 0 or price >= 1:
        price = 0.5
    if result == "won":
        # Ganamos: recibimos size/price - size
        return round(size / price - size, 4)
    elif result == "lost":
        return round(-size, 4)
    return 0.0

async def check_resolved_trades():
    """Revisa todos los bot_trades abiertos y actualiza outcome + reputación."""
    open_trades = database.get_open_bot_trades()
    if not open_trades:
        print("[TRACKER] Sin trades abiertos que revisar")
        return

    print(f"[TRACKER] Revisando {len(open_trades)} trades abiertos...")
    resolved = 0
    for trade in open_trades:
        result = get_market_result(
            trade["market_id"],
            trade["side"],
            trade.get("token_id")
        )
        if result in ("won", "lost"):
            pnl = calc_pnl(trade, result)
            database.close_bot_trade(trade["id"], result, pnl)
            wallet = trade.get("wallet", "")
            if wallet and wallet != "eonet_nasa":
                tier = database.update_trade_result(wallet, result == "won", pnl)
                print(f"[TRACKER] Trade {trade['id']} → {result.upper()} | PnL {pnl:+.2f} USDC | {wallet[:10]} tier={tier}")
            else:
                print(f"[TRACKER] Trade {trade['id']} → {result.upper()} | PnL {pnl:+.2f} USDC")
            resolved += 1
        # Pequeña pausa para no saturar la API
        await asyncio.sleep(0.5)

    print(f"[TRACKER] {resolved}/{len(open_trades)} trades resueltos")

async def reputation_loop():
    """Loop principal: ejecuta check_resolved_trades cada hora."""
    print("[TRACKER] Reputation tracker iniciado (cada hora)")
    while True:
        await asyncio.sleep(3600)  # esperar 1 hora
        try:
            await check_resolved_trades()
        except Exception as e:
            print(f"[TRACKER] Error en loop: {e}")
