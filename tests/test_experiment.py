"""
test_experiment.py — Fase 2.6 (multi-agente)
==============================================
Aislado vía POLY_DB_PATH=/tmp/test_experiment.db.
"""
import os
import sys
from datetime import datetime, timedelta

TEST_DB = "/tmp/test_experiment.db"
os.environ["POLY_DB_PATH"] = TEST_DB
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import sqlite3
import database
import config

config.PAPER_TRADING_ENABLED = True
config.RISK_CHECK_LOW_BALANCE = False

# Mock requests para no tocar Polymarket
import requests as _requests
_orig_get = _requests.get
_clob_price = {"value": 0.5}
_market_state = {"closed": False, "tokens": []}

def _fake_get(url, **kw):
    class R:
        def __init__(self, data, status=200):
            self._data = data; self.status_code = status
        def json(self_): return self_._data
        def raise_for_status(self_): pass
    if "midpoint" in url:
        return R({"mid": _clob_price["value"]})
    if "/markets/" in url:
        return R(_market_state)
    if "value" in url:
        return R([{"value": 999.0}])
    return _orig_get(url, **kw)
_requests.get = _fake_get

database.init_db()
import paper_executor
import risk_manager

def reset():
    assert not database.is_prod_db()
    conn = database.get_connection()
    c = conn.cursor()
    c.execute("DELETE FROM paper_trades")
    c.execute("DELETE FROM detected_trades")
    c.execute("DELETE FROM risk_events")
    c.execute("DELETE FROM tuner_actions WHERE id > 0")
    conn.commit()
    conn.close()


def insert_signal(question="TEST_market"):
    conn = database.get_connection()
    c = conn.cursor()
    c.execute("""INSERT INTO detected_trades
        (wallet_address, market_id, market_question, side, size_usdc, entry_price,
         detected_at, confidence_score)
        VALUES ('0xT', 'CID_X', ?, 'BUY', 100, 0.5, datetime('now'), 0.7)""", (question,))
    dt_id = c.lastrowid
    conn.commit()
    conn.close()
    return dt_id


# ════════════════════════════════════════════════════════════════════
# TEST 1: una señal genera 3 paper_trades (uno por agente activo)
# ════════════════════════════════════════════════════════════════════
print("=" * 70)
print(" TEST 1 — una señal → N paper_trades (uno por agente activo)")
print("=" * 70)
reset()

# Verificar agentes seed
conn = database.get_connection()
c = conn.cursor()
c.execute("SELECT id, name, tp_multiplier, sl_multiplier, timeout_seconds, active FROM agents ORDER BY id")
agents = c.fetchall()
conn.close()
print("Agentes en BD:")
for a in agents:
    print(f"  {a[0]} · {a[1]} · TP={a[2]} SL={a[3]} timeout={a[4]}s active={a[5]}")
active_non_legacy = [a for a in agents if a[5] == 1 and a[0] != 'A_legacy']
assert len(active_non_legacy) == 3, f"Esperados 3 activos no-legacy, got {len(active_non_legacy)}"
print(f"✅ {len(active_non_legacy)} agentes activos no-legacy: {[a[0] for a in active_non_legacy]}")

# Disparar señal
_clob_price["value"] = 0.5
dt_id = insert_signal()
ids = paper_executor.evaluate_signal_for_paper_trade({
    "detected_trade_id": dt_id, "wallet": "0xWHALE", "market_id": "CID_X",
    "token_id": "TOK_X", "question": "TEST_market", "outcome_name": "YES",
    "side": "BUY", "niche": "esports", "insider_price": 0.5,
})
assert len(ids) == 3, f"Esperados 3 trades creados, got {len(ids)}"
print(f"✅ {len(ids)} paper_trades creados: {ids}")

# Verificar configs por agente
conn = database.get_connection()
c = conn.cursor()
c.execute("""SELECT agent_id, tp_multiplier, sl_multiplier, tp_target_price, sl_target_price,
                    timeout_seconds, is_hold_to_resolve
             FROM paper_trades WHERE detected_trade_id=? ORDER BY agent_id""", (dt_id,))
trades = c.fetchall()
conn.close()
expected = {
    "A": (1.8, 0.80,  7 * 86400, 0),
    "B": (1.5, 0.60,  7 * 86400, 0),
    "C": (0.0, 0.0,  30 * 86400, 1),  # almacenado como 0, flag is_h2r=1
}
for t in trades:
    ag, tp_mul, sl_mul, tp_tgt, sl_tgt, to_s, is_h2r = t
    exp_tp, exp_sl, exp_to, exp_h2r = expected[ag]
    assert tp_mul == exp_tp, f"agent {ag} tp_multiplier {tp_mul} != {exp_tp}"
    assert sl_mul == exp_sl, f"agent {ag} sl_multiplier {sl_mul} != {exp_sl}"
    assert to_s  == exp_to, f"agent {ag} timeout mismatch"
    assert is_h2r == exp_h2r, f"agent {ag} is_h2r {is_h2r} != {exp_h2r}"
    if is_h2r:
        print(f"  ✅ {ag}: hold-to-resolve, timeout={to_s//86400}d")
    else:
        print(f"  ✅ {ag}: TP={tp_tgt} SL={sl_tgt} timeout={to_s//86400}d")


