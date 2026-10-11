"""
test_risk_manager.py — Fase 2.5
================================
Verifica los 4 circuit breakers + integración con paper executor.
Aislado vía POLY_DB_PATH=/tmp/test_risk.db (NUNCA toca producción).
"""
import os
import sys

# CRÍTICO: aislar BD ANTES de importar
TEST_DB = "/tmp/test_risk.db"
os.environ["POLY_DB_PATH"] = TEST_DB
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import sqlite3
from datetime import datetime, timedelta

import database
import config
import risk_manager

database.init_db()

# Mock GLOBAL de requests.get para no tocar Polymarket real durante tests.
# Default: balance alto, never trigger LOW_BALANCE. Tests específicos pueden override.
import requests as _requests
_orig_get = _requests.get
_balance_response = [{"value": 999.0}]

def _fake_get(url, **kw):
    if "data-api.polymarket.com/value" in url:
        class R:
            status_code = 200
            def json(self_): return _balance_response
        return R()
    return _orig_get(url, **kw)

_requests.get = _fake_get


def reset():
    """Limpia state de tests anteriores en la BD aislada."""
    assert not database.is_prod_db(), "BD activa NO es de test"
    conn = database.get_connection()
    c = conn.cursor()
    c.execute("DELETE FROM risk_events")
    c.execute("DELETE FROM paper_trades")
    conn.commit()
    conn.close()
    risk_manager._api_error_log.clear()


def insert_paper_closed(pnl_net, exit_at_iso=None):
    """Inserta un paper_trade ya cerrado con el pnl_net especificado."""
    conn = database.get_connection()
    c = conn.cursor()
    c.execute("""INSERT INTO paper_trades
        (detected_trade_id, market_id, market_question, side,
         entry_at, entry_price, entry_size_usdc, entry_shares,
         tp_target_price, sl_target_price, tp_multiplier, sl_multiplier,
         status, exit_at, exit_price, exit_reason,
         realized_pnl_gross, realized_pnl_net, fees_estimated)
        VALUES (1, 'CID', 'q', 'BUY',
                datetime('now', '-1 hour'), 0.5, 5, 10,
                0.7, 0.325, 1.4, 0.65,
                'sl_hit', ?, 0.4, 'sl_hit',
                ?, ?, 0.30)""",
        (exit_at_iso or datetime.utcnow().isoformat(), pnl_net + 0.3, pnl_net))
    conn.commit()
    conn.close()


# ════════════════════════════════════════════════════════════════
# TEST 1 — DRAWDOWN: PnL <= -(TRADE_SIZE × MULTIPLIER) → veto
# ════════════════════════════════════════════════════════════════
print("=" * 70)
print(" TEST 1 — DRAWDOWN circuit breaker")
print("=" * 70)
reset()

# Configurar: TRADE_SIZE=5, MULTIPLIER=10 → threshold = -$50
config.PAPER_SIZE_USDC = 5.0
config.DRAWDOWN_MULTIPLIER = 10.0

# Insertar paper_trade que sume -$51 (cruza el threshold)
insert_paper_closed(-51.0)
veto, reason, cd = risk_manager.has_veto()
assert veto and reason == "DRAWDOWN", f"Esperado DRAWDOWN, got: ({veto}, {reason})"
assert cd is None, f"DRAWDOWN debe ser permanente, got cd={cd}"
print(f"✅ Drawdown veto activo: pnl=-51, threshold=-50, reason={reason}")

# Verificar paper_executor no abre nuevos durante veto
import paper_executor
result = paper_executor.evaluate_signal_for_paper_trade({
    "detected_trade_id": 999, "wallet": "0xT", "market_id": "C",
    "token_id": "T", "question": "Q", "outcome_name": "Y",
    "side": "BUY", "niche": "esports", "insider_price": 0.5,
})
assert result is None, f"paper_executor abrió trade pese a veto: {result}"
print(f"✅ paper_executor.evaluate_signal_for_paper_trade() devolvió None bajo veto")

# Verificar que el evento se persistió
conn = database.get_connection()
c = conn.cursor()
c.execute("SELECT event_type, trigger_value, threshold, veto_duration_seconds, resolved_at FROM risk_events WHERE event_type='DRAWDOWN'")
row = c.fetchone()
conn.close()
assert row[0] == "DRAWDOWN" and row[3] == 0 and row[4] is None
print(f"✅ risk_events: type=DRAWDOWN, trigger={row[1]}, threshold={row[2]}, permanente, no resuelto")


# ════════════════════════════════════════════════════════════════
# TEST 2 — MANUAL_PAUSE / RESUME
# ════════════════════════════════════════════════════════════════
print()
print("=" * 70)
print(" TEST 2 — MANUAL_PAUSE + /resume")
print("=" * 70)
reset()
config.PAPER_SIZE_USDC = 100.0  # threshold = -$1000, sin drawdown

