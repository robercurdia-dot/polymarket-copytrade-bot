"""
test_executor.py — Fase 3.1
Tests de slippage threshold, retries con backoff, sanitización de logs.
Aislado vía POLY_DB_PATH.
"""
import os
import sys

os.environ["POLY_DB_PATH"] = "/tmp/test_executor.db"
if os.path.exists("/tmp/test_executor.db"):
    os.remove("/tmp/test_executor.db")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import database
database.init_db()
import config
import executor
import risk_manager

# Estado base para tests de slippage: paper mode (no toca CLOB en la rama real),
# pero el check de slippage corre ANTES de la bifurcación paper/real.
_orig = {
    "PAPER_TRADING": config.PAPER_TRADING,
    "REAL_TRADING_ENABLED": config.REAL_TRADING_ENABLED,
    "MAX_SLIPPAGE_ALLOWED": executor.MAX_SLIPPAGE_ALLOWED,
    "can_trade": risk_manager.can_trade,
    "get_current_price": executor.get_current_price,
    "_with_timeout": executor._with_timeout,
}

# can_trade siempre OK, _with_timeout directo
risk_manager.can_trade = lambda size, agent_id=None: (True, "ok")
executor._with_timeout = lambda fn, *a, **kw: fn(*a, **{k: v for k, v in kw.items() if k != "timeout"})

print("=" * 64)
print(" TEST executor — slippage, retries, sanitizer")
print("=" * 64)

# ── TEST 1: slippage por encima del threshold → blocked ──────────────
# threshold 0.05; entry 0.50, precio 0.60 → slippage = 0.10/0.50 = 20%
config.PAPER_TRADING = True
config.REAL_TRADING_ENABLED = False
executor.MAX_SLIPPAGE_ALLOWED = 0.05
executor.get_current_price = lambda tid: 0.60    # slippage = 20%
r = executor.execute_trade(1, "CID", "TOK", "BUY", 3, 0.50)
assert r["status"] == "blocked", f"esperado blocked, got {r}"
assert r["reason"] == "slippage_too_high"
print(f"TEST 1 PASA — slippage 20% > 5% → blocked (reason={r['reason']})")

# ── TEST 2: slippage justo por encima del borde → blocked ────────────
# entry 0.50, precio 0.5255 → slippage = 5.1% (claramente > 5%)
executor.get_current_price = lambda tid: 0.5255
r = executor.execute_trade(2, "CID", "TOK", "BUY", 3, 0.50)
assert r["status"] == "blocked", f"slippage 5.1% debe bloquear, got {r}"
print(f"TEST 2 PASA — slippage 5.1% (borde superior) → blocked")

# ── TEST 3: slippage por debajo del threshold → procede ──────────────
executor.get_current_price = lambda tid: 0.51    # slippage = 0.01/0.50 = 2%
r = executor.execute_trade(3, "CID", "TOK", "BUY", 3, 0.50)
assert r["status"] == "paper_ok", f"slippage bajo debe proceder, got {r}"
print(f"TEST 3 PASA — slippage 2% < 5% → procede ({r['status']})")

# ── TEST 4: retry en error transitorio → eventualmente OK ────────────
calls = {"n": 0}
def _flaky():
    calls["n"] += 1
    if calls["n"] < 3:
        raise ConnectionError("transient boom")
    return "OK_FINAL"
result = executor._retry_with_backoff(_flaky, max_retries=3, base_delay=0.01)
assert result == "OK_FINAL", f"esperado OK_FINAL, got {result}"
assert calls["n"] == 3, f"esperado 3 intentos, got {calls['n']}"
print(f"TEST 4 PASA — retry en error transitorio → OK tras {calls['n']} intentos")

# ── TEST 4b: error NO transitorio → no reintenta ─────────────────────
calls_nt = {"n": 0}
def _hard_fail():
    calls_nt["n"] += 1
    raise ValueError("no transitorio")
try:
    executor._retry_with_backoff(_hard_fail, max_retries=3, base_delay=0.01)
    assert False, "debió propagar ValueError"
except ValueError:
    pass
assert calls_nt["n"] == 1, f"error no transitorio NO debe reintentar, got {calls_nt['n']}"
print(f"TEST 4b PASA — error no transitorio → no reintenta ({calls_nt['n']} intento)")

# ── TEST 5: sanitizer redacta private key y secrets ──────────────────
pk = "0x" + "a" * 64
msg = f"Error con key={pk} y secret=abc123 y passphrase=xyz"
sanitized = executor._sanitize(msg)
assert pk not in sanitized, "private key NO debe aparecer en logs"
assert "abc123" not in sanitized, "secret NO debe aparecer"
assert "xyz" not in sanitized, "passphrase NO debe aparecer"
assert "[REDACTED]" in sanitized
print(f"TEST 5 PASA — sanitizer redacta credenciales → '{sanitized}'")

# ── TEST 6: _is_transient clasifica correctamente ────────────────────
from py_clob_client.exceptions import PolyApiException
assert executor._is_transient(ConnectionError("x")) is True
assert executor._is_transient(TimeoutError("x")) is True
assert executor._is_transient(ValueError("x")) is False
# PolyApiException con status_code transitorio
class _FakePoly(PolyApiException):
    def __init__(self, code):
        self.status_code = code
        self.error_msg = ""
assert executor._is_transient(_FakePoly(503)) is True
assert executor._is_transient(_FakePoly(400)) is False
print("TEST 6 PASA — _is_transient clasifica transitorios vs permanentes")

# ── Restaurar y limpiar ──────────────────────────────────────────────
config.PAPER_TRADING = _orig["PAPER_TRADING"]
config.REAL_TRADING_ENABLED = _orig["REAL_TRADING_ENABLED"]
executor.MAX_SLIPPAGE_ALLOWED = _orig["MAX_SLIPPAGE_ALLOWED"]
risk_manager.can_trade = _orig["can_trade"]
executor.get_current_price = _orig["get_current_price"]
executor._with_timeout = _orig["_with_timeout"]
if os.path.exists("/tmp/test_executor.db"):
    os.remove("/tmp/test_executor.db")

print("=" * 64)
print(" TODOS LOS 6 TESTS PASAN")
print("=" * 64)