# ════════════════════════════════════════════════════════════════════
# TEST 2: agente C NO se cierra por TP/SL aunque precio toque thresholds
# ════════════════════════════════════════════════════════════════════
print()
print("=" * 70)
print(" TEST 2 — agente C no cierra por TP/SL (solo RESOLVED o TIMEOUT)")
print("=" * 70)
reset()
_clob_price["value"] = 0.5
dt_id = insert_signal("market2")
paper_executor.evaluate_signal_for_paper_trade({
    "detected_trade_id": dt_id, "wallet": "0xT", "market_id": "CID_X",
    "token_id": "TOK_X", "question": "market2", "outcome_name": "YES",
    "side": "BUY", "niche": "esports", "insider_price": 0.5,
})
# Mover precio a 2x (TP teórico para A=0.9, B=0.75)
_clob_price["value"] = 2.0
stats = paper_executor.check_open_paper_trades()
print(f"Stats tras mover precio a 2.0: {stats}")

conn = database.get_connection()
c = conn.cursor()
c.execute("SELECT agent_id, status FROM paper_trades WHERE detected_trade_id=? ORDER BY agent_id", (dt_id,))
results = c.fetchall()
conn.close()
for ag, status in results:
    print(f"  {ag}: status={status}")
assert any(ag == "A" and status == "tp_hit" for ag, status in results), "A debería tp_hit"
assert any(ag == "B" and status == "tp_hit" for ag, status in results), "B debería tp_hit"
assert any(ag == "C" and status == "open" for ag, status in results), "C debe seguir OPEN (no TP/SL)"
print("✅ A y B cerraron por TP, C sigue OPEN (correcto)")

# Mover precio a 0.1 (SL teórico para A=0.4, B=0.3) — C tampoco debe cerrar
_clob_price["value"] = 0.1
stats = paper_executor.check_open_paper_trades()
conn = database.get_connection()
c = conn.cursor()
c.execute("SELECT status FROM paper_trades WHERE detected_trade_id=? AND agent_id='C'", (dt_id,))
status = c.fetchone()[0]
conn.close()
assert status == "open", f"C debe seguir OPEN tras precio bajo, got {status}"
print("✅ C sigue OPEN incluso con precio bajo (no SL aplicable)")


# ════════════════════════════════════════════════════════════════════
# TEST 3: veto risk_manager bloquea apertura para todos los agentes
# ════════════════════════════════════════════════════════════════════
print()
print("=" * 70)
print(" TEST 3 — veto risk_manager bloquea apertura para TODOS los agentes")
print("=" * 70)
reset()
risk_manager.manual_pause(reason="test_veto")
dt_id = insert_signal("market3")
ids = paper_executor.evaluate_signal_for_paper_trade({
    "detected_trade_id": dt_id, "wallet": "0xT", "market_id": "CID_X",
    "token_id": "TOK_X", "question": "market3", "outcome_name": "YES",
    "side": "BUY", "niche": "esports", "insider_price": 0.5,
})
assert ids == [], f"Esperado [] bajo veto, got {ids}"
risk_manager.manual_resume()
print("✅ Bajo veto: 0 trades creados (esperado)")


# ════════════════════════════════════════════════════════════════════
# TEST 4: tuner studio per-agent NO aplica nada (config_frozen=1 todos)
# ════════════════════════════════════════════════════════════════════
print()
print("=" * 70)
print(" TEST 4 — tuner per-agent sin aplicar (todos congelados)")
print("=" * 70)
reset()

