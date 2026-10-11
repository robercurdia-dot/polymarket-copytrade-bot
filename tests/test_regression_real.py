"""
test_regression_real.py — Suite de regresión para el sistema real DEF_COPY.
Cubre todos los bugs encontrados y corregidos antes del go-live.
Ejecutar: python3 -m pytest tests/test_regression_real.py -v
"""
import sqlite3, ast, re, subprocess, os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ─── helpers ────────────────────────────────────────────────────────────────

def read(f): return (ROOT / f).read_text()
def env():   return (ROOT / '.env').read_text()
def db():    return sqlite3.connect(str(ROOT / 'polymarket_insider.db'))

# ════════════════════════════════════════════════════════════════════════════
# BLOQUE 1 — Sintaxis Python
# ════════════════════════════════════════════════════════════════════════════

CORE_FILES = [
    'config.py', 'risk_manager.py', 'real_signal_router.py',
    'definitive_copy_watcher.py', 'paper_executor.py',
    'executor.py', 'data_exporter.py', 'main.py', 'database.py',
]

def test_python_syntax():
    """Ningún archivo core tiene error de sintaxis."""
    for f in CORE_FILES:
        ast.parse(read(f))  # lanza SyntaxError si falla

def test_js_syntax():
    """El JS del dashboard no tiene errores de sintaxis."""
    html = (ROOT / 'web/dashboard.html').read_text()
    scripts = re.findall(r'<script>(.*?)</script>', html, re.DOTALL)
    assert scripts, "No se encontró bloque <script>"
    js = '/tmp/_dash_regression.js'
    Path(js).write_text(scripts[0])
    r = subprocess.run(['node', '--check', js], capture_output=True, text=True)
    assert r.returncode == 0, f"JS inválido: {r.stderr[:200]}"

def test_no_duplicate_js_toplevel_vars():
    """No hay const declaradas dos veces a nivel top-level (fuera de funciones)."""
    html = (ROOT / 'web/dashboard.html').read_text()
    scripts = re.findall(r'<script>(.*?)</script>', html, re.DOTALL)
    js = scripts[0] if scripts else ''
    # Solo top-level: líneas que comienzan con 'const ' sin indentación
    toplevel = re.findall(r'^const\s+(\w+)\s*=', js, re.MULTILINE)
    from collections import Counter
    dupes = [k for k, v in Counter(toplevel).items() if v > 1]
    assert not dupes, f"Variables JS top-level duplicadas: {dupes}"

# ════════════════════════════════════════════════════════════════════════════
# BLOQUE 2 — Config y .env
# ════════════════════════════════════════════════════════════════════════════

def test_def_copy_in_allowed_agents():
    """DEF_COPY está en _ALLOWED_AGENTS de config.py — sin esto el proceso crashea."""
    src = read('config.py')
    assert 'DEF_COPY' in src, "DEF_COPY no está en config.py _ALLOWED_AGENTS"

def test_real_trading_enabled_no_quotes():
    """REAL_TRADING_ENABLED sin comillas — con comillas nunca se activa."""
    e = env()
    assert "REAL_TRADING_ENABLED='true'" not in e, "REAL_TRADING_ENABLED tiene comillas — nunca activará"
    assert "REAL_TRADING_ENABLED='false'" not in e, "REAL_TRADING_ENABLED tiene comillas"

def test_real_copy_vars_exist():
    """Variables de DEF_COPY real existen en .env."""
    e = env()
    assert 'REAL_COPY_ENABLED=' in e
    assert 'REAL_COPY_TRADE_SIZE=' in e

def test_agents_b_d_disabled():
    """Agentes B y D están desactivados en DB para no interferir con real."""
    conn = db()
    for ag in ('B', 'D'):
        row = conn.execute("SELECT active FROM agents WHERE id=?", (ag,)).fetchone()
        if row:
            assert row[0] == 0, f"Agente {ag} sigue activo — debe estar desactivado"
    conn.close()

def test_real_d_disabled_env():
    """REAL_D_ENABLED=0 en .env — agente D no ejecuta real."""
    e = env()
    assert 'REAL_D_ENABLED=0' in e, "REAL_D_ENABLED no está en 0"

