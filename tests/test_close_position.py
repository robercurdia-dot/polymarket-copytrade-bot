"""
test_close_position.py — Fase 3.1
Tests del refactor de close_position con orderbook real. Aislado vía POLY_DB_PATH.
"""
import os
import sys

os.environ["POLY_DB_PATH"] = "/tmp/test_closepos.db"
if os.path.exists("/tmp/test_closepos.db"):
    os.remove("/tmp/test_closepos.db")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import database
database.init_db()
import executor
import config


# ── Helpers de mock ──────────────────────────────────────────────────

class FakeOrderSummary:
    def __init__(self, price, size):
        self.price = str(price)
        self.size = str(size)

class FakeOrderBook:
    def __init__(self, bids, asks):
        self.bids = [FakeOrderSummary(p, s) for p, s in bids]
        self.asks = [FakeOrderSummary(p, s) for p, s in asks]

class FakeClient:
    """Cliente CLOB simulado. Configurable por test."""
    def __init__(self, orderbook=None, order_response=None, raise_exc=None):
        self._orderbook = orderbook
        self._order_response = order_response
        self._raise_exc = raise_exc
        self.orders_sent = []
    def get_order_book(self, token_id):
        if self._raise_exc:
            raise self._raise_exc
        return self._orderbook
    def create_and_post_order(self, order_args):
        self.orders_sent.append(order_args)
        if self._raise_exc:
            raise self._raise_exc
        return self._order_response


def _insert_real_open_trade():
    """Inserta un bot_trade real abierto y devuelve su id."""
    conn = database.get_connection()
    c = conn.cursor()
    c.execute("""INSERT INTO bot_trades
        (detected_trade_id, market_id, token_id, side, size_usdc, size_filled,
         paper_trade, status, outcome)
        VALUES (1, 'CID', 'TOK', 'BUY', 3, 3, 0, 'open', 'open')""")
    bt_id = c.lastrowid
    conn.commit()
    conn.close()
    return bt_id


def _status_of(bt_id):
    conn = database.get_connection()
    c = conn.cursor()
    c.execute("SELECT status, error FROM bot_trades WHERE id=?", (bt_id,))
    row = c.fetchone()
    conn.close()
    return row


_orig_get_client = executor.get_client
_orig_with_timeout = executor._with_timeout

# _with_timeout en tests: ejecutar directo sin thread (más simple y determinista)
executor._with_timeout = lambda fn, *a, **kw: fn(*a, **{k: v for k, v in kw.items() if k != "timeout"})

print("=" * 64)
print(" TEST close_position")
print("=" * 64)

# ── TEST 1: orderbook con bids → cierre OK ───────────────────────────
bt = _insert_real_open_trade()
fake = FakeClient(
    orderbook=FakeOrderBook(bids=[(0.50, 100), (0.48, 50)], asks=[(0.52, 80)]),
    order_response={"success": True, "status": "matched", "orderID": "0xABC",
                    "errorMsg": "", "transactionsHashes": ["0xdef"]},
)
executor.get_client = lambda force_refresh=False: fake
r = executor.close_position(bt)
assert r["status"] == "ok", f"esperado ok, got {r}"
assert r["close_price"] == 0.50, f"debe vender al best_bid 0.50, got {r['close_price']}"
assert _status_of(bt)[0] == "closed"
print("TEST 1 PASA — orderbook con bids → cierre OK al best_bid (0.50)")

# ── TEST 2: orderbook sin bids → close_failed ────────────────────────
bt = _insert_real_open_trade()
fake = FakeClient(orderbook=FakeOrderBook(bids=[], asks=[(0.52, 80)]))
executor.get_client = lambda force_refresh=False: fake
r = executor.close_position(bt)
assert r["status"] == "close_failed", f"esperado close_failed, got {r}"
assert r["reason"] == "no_bids"
assert _status_of(bt)[0] == "close_failed"
assert len(fake.orders_sent) == 0, "no debe mandar orden si no hay bids"
print("TEST 2 PASA — orderbook sin bids → close_failed (no manda orden)")

# ── TEST 3: spread > 10% → close_failed ──────────────────────────────
bt = _insert_real_open_trade()
# best_bid=0.50, best_ask=0.60 → spread = 0.10/0.50 = 20%
fake = FakeClient(orderbook=FakeOrderBook(bids=[(0.50, 100)], asks=[(0.60, 80)]))
executor.get_client = lambda force_refresh=False: fake
r = executor.close_position(bt)
assert r["status"] == "close_failed", f"esperado close_failed, got {r}"
assert r["reason"] == "spread_too_wide"
assert _status_of(bt)[0] == "close_failed"
assert len(fake.orders_sent) == 0, "no debe mandar orden si spread excesivo"
print("TEST 3 PASA — spread 20% > 10% → close_failed (no manda orden)")

# ── TEST 4: error leyendo orderbook → close_failed ───────────────────
bt = _insert_real_open_trade()
fake = FakeClient(raise_exc=ConnectionError("boom"))
executor.get_client = lambda force_refresh=False: fake
r = executor.close_position(bt)
assert r["status"] == "close_failed", f"esperado close_failed, got {r}"
assert r["reason"] == "orderbook_error"
assert _status_of(bt)[0] == "close_failed"
print("TEST 4 PASA — error leyendo orderbook → close_failed con retry agotado")

# ── Restaurar y limpiar ──────────────────────────────────────────────
executor.get_client = _orig_get_client
executor._with_timeout = _orig_with_timeout
if os.path.exists("/tmp/test_closepos.db"):
    os.remove("/tmp/test_closepos.db")

print("=" * 64)
print(" TODOS LOS 4 TESTS PASAN")
print("=" * 64)