risk_manager.manual_pause(reason="test_pause")
veto, reason, cd = risk_manager.has_veto()
assert veto and reason == "MANUAL_PAUSE"
print(f"✅ Tras /pause: veto={veto}, reason={reason}")

result = risk_manager.manual_resume()
assert result["manual_released"], "manual no liberado"
assert not result["other_vetoes"], f"other_vetoes inesperados: {result['other_vetoes']}"
veto, reason, cd = risk_manager.has_veto()
assert not veto, f"Tras /resume sigue veto: {reason}"
print(f"✅ Tras /resume: sin veto, manual_released=True")

# /resume con drawdown activo simultáneo: debe reportar otro veto
risk_manager.manual_pause(reason="test")
config.PAPER_SIZE_USDC = 5.0
config.DRAWDOWN_MULTIPLIER = 10.0
insert_paper_closed(-60.0)
risk_manager.has_veto()  # trigger drawdown
result = risk_manager.manual_resume()
assert result["manual_released"]
assert any(v["type"] == "DRAWDOWN" for v in result["other_vetoes"])
print(f"✅ /resume con drawdown reporta correctamente: other_vetoes={[v['type'] for v in result['other_vetoes']]}")


# ════════════════════════════════════════════════════════════════
# TEST 3 — API_ERRORS: 11 errores en 1h → veto 1h, auto-libera al expirar
# ════════════════════════════════════════════════════════════════
print()
print("=" * 70)
print(" TEST 3 — API_ERRORS (1h temporal)")
print("=" * 70)
reset()
config.PAPER_SIZE_USDC = 100.0  # sin drawdown
config.MAX_API_ERRORS_HOUR = 10

for _ in range(11):
    risk_manager.record_api_error()
veto, reason, cd = risk_manager.has_veto()
assert veto and reason == "API_ERRORS"
assert cd and cd > 0 and cd <= 3600, f"countdown inválido: {cd}"
print(f"✅ Tras 11 errores: veto=API_ERRORS, countdown={cd}s")

# Forzar expiración: backdate timestamp del evento >1h
conn = database.get_connection()
c = conn.cursor()
old = (datetime.utcnow() - timedelta(hours=2)).isoformat()
c.execute("UPDATE risk_events SET timestamp=? WHERE event_type='API_ERRORS' AND resolved_at IS NULL", (old,))
conn.commit()
conn.close()
veto, reason, cd = risk_manager.has_veto()
assert not veto, f"Debería haber auto-expirado, sigue veto: {reason}"
# Confirmar que se auto-resolvió
conn = database.get_connection()
c = conn.cursor()
c.execute("SELECT resolved_at FROM risk_events WHERE event_type='API_ERRORS' ORDER BY id DESC LIMIT 1")
row = c.fetchone()
conn.close()
assert row[0] is not None, "auto-resolución no marcó resolved_at"
print(f"✅ Veto API_ERRORS auto-libera tras expirar: resolved_at={row[0]}")


# ════════════════════════════════════════════════════════════════
# TEST 4 — LOW_BALANCE: balance < min → veto permanente
# ════════════════════════════════════════════════════════════════
print()
print("=" * 70)
print(" TEST 4 — LOW_BALANCE (mock balance=4 < min=5)")
print("=" * 70)
reset()
config.PAPER_SIZE_USDC = 100.0
config.MIN_WALLET_BALANCE_USDC = 5.0
# RISK_CHECK_LOW_BALANCE=True activa el check (Fase 3a).
config.RISK_CHECK_LOW_BALANCE = True

# Override mock para devolver balance=4 (< min=5)
_balance_response = [{"value": 4.0}]

veto, reason, cd = risk_manager.has_veto()
assert veto and reason == "LOW_BALANCE"
assert cd is None, f"LOW_BALANCE debe ser permanente"
print(f"✅ Con RISK_CHECK_LOW_BALANCE=True y balance=$4: veto=LOW_BALANCE permanente")

# Verificar que con flag desactivada (modo paper / pre-Fase 3a) se omite
config.RISK_CHECK_LOW_BALANCE = False
reset()
veto, reason, cd = risk_manager.has_veto()
assert not veto, f"Con RISK_CHECK_LOW_BALANCE=False no debe activar, got: {reason}"
print(f"✅ Con RISK_CHECK_LOW_BALANCE=False: SIN veto (correcto, pre-Fase 3a)")

# Restaurar default
_balance_response = [{"value": 999.0}]


# ════════════════════════════════════════════════════════════════
# Cleanup final
# ════════════════════════════════════════════════════════════════
reset()
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
print()
print("=" * 70)
print(" ✅ TODOS LOS 4 TESTS PASAN — Fase 2.5 verificada")
print("=" * 70)