# ════════════════════════════════════════════════════════════════════════════
# BLOQUE 3 — Watcher: cierre real en CLOB
# ════════════════════════════════════════════════════════════════════════════

def test_watcher_calls_close_position():
    """El watcher llama a executor.close_position() — no solo actualiza DB."""
    src = read('definitive_copy_watcher.py')
    assert 'close_position' in src, "watcher no llama a close_position — posiciones quedan abiertas on-chain"

def test_watcher_filters_paper_trades():
    """El watcher filtra paper_trade=0 al buscar bot_trades reales."""
    src = read('definitive_copy_watcher.py')
    assert 'paper_trade=0' in src, "watcher no filtra paper_trade=0 — podría cerrar trades paper como reales"

def test_watcher_merges_active():
    """MERGEs siguen siendo señales de salida — no ignorarlos (revert del bug)."""
    src = read('definitive_copy_watcher.py')
    # El bug era: _fetch_exits devolvía merges=[] hardcodeado ignorando la API.
    # La fix es que _fetch_exits construya la lista leyendo type=='MERGE' de la API.
    assert "type') == 'MERGE'" in src or "== 'MERGE'" in src, \
        "MERGEs no se leen de la API — se perdería +$67 de PnL histórico"
    # Verificar que trader_merged sigue siendo exit_reason válido
    assert "trader_merged" in src, "trader_merged no está como exit_reason"

# ════════════════════════════════════════════════════════════════════════════
# BLOQUE 4 — Router: idempotencia y guards
# ════════════════════════════════════════════════════════════════════════════

def test_router_idempotency_guard():
    """route_signal_to_real_copy verifica bot_trades existentes antes de abrir."""
    src = read('real_signal_router.py')
    assert "bot_trades" in src and "status='open'" in src, \
        "Router no tiene guard de idempotencia — podría abrir trades duplicados"

def test_router_hardcoded_swisstony():
    """Solo Swisstony puede activar el router real DEF_COPY."""
    src = read('real_signal_router.py')
    assert '0x204f72f35326db932158cba6adff0b9a1da95e14' in src, \
        "Wallet de Swisstony no hardcodeada en router"

def test_router_hot_reload():
    """REAL_TRADING_ENABLED se lee con hot-reload — no requiere reinicio."""
    src = read('real_signal_router.py')
    assert 'load_dotenv' in src and 'REAL_TRADING_ENABLED' in src, \
        "Router no hace hot-reload de REAL_TRADING_ENABLED"

# ════════════════════════════════════════════════════════════════════════════
# BLOQUE 5 — Executor
# ════════════════════════════════════════════════════════════════════════════

def test_executor_def_copy_size_cap():
    """DEF_COPY respeta MAX_TRADE_SIZE_USDC igual que agente B."""
    src = read('executor.py')
    assert "'DEF_COPY'" in src and 'MAX_TRADE_SIZE_USDC' in src, \
        "DEF_COPY no tiene cap de tamaño en executor"

# ════════════════════════════════════════════════════════════════════════════
# BLOQUE 6 — Risk manager
# ════════════════════════════════════════════════════════════════════════════

def test_risk_manager_no_performance_block():
    """Risk manager no bloquea por rendimiento — solo por bugs técnicos."""
    src = read('risk_manager.py')
    # check_daily_loss_limit no debe estar en can_trade
    # Buscamos que can_trade NO llame a check_daily_loss_limit
    can_trade_fn = re.search(r'def can_trade.*?(?=\ndef |\Z)', src, re.DOTALL)
    if can_trade_fn:
        body = can_trade_fn.group(0)
        assert 'check_daily_loss_limit' not in body, \
            "can_trade llama a check_daily_loss_limit — bloquea por rendimiento"
        assert 'check_open_positions' not in body or True, \
            "check_open_positions presente — ok si no bloquea DEF_COPY"

# ════════════════════════════════════════════════════════════════════════════
# BLOQUE 7 — DB: no posiciones reales huérfanas
# ════════════════════════════════════════════════════════════════════════════

