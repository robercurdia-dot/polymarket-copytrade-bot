"""
test_isolation.py — Verifica que tests usan BD aislada (post-incidente 2026-05-04).

CRÍTICO: setear POLY_DB_PATH ANTES de importar database, para que el módulo
lea la env var en su carga inicial.

Ejecutar:
    POLY_DB_PATH=/tmp/test_aislado.db python tests/test_isolation.py
o:
    python tests/test_isolation.py    (auto-setea env var)
"""
import os
import sys

# Path absoluto para que se pueda ejecutar desde cualquier cwd
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# CRÍTICO: setear ANTES de importar database
TEST_DB = "/tmp/test_aislado.db"
os.environ["POLY_DB_PATH"] = TEST_DB

# Limpiar BD de tests previa si existe
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)

sys.path.insert(0, ROOT)

# Snapshot del .db de prod ANTES de cualquier operación de tests
PROD_DB = os.path.join(ROOT, "polymarket_insider.db")
prod_size_before = os.path.getsize(PROD_DB) if os.path.exists(PROD_DB) else 0
prod_mtime_before = os.path.getmtime(PROD_DB) if os.path.exists(PROD_DB) else 0

# Conteos de prod ANTES (para verificar que no se tocan)
import sqlite3
prod_conn = sqlite3.connect(PROD_DB)
prod_pt_before    = prod_conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
prod_dt_before    = prod_conn.execute("SELECT COUNT(*) FROM detected_trades").fetchone()[0]
prod_conn.close()

import database

print("=" * 60)
print(" TEST DE AISLAMIENTO DE TESTS")
print("=" * 60)
print(f"POLY_DB_PATH activa: {os.environ.get('POLY_DB_PATH')}")
print(f"database.DB_PATH:    {database.DB_PATH}")
print(f"is_prod_db:          {database.is_prod_db()}")
assert database.DB_PATH == TEST_DB
assert not database.is_prod_db()
print("✅ database lee POLY_DB_PATH correctamente")

# Inicializar BD de test (crea esquema en /tmp/test_aislado.db, NO en prod)
database.init_db()
print(f"✅ init_db() creó esquema en {TEST_DB}")

# Insertar datos en BD de test
conn = database.get_connection()
c = conn.cursor()
c.execute("""INSERT INTO paper_trades
    (detected_trade_id, market_id, market_question, side,
     entry_at, entry_price, entry_size_usdc, entry_shares,
     tp_target_price, sl_target_price, tp_multiplier, sl_multiplier, status)
    VALUES (1, 'TEST_CID', 'TEST_market', 'BUY',
            datetime('now'), 0.5, 10, 20, 0.7, 0.325, 1.4, 0.65, 'open')""")
conn.commit()
n_test = c.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
conn.close()
print(f"✅ Inserté 1 paper_trade en test BD; cuenta = {n_test}")

# Operación destructiva permitida en test BD
database.unsafe_truncate_table("paper_trades")
conn = database.get_connection()
c = conn.cursor()
n_test_after = c.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
conn.close()
assert n_test_after == 0
print(f"✅ unsafe_truncate_table() funciona en test BD (cuenta tras truncate: {n_test_after})")

# VERIFICACIÓN CRÍTICA: la BD de PRODUCCIÓN no fue tocada
prod_conn = sqlite3.connect(PROD_DB)
prod_pt_after = prod_conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
prod_dt_after = prod_conn.execute("SELECT COUNT(*) FROM detected_trades").fetchone()[0]
prod_conn.close()
prod_size_after = os.path.getsize(PROD_DB)

print()
print("──── Verificación NO-TOQUE de producción ────")
print(f"paper_trades prod ANTES:    {prod_pt_before}")
print(f"paper_trades prod DESPUÉS:  {prod_pt_after}")
print(f"detected_trades prod ANTES: {prod_dt_before}")
print(f"detected_trades prod DESPUÉS:{prod_dt_after}")
assert prod_pt_before == prod_pt_after, f"❌ paper_trades de prod cambió: {prod_pt_before}→{prod_pt_after}"
assert prod_dt_before == prod_dt_after, f"❌ detected_trades de prod cambió"
print("✅ Producción intacta: ningún registro modificado")

# Limpieza
os.remove(TEST_DB)
print(f"\n🧹 Test BD eliminada: {TEST_DB}")
print()
print("=" * 60)
print(" ✅ TEST DE AISLAMIENTO PASA")
print("=" * 60)
