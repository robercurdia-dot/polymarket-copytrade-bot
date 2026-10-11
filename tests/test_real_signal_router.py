"""
test_real_signal_router.py — Fase 3.1
Tests de la capa Agente → Real Trading. Aislado vía POLY_DB_PATH.
"""
import os
import sys

os.environ["POLY_DB_PATH"] = "/tmp/test_router.db"
if os.path.exists("/tmp/test_router.db"):
    os.remove("/tmp/test_router.db")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config
import real_signal_router
import risk_manager

# Señal y decisiones base reutilizables
SIGNAL = {
    "detected_trade_id": 1, "market_id": "CID_X", "token_id": "TOK_X",
    "side": "BUY", "outcome_name": "YES", "question": "test",
    "insider_price": 0.45,
}
DECISIONS_OPEN = {
    "A": {"action": "open", "entry_price": 0.45, "size": 100},
    "B": {"action": "open", "entry_price": 0.45, "size": 100},
    "C": {"action": "open", "entry_price": 0.45, "size": 100},
}

# Guardamos los originales para restaurar
_orig = {
    "PAPER_TRADING": config.PAPER_TRADING,
    "REAL_TRADING_ENABLED": config.REAL_TRADING_ENABLED,
    "REAL_TRADING_AGENT": config.REAL_TRADING_AGENT,
    "can_trade": risk_manager.can_trade,
}

# Stub de execute_trade para que los tests NO toquen el CLOB
import executor
_exec_calls = []
def _fake_execute_trade(**kwargs):
    _exec_calls.append(kwargs)
    return {"status": "ok", "bot_trade_id": 999}
executor.execute_trade = _fake_execute_trade


def _reset():
    _exec_calls.clear()
    risk_manager.can_trade = lambda size, agent_id=None: (True, "ok")


print("=" * 64)
print(" TEST real_signal_router")
print("=" * 64)

# ── TEST 1: PAPER_TRADING=true → False ───────────────────────────────
_reset()
config.PAPER_TRADING = True
config.REAL_TRADING_ENABLED = True
config.REAL_TRADING_AGENT = "B"
r = real_signal_router.route_signal_to_real(SIGNAL, DECISIONS_OPEN)
assert r is False, f"esperado False, got {r}"
assert len(_exec_calls) == 0
print("TEST 1 PASA — PAPER_TRADING=true → route returns False (no real)")

# ── TEST 2: REAL_TRADING_ENABLED=false → False ───────────────────────
_reset()
config.PAPER_TRADING = False
config.REAL_TRADING_ENABLED = False
config.REAL_TRADING_AGENT = "B"
r = real_signal_router.route_signal_to_real(SIGNAL, DECISIONS_OPEN)
assert r is False, f"esperado False, got {r}"
assert len(_exec_calls) == 0
print("TEST 2 PASA — REAL_TRADING_ENABLED=false → route returns False")

# ── TEST 3: REAL_TRADING_AGENT='NONE' → False ────────────────────────
_reset()
config.PAPER_TRADING = False
config.REAL_TRADING_ENABLED = True
config.REAL_TRADING_AGENT = "NONE"
r = real_signal_router.route_signal_to_real(SIGNAL, DECISIONS_OPEN)
assert r is False, f"esperado False, got {r}"
assert len(_exec_calls) == 0
print("TEST 3 PASA — REAL_TRADING_AGENT='NONE' → route returns False")

# ── TEST 4: agente B decide 'skip' → False ───────────────────────────
_reset()
config.PAPER_TRADING = False
config.REAL_TRADING_ENABLED = True
config.REAL_TRADING_AGENT = "B"
decisions_skip = dict(DECISIONS_OPEN)
decisions_skip["B"] = {"action": "skip"}
r = real_signal_router.route_signal_to_real(SIGNAL, decisions_skip)
assert r is False, f"esperado False, got {r}"
assert len(_exec_calls) == 0
print("TEST 4 PASA — agente B decide 'skip' → route returns False")

# ── TEST 5: agente B decide 'open' + flags ok → True + ejecuta ───────
_reset()
config.PAPER_TRADING = False
config.REAL_TRADING_ENABLED = True
config.REAL_TRADING_AGENT = "B"
r = real_signal_router.route_signal_to_real(SIGNAL, DECISIONS_OPEN)
assert r is True, f"esperado True, got {r}"
assert len(_exec_calls) == 1, f"esperado 1 llamada a execute_trade, got {len(_exec_calls)}"
assert _exec_calls[0]["detected_trade_id"] == 1
assert _exec_calls[0]["size_usdc"] == config.MAX_TRADE_SIZE_USDC
print("TEST 5 PASA — agente B 'open' + flags ok → route returns True, ejecuta")

# ── TEST 6: risk_manager.can_trade=False → False ─────────────────────
_reset()
config.PAPER_TRADING = False
config.REAL_TRADING_ENABLED = True
config.REAL_TRADING_AGENT = "B"
risk_manager.can_trade = lambda size, agent_id=None: (False, "veto_DRAWDOWN")
r = real_signal_router.route_signal_to_real(SIGNAL, DECISIONS_OPEN)
assert r is False, f"esperado False, got {r}"
assert len(_exec_calls) == 0
print("TEST 6 PASA — risk_manager.can_trade=False → route returns False")

# ── TEST 7: niche excluido → False (Fase 3.1b) ───────────────────────
_reset()
config.PAPER_TRADING = False
config.REAL_TRADING_ENABLED = True
config.REAL_TRADING_AGENT = "B"
config.REAL_EXCLUDED_NICHES = ["geopolitics"]
sig_geo = dict(SIGNAL); sig_geo["niche"] = "geopolitics"
r = real_signal_router.route_signal_to_real(sig_geo, DECISIONS_OPEN)
assert r is False, f"esperado False, got {r}"
assert len(_exec_calls) == 0
print("TEST 7 PASA — niche='geopolitics' excluido → route returns False")

# ── Restaurar ────────────────────────────────────────────────────────
config.PAPER_TRADING = _orig["PAPER_TRADING"]
config.REAL_TRADING_ENABLED = _orig["REAL_TRADING_ENABLED"]
config.REAL_TRADING_AGENT = _orig["REAL_TRADING_AGENT"]
risk_manager.can_trade = _orig["can_trade"]
if os.path.exists("/tmp/test_router.db"):
    os.remove("/tmp/test_router.db")

print("=" * 64)
print(" TODOS LOS 7 TESTS PASAN")
print("=" * 64)