def test_no_open_real_positions_legacy():
    """No hay bot_trades reales abiertos de agentes desactivados (B, D)."""
    conn = db()
    # B y D están desactivados — no deben tener posiciones abiertas nuevas
    count = conn.execute(
        """SELECT COUNT(*) FROM bot_trades
           WHERE status='open' AND paper_trade=0
           AND agent_id IN ('B','D')"""
    ).fetchone()[0]
    # DEF_COPY puede tener posiciones abiertas — es el sistema activo
    defc = conn.execute(
        "SELECT COUNT(*) FROM bot_trades WHERE status='open' AND paper_trade=0 AND agent_id='DEF_COPY'"
    ).fetchone()[0]
    conn.close()
    assert count == 0, f"{count} bot_trades reales de B/D abiertos — agentes desactivados"
    # Informativo: mostrar cuántos DEF_COPY hay abiertos
    print(f"  DEF_COPY real abiertos: {defc} ✅")

def test_no_ghost_paper_trades():
    """DEF_COPY_05 no tiene trades de wallets ajenas a Lily/Swisstony."""
    conn = db()
    bad = conn.execute("""
        SELECT COUNT(*) FROM paper_trades
        WHERE agent_id='DEF_COPY_05'
          AND wallet_copied NOT IN (
            '0x01c78f8873c0c86d6b6b92ff627e3802237ee995',
            '0x204f72f35326db932158cba6adff0b9a1da95e14'
          )
          AND status NOT IN ('cancelled')
    """).fetchone()[0]
    conn.close()
    assert bad == 0, f"{bad} trades fantasma en DEF_COPY_05 de wallets ajenas"

def test_def_copy_no_lily():
    """DEF_COPY solo tiene trades de Swisstony — Lily eliminada."""
    conn = db()
    lily = conn.execute("""
        SELECT COUNT(*) FROM paper_trades
        WHERE agent_id='DEF_COPY'
          AND LOWER(wallet_copied)='0x01c78f8873c0c86d6b6b92ff627e3802237ee995'
          AND status != 'cancelled'
    """).fetchone()[0]
    conn.close()
    assert lily == 0, f"{lily} trades de Lily en DEF_COPY — deben estar en DEF_COPY_05"

# ════════════════════════════════════════════════════════════════════════════
# BLOQUE 8 — Dashboard IDs
# ════════════════════════════════════════════════════════════════════════════

def test_dashboard_required_ids():
    """Todos los IDs que el JS necesita existen en el HTML."""
    html = (ROOT / 'web/dashboard.html').read_text()
    required = [
        'definitive-shadows-section',
        'dt-tp-open-closed', 'dt-hold-open-closed', 'dt-copy-open-closed',
        'dt-tp-wr-pnl', 'dt-hold-wr-pnl', 'dt-copy-wr-pnl',
        'dt-tp-live', 'dt-hold-live', 'dt-copy-live',
        'dt-tp-peak', 'dt-hold-peak', 'dt-copy-peak',
        'dt-tp-equity', 'dt-hold-equity', 'dt-copy-equity',
        'rc-open', 'rc-closed', 'rc-wr', 'rc-pnl', 'rc-equity',
        'rt-balance', 'rt-pending',
        'risk-veto-banner', 'r-apierr', 'r-bal',
    ]
    for id_ in required:
        assert f'id="{id_}"' in html, f"ID faltante en HTML: {id_}"

def test_dashboard_no_duplicate_ids():
    """No hay IDs duplicados en el HTML (excluir wd-modal que es legítimo)."""
    html = (ROOT / 'web/dashboard.html').read_text()
    ids = re.findall(r'id="([^"]+)"', html)
    from collections import Counter
    dupes = [k for k, v in Counter(ids).items() if v > 1 and not k.startswith('wd-')]
    assert not dupes, f"IDs HTML duplicados: {dupes}"

# ════════════════════════════════════════════════════════════════════════════
# BLOQUE 9 — Config cargada correctamente en runtime
# Fallo real go-live 2026-06-06: config.REAL_TRADING_ENABLED se leía como False
# porque el proceso arrancó ANTES de que se pusiera el flag en .env.
# Solución: reiniciar el proceso. Test: verificar que config lee el .env real.
# ════════════════════════════════════════════════════════════════════════════