# Insertar trades suficientes para que evaluate_tp/sl propongan algo.
# Necesitamos ≥20 trades cerrados por agente (config.AUTO_TUNER_MIN_TP=20).
# Para forzar propuesta TP "increase": tp_pct >= 60% en <4h.
def insert_closed_paper(agent_id, status, hours_held=2, pnl_net=2.0):
    conn = database.get_connection()
    c = conn.cursor()
    c.execute("""INSERT INTO detected_trades (wallet_address, market_id, market_question, side, size_usdc, entry_price, detected_at, confidence_score)
                 VALUES ('0xT','CID','q','BUY',100,0.5,datetime('now'),0.7)""")
    dt = c.lastrowid
    entry_at = (datetime.utcnow() - timedelta(hours=hours_held)).isoformat()
    exit_at = datetime.utcnow().isoformat()
    c.execute("""INSERT INTO paper_trades
        (detected_trade_id, market_id, token_id, market_question, niche, side,
         entry_at, entry_price, entry_size_usdc, entry_shares,
         tp_target_price, sl_target_price, tp_multiplier, sl_multiplier,
         status, exit_at, exit_price, exit_reason, realized_pnl_net,
         realized_pnl_gross, fees_estimated, agent_id, timeout_seconds, is_hold_to_resolve)
        VALUES (?, 'CID','TOK','q','esports','BUY', ?, 0.5, 100, 200,
                0.9, 0.4, 1.8, 0.8, ?, ?, 0.7, ?, ?, ?, 0.3, ?, ?, 0)""",
        (dt, entry_at, status, exit_at, status, pnl_net, pnl_net + 0.3, agent_id, 7 * 86400))
    conn.commit()
    conn.close()

for ag in ("A", "B"):
    for _ in range(15):
        insert_closed_paper(ag, "tp_hit", pnl_net=2.0)
    for _ in range(5):
        insert_closed_paper(ag, "sl_hit", pnl_net=-3.0)

# Reset cooldown del tuner_state para permitir propuesta
conn = database.get_connection()
c = conn.cursor()
c.execute("UPDATE tuner_state SET cooldown_until=NULL")
conn.commit()
conn.close()

import auto_tuner
config.AUTO_TUNER_DRY_RUN = True
config.AUTO_TUNER_ENABLED = True
summary = auto_tuner.run_one_cycle()
print(f"summary: {summary}")
assert summary.get("experiment_mode") is True, "Debe estar en modo experimento"
assert summary.get("applied", 0) == 0, "applied debe ser 0 (todos congelados)"

# Verificar que las propuestas se persistieron con agent_id distinto
conn = database.get_connection()
c = conn.cursor()
c.execute("""SELECT DISTINCT agent_id FROM tuner_actions
             WHERE applied=0 AND dry_run=1 ORDER BY agent_id""")
agent_ids = [r[0] for r in c.fetchall()]
c.execute("SELECT COUNT(*) FROM tuner_actions WHERE applied=0 AND dry_run=1")
n_props = c.fetchone()[0]
conn.close()
print(f"✅ {n_props} propuestas pendientes, distribución por agente: {agent_ids}")
assert n_props > 0, "Esperaba al menos una propuesta"


# ════════════════════════════════════════════════════════════════════
# TEST 5: paper_trades legacy NO aparecen en data.experiment
# ════════════════════════════════════════════════════════════════════
print()
print("=" * 70)
print(" TEST 5 — paper_trades A_legacy excluidos de data.experiment")
print("=" * 70)
reset()

# Insertar 1 paper de cada agente + 5 legacy
def insert_paper(agent_id):
    conn = database.get_connection()
    c = conn.cursor()
    c.execute("""INSERT INTO detected_trades (wallet_address, market_id, market_question, side, size_usdc, entry_price, detected_at, confidence_score)
                 VALUES ('0xT','CID','q','BUY',100,0.5,datetime('now'),0.7)""")
    dt = c.lastrowid
    c.execute("""INSERT INTO paper_trades
        (detected_trade_id, market_id, token_id, market_question, niche, side,
         entry_at, entry_price, entry_size_usdc, entry_shares,
         tp_target_price, sl_target_price, tp_multiplier, sl_multiplier,
         status, agent_id, timeout_seconds, fees_estimated, is_hold_to_resolve)
        VALUES (?,'CID','TOK','q','esports','BUY', datetime('now'), 0.5, 100, 200,
                0.9, 0.4, 1.8, 0.8, 'open', ?, 604800, 0.3, 0)""", (dt, agent_id))
    conn.commit()
    conn.close()

for ag in ("A", "B", "C", "A_legacy", "A_legacy", "A_legacy", "A_legacy", "A_legacy"):
    insert_paper(ag)

import data_exporter
exp = data_exporter._build_experiment_block()
agent_ids_in_block = {a["id"] for a in exp["agents"]}
assert "A_legacy" not in agent_ids_in_block, "A_legacy NO debe aparecer"
assert agent_ids_in_block == {"A", "B", "C"}, f"Esperaba {{A,B,C}}, got {agent_ids_in_block}"
# Cada agente debe tener exactly 1 trade abierto (el legacy NO cuenta)
for a in exp["agents"]:
    assert a["open"] == 1, f"agent {a['id']} open={a['open']} (esperado 1)"
print(f"✅ data.experiment.agents = {agent_ids_in_block}, A_legacy excluido")

# Cleanup
reset()
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)

print()
print("=" * 70)
print(" ✅ TODOS LOS 5 TESTS PASAN — Fase 2.6 verificada")
print("=" * 70)
