"""Test estructural L21 — inmutabilidad de estados terminales.
Cubre las 4 race conditions identificadas en el audit 2026-05-24."""
import os, sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TEST_DB = "/tmp/test_terminal_guard.db"

def setup():
    if os.path.exists(TEST_DB): os.unlink(TEST_DB)
    os.environ["POLY_DB_PATH"] = TEST_DB
    con = sqlite3.connect(TEST_DB)
    con.execute("""CREATE TABLE bot_trades (
        id INTEGER PRIMARY KEY, detected_trade_id INTEGER, market_id TEXT,
        side TEXT, size_usdc REAL, entry_price REAL, executed_at TEXT,
        paper_trade INTEGER DEFAULT 0, outcome TEXT DEFAULT 'open',
        pnl_usdc REAL, token_id TEXT, status TEXT, size_filled REAL,
        fees REAL, order_id TEXT, client_order_id TEXT, error TEXT)""")
    con.commit(); con.close()


def test_terminal_immutability():
    setup()
    import importlib, database
    importlib.reload(database)
    con = sqlite3.connect(TEST_DB)
    for i, st in enumerate(['open', 'closed', 'rejected', 'failed'], 1):
        con.execute("INSERT INTO bot_trades (id, status, paper_trade, size_filled, entry_price) VALUES (?, ?, 0, 5, 0.5)", (i, st))
    con.commit(); con.close()

    # open → closed permitido
    assert database.update_trade_status(1, 'closed') is True
    # closed → open RECHAZADO (race condition L01/L07/L18)
    assert database.update_trade_status(2, 'open') is False
    # rejected → open RECHAZADO (race condition L16)
    assert database.update_trade_status(3, 'open') is False
    # failed → closing RECHAZADO
    assert database.update_trade_status(4, 'closing') is False
    # force override
    assert database.update_trade_status(2, 'open', force=True) is True
    # update_trade_filled también respeta guard
    database.update_trade_status(2, 'closed')
    assert database.update_trade_filled(2, 0.4, 5, 0, "x", status='open') is False
    # pending → open permitido (no terminal)
    con = sqlite3.connect(TEST_DB)
    con.execute("INSERT INTO bot_trades (id, status, paper_trade) VALUES (5, 'pending', 0)")
    con.commit(); con.close()
    assert database.update_trade_filled(5, 0.5, 5, 0, "x", status='open') is True
    os.unlink(TEST_DB)
    print("✓ test_terminal_immutability 7/7")


if __name__ == "__main__":
    test_terminal_immutability()