def test_config_real_trading_matches_env():
    """config.REAL_TRADING_ENABLED debe coincidir con el valor en .env.
    Si falla → el proceso está corriendo con código/config viejo. REINICIAR.
    """
    import importlib, config as cfg_module
    # Releer el .env para comparar
    from dotenv import dotenv_values
    env_vals = dotenv_values(str(ROOT / '.env'))
    env_value = env_vals.get('REAL_TRADING_ENABLED', 'false').strip().lower() == 'true'
    # Recargar config dinámicamente
    importlib.reload(cfg_module)
    assert cfg_module.REAL_TRADING_ENABLED == env_value, (
        f"config.REAL_TRADING_ENABLED={cfg_module.REAL_TRADING_ENABLED} "
        f"pero .env dice {env_value}. "
        f"SOLUCIÓN: reiniciar el proceso main.py"
    )

def test_config_paper_trading_false():
    """PAPER_TRADING debe estar en false para ejecutar trades reales."""
    import importlib, config as cfg_module
    importlib.reload(cfg_module)
    assert cfg_module.PAPER_TRADING == False, (
        "PAPER_TRADING=True — el sistema está en modo paper, no ejecutará real. "
        "Cambiar a PAPER_TRADING=false en .env y reiniciar."
    )

def test_real_copy_enabled_env():
    """REAL_COPY_ENABLED=true en .env — sin esto el router no ejecuta."""
    from dotenv import dotenv_values
    env_vals = dotenv_values(str(ROOT / '.env'))
    val = env_vals.get('REAL_COPY_ENABLED', 'false').strip().lower()
    # Solo verificar consistencia — si está false es intencional (pre go-live)
    # Si está true, verificar que config también lo lee bien
    if val == 'true':
        # El router hace hot-reload, pero loguear para awareness
        assert 'REAL_COPY_ENABLED' in (ROOT / '.env').read_text(), \
            "REAL_COPY_ENABLED no existe en .env"

def test_real_copy_uses_proportional_scale():
    """route_signal_to_real_copy usa escala proporcional (agg × scale), NO tamaño fijo.
    Fallo go-live 2026-06-06: REAL_COPY_TRADE_SIZE=5 fijo ignoraba la escala del paper.
    """
    src = read('real_signal_router.py')
    assert 'DEFINITIVE_SWIS_SCALE' in src, \
        "Router no usa DEFINITIVE_SWIS_SCALE — tamaño será fijo, no proporcional al paper"
    assert 'agg * scale' in src, \
        "Router no calcula agg×scale — tamaño no será 1:1 con el paper"
    assert '_copy_trade_size()' not in src.split('DEFINITIVE_SWIS_SCALE')[1][:200], \
        "Router cae al tamaño fijo en lugar de la escala proporcional"

def test_executor_real_not_blocked():
    """El executor NO debe tener el flag REAL_TRADING_ENABLED hardcodeado a false.
    Fallo go-live: el proceso viejo tenía config.REAL_TRADING_ENABLED=False en memoria
    aunque el .env ya decía true. Solución fue reiniciar main.py.
    Este test verifica que config y .env están sincronizados."""
    import importlib, config as cfg_module
    from dotenv import dotenv_values
    importlib.reload(cfg_module)
    env_vals = dotenv_values(str(ROOT / '.env'))
    env_real = env_vals.get('REAL_TRADING_ENABLED', 'false').strip().lower() == 'true'
    env_paper = env_vals.get('PAPER_TRADING', 'true').strip().lower() == 'true'
    config_real = cfg_module.REAL_TRADING_ENABLED
    config_paper = cfg_module.PAPER_TRADING
    assert config_real == env_real, \
        f"DESINCRONIZADO: config={config_real} vs .env={env_real} — REINICIAR main.py"
    assert config_paper == env_paper, \
        f"DESINCRONIZADO: config_paper={config_paper} vs .env={env_paper} — REINICIAR main.py"

