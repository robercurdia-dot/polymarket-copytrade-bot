import os
import sqlite3
import hashlib
import json
from datetime import datetime
import config

RESEARCH_CACHE_TTL_H = 6   # horas antes de que el cache de research expire

# DB path: env var POLY_DB_PATH overridable (CRÍTICO para tests aislados).
# Tests deben setear POLY_DB_PATH=/tmp/test_polymarket.db ANTES de importar.
PROD_DB_PATH = "polymarket_insider.db"
DB_PATH = os.getenv("POLY_DB_PATH", PROD_DB_PATH)

def is_prod_db() -> bool:
    """True si la BD activa es la de producción. Usado por guards destructivos."""
    return os.path.abspath(DB_PATH) == os.path.abspath(PROD_DB_PATH)

def get_connection():
    # H-1 FIX (2026-06-10): WAL mode evita "database is locked" en escrituras
    # concurrentes desde múltiples threads (export_loop, executor, watcher).
    # WAL permite múltiples lectores simultáneos + 1 escritor sin bloquear lectores.
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def unsafe_truncate_table(table: str):
    """
    Helper destructivo SOLO para tests. Lanza si la BD activa es producción.
    Esto previene incidentes como 2026-05-04 (DELETE FROM paper_trades sin
    WHERE durante un test borró 217 registros de prod).
    """
    if is_prod_db():
        raise RuntimeError(
            f"unsafe_truncate_table('{table}') BLOQUEADO: la BD activa es PRODUCCIÓN ({DB_PATH}). "
            "Setea POLY_DB_PATH=/tmp/test_xxx.db antes de importar database."
        )
    conn = get_connection()
    c = conn.cursor()
    c.execute(f"DELETE FROM {table}")
    conn.commit()
    conn.close()

def init_db():
    conn = get_connection()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS wallets (
        address TEXT PRIMARY KEY,
        total_trades INTEGER DEFAULT 0,
        winning_trades INTEGER DEFAULT 0,
        win_rate REAL DEFAULT 0,
        avg_entry_odds REAL DEFAULT 0,
        insider_score REAL DEFAULT 0,
        first_seen TEXT,
        last_trade TEXT,
        active INTEGER DEFAULT 1,
        username TEXT DEFAULT '',
        closed_week INTEGER DEFAULT 0,
        win_rate_week REAL DEFAULT 0,
        profit_factor REAL DEFAULT 0,
        closed_total INTEGER DEFAULT 0
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS detected_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        wallet_address TEXT,
        market_id TEXT,
        market_question TEXT,
        side TEXT,
        size_usdc REAL,
        entry_price REAL,
        detected_at TEXT,
        confidence_score REAL,
        research_summary TEXT,
        action_taken TEXT,
        days_left INTEGER DEFAULT 999,
        hours_left INTEGER,
        retorno REAL
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS user_feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id INTEGER,
        action TEXT,
        niche TEXT,
        confidence REAL,
        retorno REAL,
        days_left REAL,
        insider_score REAL,
        timestamp TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS bot_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        detected_trade_id INTEGER,
        market_id TEXT,
        token_id TEXT,
        side TEXT,
        size_usdc REAL,
        entry_price REAL,
        executed_at TEXT,
        paper_trade INTEGER DEFAULT 0,
        outcome TEXT DEFAULT 'open',
        pnl_usdc REAL DEFAULT 0
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS wallet_niche_scores (
        wallet_address TEXT,
        niche TEXT,
        win_rate REAL DEFAULT 0,
        profit_factor REAL DEFAULT 0,
        total_trades INTEGER DEFAULT 0,
        avg_size REAL DEFAULT 0,
        niche_score REAL DEFAULT 0,
        updated_at TEXT,
        PRIMARY KEY (wallet_address, niche)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS elite_traders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        niche TEXT,
        wallet_address TEXT,
        win_rate REAL,
        profit_factor REAL,
        total_trades INTEGER,
        avg_size REAL,
        niche_score REAL,
        rank INTEGER,
        updated_at TEXT,
        UNIQUE(niche, wallet_address)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS trader_reputation (
        wallet TEXT PRIMARY KEY,
        username TEXT DEFAULT '',
        total_copied INTEGER DEFAULT 0,
        total_won INTEGER DEFAULT 0,
        total_lost INTEGER DEFAULT 0,
        real_pnl_usdc REAL DEFAULT 0,
        reputation_score REAL DEFAULT 0.5,
        tier TEXT DEFAULT 'NORMAL',
        consecutive_losses INTEGER DEFAULT 0,
        penalized_until TEXT DEFAULT NULL,
        last_updated TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS tracked_positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        manual_trade_id INTEGER,
        wallet_address TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        outcome TEXT,
        initial_size REAL,
        initial_price REAL,
        last_size REAL,
        last_price REAL,
        status TEXT DEFAULT 'tracking',
        event_log TEXT DEFAULT '[]',
        started_at TEXT,
        last_check TEXT,
        last_event_at TEXT
    )""")
    c.execute("""CREATE INDEX IF NOT EXISTS idx_tracked_status
                 ON tracked_positions(status)""")
    c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_tracked_unique_active
                 ON tracked_positions(wallet_address, condition_id, outcome)
                 WHERE status = 'tracking'""")

    # ── user_profile_snapshots (Fase 2.2 — my_behavior_analyzer) ─────────
    # Histórico completo de snapshots de comportamiento del usuario.
    # Generado diariamente desde my_wallet.closed_positions clasificadas por
    # close_type (Pasada B de Fase 1.2). Permite detectar drift de estilo
    # a lo largo del tiempo.
    c.execute("""CREATE TABLE IF NOT EXISTS user_profile_snapshots (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_at           TEXT NOT NULL,
        sample_size           INTEGER NOT NULL,
        is_milestone          INTEGER NOT NULL DEFAULT 0,
        -- Estilo agregado
        style                 TEXT,           -- MOMENTUM | HYBRID | HOLD
        style_confidence      TEXT,           -- high | medium | low
        sell_pct              REAL,
        avg_hold_hours        REAL,
        -- Distribución de buckets
        n_redeem_win          INTEGER, pnl_redeem_win   REAL,
        n_sell_profit         INTEGER, pnl_sell_profit  REAL,
        n_sell_loss           INTEGER, pnl_sell_loss    REAL,
        n_redeem_loss         INTEGER, pnl_redeem_loss  REAL,
        -- Métricas derivadas y patrones (JSON)
        edges_json            TEXT,
        patterns_json         TEXT,
        recommendations_json  TEXT
    )""")
    c.execute("""CREATE INDEX IF NOT EXISTS idx_profile_snapshot_at
                 ON user_profile_snapshots(snapshot_at DESC)""")

    # ── Migración: añadir columnas paper_metrics a user_profile_snapshots ─
    _existing_cols = {r[1] for r in c.execute("PRAGMA table_info(user_profile_snapshots)")}
    for col, ddl in [
        ("paper_n_trades",    "ALTER TABLE user_profile_snapshots ADD COLUMN paper_n_trades INTEGER"),
        ("paper_n_closed",    "ALTER TABLE user_profile_snapshots ADD COLUMN paper_n_closed INTEGER"),
        ("paper_wr",          "ALTER TABLE user_profile_snapshots ADD COLUMN paper_wr REAL"),
        ("paper_pnl_net",     "ALTER TABLE user_profile_snapshots ADD COLUMN paper_pnl_net REAL"),
        ("paper_tp_hit_pct",  "ALTER TABLE user_profile_snapshots ADD COLUMN paper_tp_hit_pct REAL"),
        ("paper_sl_hit_pct",  "ALTER TABLE user_profile_snapshots ADD COLUMN paper_sl_hit_pct REAL"),
        ("milestone_label",   "ALTER TABLE user_profile_snapshots ADD COLUMN milestone_label TEXT"),
    ]:
        if col not in _existing_cols:
            try: c.execute(ddl)
            except Exception: pass

    # ── paper_trades (Fase 2.3 — paper trading automático) ────────────────
    # Cada señal que pasa filtros en monitor.py abre 1 paper_trade.
    # tp/sl_multiplier se snapshottean al crear → cambios futuros en
    # config no mutan posiciones abiertas.
    c.execute("""CREATE TABLE IF NOT EXISTS paper_trades (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        detected_trade_id   INTEGER NOT NULL,
        wallet_copied       TEXT,
        market_id           TEXT NOT NULL,
        token_id            TEXT,
        market_question     TEXT,
        outcome_name        TEXT,
        side                TEXT,
        niche               TEXT,
        entry_at            TEXT NOT NULL,
        entry_price         REAL NOT NULL,
        entry_size_usdc     REAL NOT NULL,
        entry_shares        REAL,
        tp_target_price     REAL NOT NULL,
        sl_target_price     REAL NOT NULL,
        tp_multiplier       REAL NOT NULL,
        sl_multiplier       REAL NOT NULL,
        status              TEXT NOT NULL DEFAULT 'open',
        exit_at             TEXT,
        exit_price          REAL,
        exit_reason         TEXT,
        last_check_at       TEXT,
        last_check_price    REAL,
        realized_pnl_gross  REAL,
        fees_estimated      REAL DEFAULT 0.30,
        realized_pnl_net    REAL,
        created_at          TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_paper_status ON paper_trades(status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_paper_entry  ON paper_trades(entry_at DESC)")
    # NOTA: el UNIQUE original era sobre (detected_trade_id) solo. Tras la
    # migración multi-agente Fase 2.6, el UNIQUE compuesto correcto se crea
    # más abajo (idx_paper_unique_dt_agent). El viejo se elimina en la misma
    # migración. NO recrearlo aquí — viola integridad con datos multi-agente.

    # ── tuner_state — estado runtime de los parámetros gestionados ────────
    c.execute("""CREATE TABLE IF NOT EXISTS tuner_state (
        parameter       TEXT PRIMARY KEY,
        current_value   TEXT NOT NULL,
        default_value   TEXT NOT NULL,
        last_updated    TEXT,
        cooldown_until  TEXT,
        n_adjustments   INTEGER DEFAULT 0,
        frozen_until    TEXT,
        frozen_reason   TEXT
    )""")

    # ── tuner_actions — audit trail de cada propuesta/aplicación/reversión ─
    c.execute("""CREATE TABLE IF NOT EXISTS tuner_actions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        action_at       TEXT NOT NULL,
        parameter       TEXT NOT NULL,
        before_value    TEXT,
        after_value     TEXT,
        direction       TEXT,
        hypothesis      TEXT,
        evidence_json   TEXT,
        confidence      TEXT,
        cooldown_until  TEXT,
        applied         INTEGER NOT NULL DEFAULT 0,
        dry_run         INTEGER NOT NULL DEFAULT 0,
        reverted_at     TEXT,
        revert_reason   TEXT,
        metric_before   REAL,
        metric_after    REAL,
        validated_at    TEXT
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_tuner_param   ON tuner_actions(parameter, action_at DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_tuner_applied ON tuner_actions(applied, action_at DESC)")

    # ── risk_events (Fase 2.5 — Risk Manager) ─────────────────────────────
    # ── equity_snapshots (L34 — serie temporal de equity real) ───────────
    # Persiste un punto por minuto desde el export loop. Permite la gráfica
    # "Equity histórica" en la dashboard, que es la métrica REAL de cómo
    # está yendo el bot (incluye unrealized PnL, a diferencia de la curva
    # PnL acumulado que solo cuenta cierres).
    c.execute("""CREATE TABLE IF NOT EXISTS equity_snapshots (
        ts              TEXT PRIMARY KEY,
        equity_total    REAL NOT NULL,
        pusd_libre      REAL NOT NULL DEFAULT 0,
        open_value      REAL NOT NULL DEFAULT 0,
        pending_redeem  REAL NOT NULL DEFAULT 0,
        extras_total    REAL NOT NULL DEFAULT 0,
        pnl_unrealized  REAL NOT NULL DEFAULT 0
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_snapshots(ts DESC)")

    c.execute("""CREATE TABLE IF NOT EXISTS risk_events (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp             TEXT NOT NULL,
        event_type            TEXT NOT NULL,        -- DRAWDOWN | API_ERRORS | LOW_BALANCE | MANUAL_PAUSE
        trigger_value         REAL,
        threshold             REAL,
        veto_duration_seconds INTEGER,              -- 0 = permanente
        resolved_at           TEXT,                 -- NULL si veto sigue activo
        context_json          TEXT
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_risk_active ON risk_events(resolved_at, timestamp DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_risk_type   ON risk_events(event_type, timestamp DESC)")

    # ── agents (Fase 2.6 — experimento multi-agente) ──────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS agents (
        id              TEXT PRIMARY KEY,
        name            TEXT NOT NULL,
        description     TEXT,
        tp_multiplier   REAL,
        sl_multiplier   REAL,
        timeout_seconds INTEGER NOT NULL,
        active          INTEGER NOT NULL DEFAULT 1,
        config_frozen   INTEGER NOT NULL DEFAULT 1,
        created_at      TEXT NOT NULL
    )""")

    # Seed obligatorio (idempotente)
    _seed_at = datetime.now().isoformat()
    AGENTS_SEED = [
        ("A",        "Actual",          "Régimen actual con TP 1.8 / SL 0.80",
         1.8, 0.80, 7  * 86400, 1, 1),
        ("B",        "Calibrado",       "Asimetría calibrada TP 1.5 / SL 0.60",
         1.5, 0.60, 7  * 86400, 1, 1),
        ("C",        "Hold-to-resolve", "Sin TP, sin SL, espera resolución natural",
         None, None, 30 * 86400, 1, 1),
        ("A_legacy", "Legacy pre-2.6",  "Histórico anterior al experimento. Oculto en dashboard.",
         None, None, 0, 0, 1),
    ]
    for ag in AGENTS_SEED:
        c.execute("SELECT id FROM agents WHERE id=?", (ag[0],))
        if not c.fetchone():
            c.execute("""INSERT INTO agents
                (id, name, description, tp_multiplier, sl_multiplier,
                 timeout_seconds, active, config_frozen, created_at)
                VALUES (?,?,?,?,?,?,?,?,?)""", ag + (_seed_at,))

    # Migración paper_trades: añadir agent_id + timeout_seconds (snapshot)
    _existing_pt_cols = {r[1] for r in c.execute("PRAGMA table_info(paper_trades)")}
    if "agent_id" not in _existing_pt_cols:
        c.execute("ALTER TABLE paper_trades ADD COLUMN agent_id TEXT NOT NULL DEFAULT 'A_legacy'")
        c.execute("CREATE INDEX IF NOT EXISTS idx_paper_agent ON paper_trades(agent_id, status)")
    if "timeout_seconds" not in _existing_pt_cols:
        c.execute("ALTER TABLE paper_trades ADD COLUMN timeout_seconds INTEGER")
    if "is_hold_to_resolve" not in _existing_pt_cols:
        c.execute("ALTER TABLE paper_trades ADD COLUMN is_hold_to_resolve INTEGER NOT NULL DEFAULT 0")

    # Multi-agente: la UNIQUE original sobre detected_trade_id ya no aplica
    # (ahora hay N papers por señal, uno por agente). Reemplazar por UNIQUE
    # compuesto (detected_trade_id, agent_id).
    try: c.execute("DROP INDEX IF EXISTS idx_paper_unique_dt")
    except Exception: pass
    c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_unique_dt_agent
                 ON paper_trades(detected_trade_id, agent_id)""")

    # Migración tuner_actions: añadir agent_id (default 'A' para pre-existentes)
    _existing_ta_cols = {r[1] for r in c.execute("PRAGMA table_info(tuner_actions)")}
    if "agent_id" not in _existing_ta_cols:
        c.execute("ALTER TABLE tuner_actions ADD COLUMN agent_id TEXT NOT NULL DEFAULT 'A'")
        c.execute("CREATE INDEX IF NOT EXISTS idx_tuner_agent ON tuner_actions(agent_id, action_at DESC)")

    # ── Migración bot_trades (Fase 3.0 — refactor executor) ──────────────
    # Nuevas columnas para tracking del flujo de orden CLOB:
    #   status:          pending → open/failed/rejected (flujo de orden)
    #                    (NO confundir con la columna 'outcome' = won/lost/open)
    #   size_filled:     size realmente llenado por el CLOB (puede ser < size_usdc
    #                    si fill parcial). PnL se calcula sobre size_filled.
    #   fees:            fees pagadas en la transacción (USDC).
    #   order_id:        ID que asigna Polymarket a la orden.
    #   client_order_id: ID idempotente generado por nosotros (bt_<id>_<ts>).
    #   error:           Mensaje de error (recortado) si status=failed/rejected.
    _existing_bt_cols = {r[1] for r in c.execute("PRAGMA table_info(bot_trades)")}
    for col, ddl in [
        ("status",          "ALTER TABLE bot_trades ADD COLUMN status TEXT DEFAULT 'pending'"),
        ("size_filled",     "ALTER TABLE bot_trades ADD COLUMN size_filled REAL"),
        ("fees",            "ALTER TABLE bot_trades ADD COLUMN fees REAL"),
        ("order_id",        "ALTER TABLE bot_trades ADD COLUMN order_id TEXT"),
        ("client_order_id", "ALTER TABLE bot_trades ADD COLUMN client_order_id TEXT"),
        ("error",           "ALTER TABLE bot_trades ADD COLUMN error TEXT"),
        # L81 FIX: agent_id existía en prod pero faltaba la migración.
        # Sin esta línea, en una instalación nueva insert_pending_trade() lanzaba
        # OperationalError: table bot_trades has no column named agent_id → 0 trades reales.
        ("agent_id",        "ALTER TABLE bot_trades ADD COLUMN agent_id TEXT DEFAULT 'B'"),
    ]:
        if col not in _existing_bt_cols:
            c.execute(ddl)

    conn.commit()
    conn.close()
    migrate_db()
    migrate_user_feedback()
    # P&L tracker tables (importación diferida para evitar ciclo)
    import pnl_tracker
    pnl_tracker.init_pnl_tables()
    _init_manual_trades_table()
    _init_hot_streak_table()
    _init_trade_evaluations_table()
    print("Base de datos inicializada")

def migrate_user_feedback():
    """Añade columnas nuevas a user_feedback para el sistema de aprendizaje."""
    conn = get_connection()
    c = conn.cursor()
    for col_def in [
        "ALTER TABLE user_feedback ADD COLUMN wallet TEXT",
        "ALTER TABLE user_feedback ADD COLUMN hour_of_day INTEGER",
        "ALTER TABLE user_feedback ADD COLUMN size_usdc REAL",
        "ALTER TABLE user_feedback ADD COLUMN outcome TEXT",
    ]:
        try:
            c.execute(col_def)
        except Exception:
            pass  # columna ya existe
    conn.commit()
    conn.close()
    _migrate_user_feedback_unique()

def _migrate_user_feedback_unique():
    """
    Añade UNIQUE(trade_id) a user_feedback para evitar duplicados por doble-click.
    Proceso:
      1. Verifica si el índice ya existe — si sí, no hace nada.
      2. Deduplica filas existentes (conserva la más reciente por trade_id).
      3. Recrea la tabla con UNIQUE(trade_id).
    Seguro de ejecutar múltiples veces (idempotente).
    """
    conn = get_connection()
    c = conn.cursor()

    # ¿Ya existe el índice único? → nada que hacer
    c.execute("""
        SELECT name FROM sqlite_master
        WHERE type='index' AND name='idx_user_feedback_trade_id_unique'
    """)
    if c.fetchone():
        conn.close()
        return

    # ── Paso 1: Deduplicar — conservar fila con MAX(id) por trade_id ──────────
    # Para trade_ids NULL SQLite trata cada NULL como distinto → se conservan todos.
    c.execute("""
        DELETE FROM user_feedback
        WHERE id NOT IN (
            SELECT MAX(id) FROM user_feedback
            GROUP BY trade_id
        )
        AND trade_id IS NOT NULL
    """)
    deleted = conn.total_changes
    if deleted:
        print(f"[DB] _migrate_user_feedback_unique: {deleted} fila(s) duplicada(s) eliminadas")

    # ── Paso 2: Crear tabla nueva con UNIQUE(trade_id) ────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_feedback_v2 (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id      INTEGER UNIQUE,
            action        TEXT,
            niche         TEXT,
            confidence    REAL,
            retorno       REAL,
            days_left     REAL,
            insider_score REAL,
            timestamp     TEXT,
            wallet        TEXT,
            hour_of_day   INTEGER,
            size_usdc     REAL,
            outcome       TEXT
        )
    """)

    # ── Paso 3: Copiar datos deduplicados ──────────────────────────────────────
    c.execute("""
        INSERT OR IGNORE INTO user_feedback_v2
            (id, trade_id, action, niche, confidence, retorno, days_left,
             insider_score, timestamp, wallet, hour_of_day, size_usdc, outcome)
        SELECT id, trade_id, action, niche, confidence, retorno, days_left,
               insider_score, timestamp, wallet, hour_of_day, size_usdc, outcome
        FROM user_feedback
    """)

    # ── Paso 4: Reemplazar tabla original ──────────────────────────────────────
    c.execute("DROP TABLE user_feedback")
    c.execute("ALTER TABLE user_feedback_v2 RENAME TO user_feedback")

    # ── Paso 5: Marcar índice como aplicado ───────────────────────────────────
    c.execute("""
        CREATE UNIQUE INDEX idx_user_feedback_trade_id_unique
        ON user_feedback(trade_id)
        WHERE trade_id IS NOT NULL
    """)

    conn.commit()
    conn.close()
    print("[DB] ✅ user_feedback: UNIQUE(trade_id) aplicado y duplicados eliminados")

def migrate_db():
    conn = get_connection()
    c = conn.cursor()
    try:
        c.execute("ALTER TABLE detected_trades ADD COLUMN days_left INTEGER DEFAULT 999")
    except:
        pass
    try:
        c.execute("ALTER TABLE detected_trades ADD COLUMN hours_left INTEGER")
    except:
        pass
    try:
        c.execute("ALTER TABLE detected_trades ADD COLUMN retorno REAL")
    except:
        pass
    try:
        c.execute("ALTER TABLE bot_trades ADD COLUMN token_id TEXT")
    except:
        pass
    try:
        c.execute("ALTER TABLE bot_trades ADD COLUMN outcome TEXT DEFAULT 'open'")
    except:
        pass
    try:
        c.execute("ALTER TABLE bot_trades ADD COLUMN pnl_usdc REAL DEFAULT 0")
    except:
        pass
    try:
        c.execute("ALTER TABLE wallets ADD COLUMN username TEXT DEFAULT ''")
    except:
        pass
    try:
        c.execute("ALTER TABLE wallets ADD COLUMN closed_week INTEGER DEFAULT 0")
    except:
        pass
    try:
        c.execute("ALTER TABLE wallets ADD COLUMN win_rate_week REAL DEFAULT 0")
    except:
        pass
    try:
        c.execute("ALTER TABLE wallets ADD COLUMN profit_factor REAL DEFAULT 0")
    except:
        pass
    try:
        c.execute("ALTER TABLE wallets ADD COLUMN closed_total INTEGER DEFAULT 0")
    except:
        pass
    # F1 (2026-06-18) — closed_at column + trigger.
    # Garantiza que CUALQUIER UPDATE que ponga status='closed' lleva closed_at.
    # Salva el bug nulo histórico independientemente de qué código path cierre.
    try:
        c.execute("ALTER TABLE bot_trades ADD COLUMN closed_at TEXT")
    except Exception:
        pass  # columna ya existe
    c.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_bot_trades_set_closed_at
        AFTER UPDATE OF status ON bot_trades
        FOR EACH ROW
        WHEN NEW.status = 'closed' AND NEW.closed_at IS NULL
        BEGIN
            UPDATE bot_trades SET closed_at = datetime('now')
            WHERE id = NEW.id AND closed_at IS NULL;
        END
    """)

    # Tabla de deduplicación EONET (24 h)
    c.execute("""
        CREATE TABLE IF NOT EXISTS eonet_seen_events (
            event_id     TEXT PRIMARY KEY,
            processed_at TEXT
        )
    """)
    # Cache de research Claude persistente entre reinicios (TTL 6 h)
    c.execute("""
        CREATE TABLE IF NOT EXISTS research_cache (
            question_hash TEXT PRIMARY KEY,
            question_text TEXT,
            result_json   TEXT,
            cached_at     TEXT
        )
    """)
    conn.commit()
    conn.close()
    migrate_user_feedback()

if __name__ == "__main__":
    init_db()

def save_wallet(address, stats):
    conn = get_connection()
    c = conn.cursor()
    active = stats.get("active", 1)
    c.execute("""INSERT OR REPLACE INTO wallets
        (address, total_trades, winning_trades, win_rate, avg_entry_odds,
         insider_score, first_seen, last_trade, username,
         closed_week, win_rate_week, profit_factor, closed_total, active)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (address, stats["total_trades"], stats["winning_trades"], stats["win_rate"],
         stats["avg_entry_odds"], stats["insider_score"], stats["first_seen"],
         datetime.now().isoformat(),
         stats.get("username", ""),
         stats.get("closed_week", 0),
         stats.get("win_rate_week", stats["win_rate"]),
         stats.get("profit_factor", 0),
         stats.get("closed_total", stats["total_trades"]),
         active))
    conn.commit()
    conn.close()

def get_wallet_username(address):
    """Devuelve el username guardado para una wallet, o '' si no tiene."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT username FROM wallets WHERE address = ?", (address,))
    row = c.fetchone()
    conn.close()
    return (row[0] or "") if row else ""

def get_wallet_by_username(username: str) -> str:
    """Devuelve la address de wallet dado un username, o '' si no existe."""
    if not username or username == "manual":
        return ""
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT address FROM wallets WHERE username = ? LIMIT 1", (username,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else ""

def get_top_wallets(limit=50):
    conn = get_connection()
    c = conn.cursor()
    c.execute("""SELECT address, insider_score, win_rate, total_trades, profit_factor
                 FROM wallets WHERE insider_score >= ? AND active = 1
                 ORDER BY insider_score DESC LIMIT ?""",
              (config.MIN_INSIDER_SCORE, limit))
    wallets = c.fetchall()
    conn.close()
    return wallets

def save_detected_trade(trade_data):
    conn = get_connection()
    c = conn.cursor()
    c.execute("""INSERT INTO detected_trades
        (wallet_address, market_id, market_question, side, size_usdc, entry_price,
         detected_at, confidence_score, research_summary, days_left, hours_left, retorno)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (trade_data["wallet"], trade_data["market_id"], trade_data["question"],
         trade_data["side"], trade_data["size"], trade_data["price"],
         datetime.now().isoformat(), trade_data["confidence"], trade_data["research"],
         trade_data.get("days_left"), trade_data.get("hours_left"), trade_data.get("retorno")))
    trade_id = c.lastrowid
    conn.commit()
    conn.close()
    return trade_id

def save_feedback(trade_id, action, niche, confidence, retorno, days_left, insider_score,
                  wallet=None, hour_of_day=None, size_usdc=None, outcome=None):
    conn = get_connection()
    c = conn.cursor()
    # INSERT OR REPLACE: si ya existe una fila con este trade_id (UNIQUE constraint),
    # la reemplaza en vez de duplicarla — evita el race condition de doble-click.
    c.execute("""INSERT OR REPLACE INTO user_feedback
        (trade_id, action, niche, confidence, retorno, days_left, insider_score, timestamp,
         wallet, hour_of_day, size_usdc, outcome)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (trade_id, action, niche, confidence, retorno, days_left, insider_score,
         datetime.now().isoformat(), wallet, hour_of_day, size_usdc, outcome))
    conn.commit()
    conn.close()

def get_all_feedback():
    """Compatibilidad hacia atrás — devuelve columnas básicas."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("""SELECT trade_id, action, niche, confidence, retorno, days_left, insider_score, timestamp
                 FROM user_feedback ORDER BY timestamp""")
    rows = c.fetchall()
    conn.close()
    return [{"trade_id": r[0], "action": r[1], "niche": r[2], "confidence": r[3],
             "retorno": r[4], "days_left": r[5], "insider_score": r[6], "timestamp": r[7]}
            for r in rows]

def get_all_feedback_enriched():
    """Devuelve todo el feedback con las columnas extendidas del sistema de aprendizaje."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("""SELECT id, trade_id, action, niche, confidence, retorno, days_left,
                        insider_score, timestamp, wallet, hour_of_day, size_usdc, outcome
                 FROM user_feedback ORDER BY timestamp""")
    rows = c.fetchall()
    conn.close()
    return [{"id": r[0], "trade_id": r[1], "action": r[2], "niche": r[3],
             "confidence": r[4], "retorno": r[5], "days_left": r[6],
             "insider_score": r[7], "timestamp": r[8], "wallet": r[9],
             "hour_of_day": r[10], "size_usdc": r[11], "outcome": r[12]}
            for r in rows]

def update_feedback_outcome(trade_id, outcome):
    """Registra el resultado real de un trade tras resolución del mercado."""
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "UPDATE user_feedback SET outcome = ? WHERE trade_id = ? AND outcome IS NULL",
        (outcome, trade_id)
    )
    conn.commit()
    conn.close()

def get_unresolved_feedback():
    """
    Devuelve todos los feedback sin outcome todavía.
    Incluye el market_id y side del detected_trade para poder consultar Gamma API.
    """
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT uf.id, uf.trade_id, uf.action, uf.wallet, uf.niche,
               dt.market_id, dt.side, dt.detected_at
        FROM user_feedback uf
        JOIN detected_trades dt ON uf.trade_id = dt.id
        WHERE uf.outcome IS NULL
        ORDER BY uf.timestamp
    """)
    rows = c.fetchall()
    conn.close()
    return [{"id": r[0], "trade_id": r[1], "action": r[2], "wallet": r[3],
             "niche": r[4], "market_id": r[5], "side": r[6], "detected_at": r[7]}
            for r in rows]

def update_trade_action(trade_id, action):
    conn = get_connection()
    c = conn.cursor()
    c.execute("UPDATE detected_trades SET action_taken = ? WHERE id = ?", (action, trade_id))
    conn.commit()
    conn.close()

def save_niche_scores(wallet, niche_scores):
    """Guarda estadísticas por nicho para una wallet."""
    conn = get_connection()
    c = conn.cursor()
    for niche, stats in niche_scores.items():
        c.execute("""INSERT OR REPLACE INTO wallet_niche_scores
            (wallet_address, niche, win_rate, profit_factor, total_trades, avg_size, niche_score, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (wallet, niche, stats["win_rate"], stats["profit_factor"],
             stats["total_trades"], stats["avg_size"], stats["niche_score"],
             datetime.now().isoformat()))
    conn.commit()
    conn.close()

def update_elite_traders(niche, traders):
    """Reemplaza el top 5 de elite_traders para un nicho dado."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("DELETE FROM elite_traders WHERE niche = ?", (niche,))
    for rank, t in enumerate(traders[:5], 1):
        c.execute("""INSERT OR REPLACE INTO elite_traders
            (niche, wallet_address, win_rate, profit_factor, total_trades, avg_size, niche_score, rank, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (niche, t["wallet"], t["win_rate"], t["profit_factor"],
             t["total_trades"], t["avg_size"], t["niche_score"],
             rank, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def is_elite_trader(wallet, niche):
    """Devuelve True si la wallet está en el top 5 del nicho dado."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT 1 FROM elite_traders WHERE wallet_address = ? AND niche = ?", (wallet, niche))
    result = c.fetchone() is not None
    conn.close()
    return result

# ── Reputación dinámica ───────────────────────────────────────────────────────

def get_trader_reputation(wallet):
    """Devuelve el dict de reputación de una wallet, o defaults si no existe."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT wallet, username, total_copied, total_won, total_lost, real_pnl_usdc, reputation_score, tier, consecutive_losses, penalized_until FROM trader_reputation WHERE wallet = ?", (wallet,))
    row = c.fetchone()
    conn.close()
    if row:
        return {
            "wallet": row[0], "username": row[1] or "",
            "total_copied": row[2] or 0,
            "total_won": row[3] or 0, "total_lost": row[4] or 0,
            "real_pnl_usdc": float(row[5] or 0),
            "reputation_score": float(row[6] or 0.5), "tier": row[7] or "NORMAL",
            "consecutive_losses": row[8] or 0, "penalized_until": row[9]
        }
    return {
        "wallet": wallet, "username": "", "total_copied": 0,
        "total_won": 0, "total_lost": 0, "real_pnl_usdc": 0,
        "reputation_score": 0.5, "tier": "NORMAL",
        "consecutive_losses": 0, "penalized_until": None
    }

def record_trade_copied(wallet, username=""):
    """Registra que el usuario copió un trade de este trader."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("""INSERT INTO trader_reputation (wallet, username, total_copied, last_updated)
        VALUES (?, ?, 1, ?)
        ON CONFLICT(wallet) DO UPDATE SET
            total_copied = total_copied + 1,
            username = CASE WHEN ? != '' THEN ? ELSE username END,
            last_updated = ?""",
        (wallet, username, datetime.now().isoformat(),
         username, username, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def update_trade_result(wallet, won, pnl_usdc):
    """Actualiza reputación tras conocer el resultado de un trade copiado."""
    rep = get_trader_reputation(wallet)
    total_won  = rep["total_won"]  + (1 if won else 0)
    total_lost = rep["total_lost"] + (0 if won else 1)
    real_pnl   = round(rep["real_pnl_usdc"] + pnl_usdc, 4)
    consec     = 0 if won else rep["consecutive_losses"] + 1

    # Tier
    penalized_until = rep["penalized_until"]
    if consec >= 3:
        from datetime import timedelta
        penalized_until = (datetime.now() + timedelta(days=7)).isoformat()
        tier = "PENALIZADO"
    elif total_won >= 5:
        tier = "ELITE"
    elif total_won >= 3:
        tier = "BUENO"
    else:
        tier = "NORMAL"

    # reputation_score: win_rate ponderado por volumen
    total = total_won + total_lost
    win_rate = total_won / total if total > 0 else 0.5
    pnl_factor = min(max(real_pnl / (total * 1.0 + 0.001), -1), 1) if total > 0 else 0
    reputation_score = round(win_rate * 0.7 + (pnl_factor + 1) / 2 * 0.3, 4)

    conn = get_connection()
    c = conn.cursor()
    c.execute("""INSERT INTO trader_reputation
        (wallet, total_won, total_lost, real_pnl_usdc, reputation_score, tier,
         consecutive_losses, penalized_until, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(wallet) DO UPDATE SET
            total_won = ?, total_lost = ?, real_pnl_usdc = ?,
            reputation_score = ?, tier = ?, consecutive_losses = ?,
            penalized_until = ?, last_updated = ?""",
        (wallet, total_won, total_lost, real_pnl, reputation_score, tier,
         consec, penalized_until, datetime.now().isoformat(),
         total_won, total_lost, real_pnl, reputation_score, tier,
         consec, penalized_until, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    print(f"[REPUTATION] {wallet[:10]} → tier={tier} won={total_won} lost={total_lost} score={reputation_score}")
    return tier

def is_trader_penalized(wallet):
    """Devuelve True si el trader está penalizado y el plazo no ha expirado."""
    rep = get_trader_reputation(wallet)
    if rep["tier"] != "PENALIZADO":
        return False
    if rep["penalized_until"]:
        try:
            until = datetime.fromisoformat(rep["penalized_until"])
            if datetime.now() < until:
                return True
            # Penalización expirada — resetear a NORMAL
            conn = get_connection()
            c = conn.cursor()
            c.execute("UPDATE trader_reputation SET tier='NORMAL', consecutive_losses=0, penalized_until=NULL WHERE wallet=?", (wallet,))
            conn.commit()
            conn.close()
        except Exception:
            pass
    return False

def get_open_bot_trades():
    """Devuelve bot_trades con outcome='open' y paper_trade=0."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("""SELECT bt.id, bt.market_id, bt.token_id, bt.side, bt.size_usdc,
                        bt.entry_price, bt.executed_at, dt.wallet_address
                 FROM bot_trades bt
                 JOIN detected_trades dt ON bt.detected_trade_id = dt.id
                 WHERE bt.outcome = 'open' AND bt.status = 'open' AND bt.paper_trade = 0""")
    rows = c.fetchall()
    conn.close()
    return [{"id": r[0], "market_id": r[1], "token_id": r[2], "side": r[3],
             "size_usdc": r[4], "entry_price": r[5], "executed_at": r[6],
             "wallet": r[7]} for r in rows]

def close_bot_trade(trade_id, outcome, pnl_usdc):
    """Marca un bot_trade como won/lost con su PnL. No toca failed/rejected."""
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "UPDATE bot_trades SET outcome=?, pnl_usdc=? "
        "WHERE id=? AND status NOT IN ('failed','rejected')",
        (outcome, pnl_usdc, trade_id))
    conn.commit()
    conn.close()


# ── Cache de research Claude (persistente, TTL 6 h) ──────────────────────────

def _research_hash(question: str) -> str:
    """SHA-256 truncado a 16 chars — clave compacta para la pregunta."""
    return hashlib.sha256(question.encode("utf-8")).hexdigest()[:16]

def get_cached_research(question: str):
    """
    Devuelve el resultado de research cacheado para esta pregunta, o None si:
    - No existe entrada
    - Ha caducado (> RESEARCH_CACHE_TTL_H horas)
    """
    try:
        key = _research_hash(question)
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT result_json, cached_at FROM research_cache WHERE question_hash = ?", (key,))
        row = c.fetchone()
        conn.close()
        if not row:
            return None
        cached_at = datetime.fromisoformat(row[1])
        age_h = (datetime.utcnow() - cached_at).total_seconds() / 3600
        if age_h > RESEARCH_CACHE_TTL_H:
            return None  # caducado
        return json.loads(row[0])
    except Exception:
        return None

def set_cached_research(question: str, result: dict):
    """Guarda (o actualiza) el resultado de research en BD."""
    try:
        key  = _research_hash(question)
        data = json.dumps(result, ensure_ascii=False)
        conn = get_connection()
        c = conn.cursor()
        c.execute("""
            INSERT INTO research_cache (question_hash, question_text, result_json, cached_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(question_hash) DO UPDATE SET
                result_json = excluded.result_json,
                cached_at   = excluded.cached_at
        """, (key, question[:200], data, datetime.utcnow().isoformat()))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[CACHE] Error guardando research en BD: {e}")

def purge_expired_research_cache():
    """Elimina entradas caducadas — llamar una vez al inicio."""
    try:
        conn = get_connection()
        c = conn.cursor()
        c.execute("""
            DELETE FROM research_cache
            WHERE (julianday('now') - julianday(cached_at)) * 24 > ?
        """, (RESEARCH_CACHE_TTL_H,))
        deleted = c.rowcount
        conn.commit()
        conn.close()
        if deleted:
            print(f"[CACHE] 🗑  {deleted} entradas de research caducadas eliminadas")
    except Exception as e:
        print(f"[CACHE] Error purgando cache: {e}")


# ── Manual trade learning ─────────────────────────────────────────────────────

def _init_manual_trades_table():
    """Crea la tabla de trades manuales si no existe."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS manual_trades (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id        TEXT NOT NULL,
            market_question  TEXT DEFAULT '',
            side             TEXT NOT NULL,
            entry_price      REAL NOT NULL,
            size_usdc        REAL NOT NULL,
            copied_from      TEXT DEFAULT '',
            reason           TEXT DEFAULT '',
            days_left        INTEGER DEFAULT 999,
            niche            TEXT DEFAULT '',
            logged_at        TEXT NOT NULL,
            resolved_at      TEXT,
            winning_side     TEXT,
            won              INTEGER,
            pnl              REAL,
            status           TEXT DEFAULT 'OPEN'
        )
    """)
    conn.commit()
    conn.close()

def save_manual_trade(market_id, side, price, size, copied_from="",
                      reason="", days_left=999, niche="", market_question="") -> int:
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        INSERT INTO manual_trades
            (market_id, market_question, side, entry_price, size_usdc,
             copied_from, reason, days_left, niche, logged_at, status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (market_id, market_question, side.upper(), price, size,
         copied_from, reason, days_left, niche,
         datetime.now().isoformat(), "OPEN"))
    trade_id = c.lastrowid
    conn.commit()
    conn.close()
    return trade_id

def get_open_manual_trades() -> list:
    conn = get_connection()
    c = conn.cursor()
    c.execute("""SELECT id, market_id, market_question, side, entry_price,
                        size_usdc, copied_from, reason, days_left, niche, logged_at
                 FROM manual_trades WHERE status='OPEN' ORDER BY logged_at DESC""")
    rows = c.fetchall()
    conn.close()
    return [{"id": r[0], "market_id": r[1], "market_question": r[2], "side": r[3],
             "entry_price": r[4], "size_usdc": r[5], "copied_from": r[6],
             "reason": r[7], "days_left": r[8], "niche": r[9], "logged_at": r[10]}
            for r in rows]

def resolve_manual_trade(trade_id, winning_side, pnl=None) -> bool:
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT side, size_usdc, entry_price FROM manual_trades WHERE id=?", (trade_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return False
    side, size, price = row
    won = side.upper() == winning_side.upper()
    if pnl is None:
        shares = size / price if price > 0 else 0
        pnl = round(shares * 1.0 - size, 2) if won else round(-size, 2)
    c.execute("""UPDATE manual_trades SET
                    resolved_at=?, winning_side=?, won=?, pnl=?, status=?
                 WHERE id=?""",
              (datetime.now().isoformat(), winning_side.upper(),
               1 if won else 0, pnl,
               "WON" if won else "LOST", trade_id))
    conn.commit()
    conn.close()
    return won

# ── Hot streak watch list ─────────────────────────────────────────────────────

# ── Trade evaluations (Claude AI pre-alert scoring) ──────────────────────────

def _init_trade_evaluations_table():
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS trade_evaluations (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_trade_id   INTEGER NOT NULL,
            trader_wallet       TEXT,
            market_question     TEXT,
            confidence_score    INTEGER,
            verdict             TEXT,
            reasoning_json      TEXT,
            recommended_size_pct INTEGER,
            risks_json          TEXT,
            summary             TEXT,
            model_used          TEXT,
            evaluated_at        TEXT
        )
    """)
    conn.commit()
    conn.close()

def store_trade_evaluation(detected_trade_id, trader_wallet, market_question,
                           confidence_score, verdict, reasoning_json,
                           recommended_size_pct, risks_json, summary, model_used):
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        INSERT INTO trade_evaluations
            (detected_trade_id, trader_wallet, market_question, confidence_score,
             verdict, reasoning_json, recommended_size_pct, risks_json,
             summary, model_used, evaluated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (detected_trade_id, trader_wallet, market_question, confidence_score,
          verdict, reasoning_json, recommended_size_pct, risks_json,
          summary, model_used, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_trader_full_stats(wallet: str) -> dict:
    """
    Devuelve stats completos de un trader:
    - De wallets: win_rate, profit_factor, total cerradas, username
    - De trader_reputation: real PnL, copies, won/lost
    - De wallet_niche_scores: WR por nicho
    """
    conn = get_connection()
    c = conn.cursor()

    # Stats base
    c.execute("""SELECT username, win_rate, profit_factor, closed_total,
                        winning_trades, total_trades - winning_trades
                 FROM wallets WHERE address = ?""", (wallet,))
    base = c.fetchone()

    # Reputación real
    c.execute("""SELECT total_copied, total_won, total_lost, real_pnl_usdc
                 FROM trader_reputation WHERE wallet = ?""", (wallet,))
    rep = c.fetchone()

    # Niche scores
    c.execute("""SELECT niche, win_rate FROM wallet_niche_scores
                 WHERE wallet_address = ? ORDER BY win_rate DESC LIMIT 1""", (wallet,))
    top_niche = c.fetchone()

    conn.close()

    result = {
        "wallet":       wallet,
        "name":         base[0] if base else "",
        "win_rate":     float(base[1] or 0.5) if base else 0.5,
        "profit_factor":float(base[2] or 1.0) if base else 1.0,
        "total_closed": int(base[3] or 0) if base else 0,
        "avg_win":      0,  # no almacenado en wallets, se usa 0
        "avg_loss":     0,
        "niche_win_rate": float(top_niche[1]) if top_niche else None,
        "total_copied": int(rep[0] or 0) if rep else 0,
        "total_won":    int(rep[1] or 0) if rep else 0,
        "total_lost":   int(rep[2] or 0) if rep else 0,
        "real_pnl_usdc":float(rep[3] or 0) if rep else 0,
    }
    return result

def get_similar_trades_history(wallet: str, niche: str, limit: int = 10) -> list:
    """
    Devuelve los últimos trades copiados de este wallet en este nicho con PnL conocido.
    """
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT bt.pnl_usdc
        FROM bot_trades bt
        JOIN detected_trades dt ON bt.detected_trade_id = dt.id
        WHERE dt.wallet_address = ?
          AND bt.outcome != 'open'
          AND bt.paper_trade = 0
        ORDER BY bt.executed_at DESC
        LIMIT ?
    """, (wallet, limit))
    rows = c.fetchall()
    conn.close()
    return [{"pnl_usdc": r[0]} for r in rows]

def _init_hot_streak_table():
    """Crea la tabla de hot streak traders si no existe."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS hot_streak_traders (
            wallet         TEXT PRIMARY KEY,
            username       TEXT DEFAULT '',
            detected_at    TEXT NOT NULL,
            expires_at     TEXT NOT NULL,
            last_10_wr     REAL DEFAULT 0,
            last_5_wr      REAL DEFAULT 0,
            last_20_wr     REAL DEFAULT 0,
            historical_wr  REAL DEFAULT 0,
            total_pnl      REAL DEFAULT 0,
            profit_factor  REAL DEFAULT 0,
            status         TEXT DEFAULT 'ACTIVE'
        )
    """)
    conn.commit()
    conn.close()

def save_hot_streak_trader(wallet, username, last_10_wr, last_5_wr, last_20_wr,
                           historical_wr, total_pnl, profit_factor):
    """Inserta o actualiza un trader en la watch list de hot streaks (TTL 48h)."""
    now        = datetime.now()
    expires_at = (now + __import__("datetime").timedelta(hours=48)).isoformat()
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        INSERT INTO hot_streak_traders
            (wallet, username, detected_at, expires_at,
             last_10_wr, last_5_wr, last_20_wr, historical_wr, total_pnl, profit_factor, status)
        VALUES (?,?,?,?,?,?,?,?,?,?,'ACTIVE')
        ON CONFLICT(wallet) DO UPDATE SET
            username      = excluded.username,
            expires_at    = excluded.expires_at,
            last_10_wr    = excluded.last_10_wr,
            last_5_wr     = excluded.last_5_wr,
            last_20_wr    = excluded.last_20_wr,
            historical_wr = excluded.historical_wr,
            total_pnl     = excluded.total_pnl,
            profit_factor = excluded.profit_factor,
            status        = 'ACTIVE'
    """, (wallet, username, now.isoformat(), expires_at,
          last_10_wr, last_5_wr, last_20_wr, historical_wr, total_pnl, profit_factor))
    conn.commit()
    conn.close()

def get_active_hot_streak_traders() -> list:
    """Devuelve traders con hot streak activo (status=ACTIVE y no expirados)."""
    conn = get_connection()
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute("""
        SELECT wallet, username, detected_at, expires_at,
               last_10_wr, last_5_wr, last_20_wr, historical_wr, total_pnl, profit_factor
        FROM hot_streak_traders
        WHERE status = 'ACTIVE' AND expires_at > ?
        ORDER BY total_pnl DESC
    """, (now,))
    rows = c.fetchall()
    conn.close()
    return [{"wallet": r[0], "username": r[1], "detected_at": r[2], "expires_at": r[3],
             "wr10": r[4], "wr5": r[5], "wr20": r[6],
             "hist_wr": r[7], "total_pnl": r[8], "profit_factor": r[9]}
            for r in rows]

def expire_hot_streak_traders():
    """Marca como EXPIRED los traders cuyo TTL de 48h haya caducado."""
    conn = get_connection()
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute("UPDATE hot_streak_traders SET status='EXPIRED' WHERE expires_at <= ? AND status='ACTIVE'", (now,))
    expired = c.rowcount
    conn.commit()
    conn.close()
    if expired:
        print(f"[HOT] {expired} hot streak(s) expirados")

def get_all_manual_trades() -> list:
    conn = get_connection()
    c = conn.cursor()
    c.execute("""SELECT id, market_id, market_question, side, entry_price, size_usdc,
                        copied_from, reason, days_left, niche, logged_at,
                        resolved_at, winning_side, won, pnl, status
                 FROM manual_trades ORDER BY logged_at DESC""")
    rows = c.fetchall()
    conn.close()
    return [{"id": r[0], "market_id": r[1], "market_question": r[2], "side": r[3],
             "entry_price": r[4], "size_usdc": r[5], "copied_from": r[6],
             "reason": r[7], "days_left": r[8], "niche": r[9], "logged_at": r[10],
             "resolved_at": r[11], "winning_side": r[12], "won": r[13],
             "pnl": r[14], "status": r[15]}
            for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# Tracked positions — whale tracker
# ─────────────────────────────────────────────────────────────────────────────

def add_tracked_position(manual_trade_id, wallet_address, condition_id, outcome,
                         initial_size, initial_price):
    """
    Inserta una posición a trackear. Si ya existe una entrada activa
    (UNIQUE WHERE status='tracking'), reutiliza la fila existente y
    devuelve su id. Devuelve None si falla.
    """
    import json as _json
    from datetime import datetime as _dt
    conn = get_connection()
    c = conn.cursor()
    now = _dt.utcnow().isoformat()
    try:
        c.execute("""
            INSERT INTO tracked_positions
                (manual_trade_id, wallet_address, condition_id, outcome,
                 initial_size, initial_price, last_size, last_price,
                 status, event_log, started_at, last_check, last_event_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'tracking', ?, ?, ?, ?)
        """, (manual_trade_id, wallet_address, condition_id, outcome,
              initial_size, initial_price, initial_size, initial_price,
              _json.dumps([{"ts": now, "type": "registered",
                            "size": initial_size, "price": initial_price}]),
              now, now, now))
        conn.commit()
        new_id = c.lastrowid
        conn.close()
        return new_id
    except Exception:
        # Probablemente UNIQUE constraint — recuperar id existente
        c.execute("""SELECT id FROM tracked_positions
                     WHERE wallet_address = ? AND condition_id = ?
                       AND COALESCE(outcome,'') = COALESCE(?,'')
                       AND status = 'tracking'""",
                  (wallet_address, condition_id, outcome))
        row = c.fetchone()
        conn.close()
        return row[0] if row else None


def get_active_tracked_positions():
    """Devuelve todas las posiciones con status='tracking' como list de dicts."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("""SELECT id, manual_trade_id, wallet_address, condition_id, outcome,
                        initial_size, initial_price, last_size, last_price,
                        status, event_log, started_at, last_check, last_event_at
                 FROM tracked_positions WHERE status = 'tracking'""")
    rows = c.fetchall()
    conn.close()
    keys = ["id","manual_trade_id","wallet_address","condition_id","outcome",
            "initial_size","initial_price","last_size","last_price",
            "status","event_log","started_at","last_check","last_event_at"]
    return [dict(zip(keys, r)) for r in rows]


def update_tracked_position(tracked_id, last_size=None, last_price=None,
                            status=None, event=None):
    """
    Actualiza una posición trackeada. `event` es un dict que se appendea al
    event_log (JSON array). Si se pasa `status` distinto de 'tracking', se
    cambia y se actualiza last_event_at.
    """
    import json as _json
    from datetime import datetime as _dt
    conn = get_connection()
    c = conn.cursor()
    now = _dt.utcnow().isoformat()

    sets = ["last_check = ?"]
    args = [now]
    if last_size is not None:
        sets.append("last_size = ?"); args.append(last_size)
    if last_price is not None:
        sets.append("last_price = ?"); args.append(last_price)
    if status is not None:
        sets.append("status = ?"); args.append(status)
        sets.append("last_event_at = ?"); args.append(now)
    if event is not None:
        c.execute("SELECT event_log FROM tracked_positions WHERE id = ?", (tracked_id,))
        row = c.fetchone()
        log = []
        if row and row[0]:
            try:
                log = _json.loads(row[0])
            except Exception:
                log = []
        ev = dict(event)
        ev.setdefault("ts", now)
        log.append(ev)
        sets.append("event_log = ?"); args.append(_json.dumps(log))
        sets.append("last_event_at = ?"); args.append(now)

    args.append(tracked_id)
    c.execute(f"UPDATE tracked_positions SET {', '.join(sets)} WHERE id = ?", args)
    conn.commit()
    conn.close()


# ═════════════════════════════════════════════════════════════════════
# Fase 3.0 — Helpers para flujo atómico de bot_trades (executor.py)
# ═════════════════════════════════════════════════════════════════════
# Patrón pending → orden CLOB → filled/failed/rejected, persistido en BD
# desde el principio para no perder trazas si la orden se ejecuta pero
# el cliente lanza excepción.

def insert_pending_trade(detected_trade_id, market_id, token_id, side,
                         size_usdc, client_order_id, paper_trade=False,
                         agent_id='B'):
    """
    Inserta un bot_trade en estado 'pending' ANTES de mandar la orden.
    Devuelve bot_trade_id.

    Campos llenos: identificadores y tamaño solicitado.
    Campos NULL: entry_price, executed_at (se completan en update_trade_filled).

    `agent_id` (2026-05-28): 'B' = TP/SL (default), 'D' = hold-to-resolve.
    """
    conn = get_connection()
    c = conn.cursor()
    c.execute("""INSERT INTO bot_trades
        (detected_trade_id, market_id, token_id, side, size_usdc,
         paper_trade, status, client_order_id, outcome, agent_id)
        VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, 'open', ?)""",
        (detected_trade_id, market_id, token_id, side, size_usdc,
         1 if paper_trade else 0, client_order_id, agent_id))
    bt_id = c.lastrowid
    conn.commit()
    conn.close()
    return bt_id


# Fix arquitectónico 2026-05-24 — inmutabilidad de estados terminales.
# `closed`, `rejected`, `failed` son TERMINALES: ningún UPDATE puede
# salir de ellos a menos que el caller pase force=True (uso administrativo).
# Esto elimina la familia de race conditions donde un loop sobreescribía
# el cierre de otro (ver LESSONS L01/L07/L16/L18/L19/L20).
TERMINAL_STATUSES = ('closed', 'rejected', 'failed')


def update_trade_status(bot_trade_id, status, error=None, force=False) -> bool:
    """
    Actualiza solo status (+ error opcional). Usado cuando la orden falla
    o es rechazada por CLOB.

    Devuelve True si el UPDATE afectó una fila, False si fue rechazado
    porque el trade ya estaba en estado terminal (otro loop lo cerró).
    Force=True salta el guard (solo para reconcile administrativo).
    """
    conn = get_connection()
    c = conn.cursor()
    if force:
        if error is not None:
            c.execute("UPDATE bot_trades SET status=?, error=? WHERE id=?",
                      (status, str(error)[:200], bot_trade_id))
        else:
            c.execute("UPDATE bot_trades SET status=? WHERE id=?",
                      (status, bot_trade_id))
    else:
        # Guard contra sobrescritura de estados terminales
        ph = ",".join("?" * len(TERMINAL_STATUSES))
        if error is not None:
            c.execute(f"""UPDATE bot_trades SET status=?, error=?
                          WHERE id=? AND status NOT IN ({ph})""",
                      (status, str(error)[:200], bot_trade_id, *TERMINAL_STATUSES))
        else:
            c.execute(f"""UPDATE bot_trades SET status=?
                          WHERE id=? AND status NOT IN ({ph})""",
                      (status, bot_trade_id, *TERMINAL_STATUSES))
    affected = c.rowcount
    conn.commit()
    conn.close()
    if affected == 0 and not force:
        # No es error — significa que otro loop ya cerró el trade.
        print(f"[DB] update_trade_status({bot_trade_id} → {status}) "
              f"rechazado: trade en estado terminal")
    return affected > 0


def update_trade_filled(bot_trade_id, entry_price, size_filled, fees,
                        order_id, status='open', executed_at=None,
                        force=False) -> bool:
    """
    Completa los datos del fill real tras confirmar la orden con CLOB.
    Llamar después de un create_and_post_order exitoso.

    Devuelve True si actualizó, False si fue rechazado por guard terminal.
    """
    from datetime import datetime as _dt
    if executed_at is None:
        executed_at = _dt.utcnow().isoformat()
    conn = get_connection()
    c = conn.cursor()
    if force:
        c.execute("""UPDATE bot_trades
            SET status=?, entry_price=?, size_filled=?, fees=?,
                order_id=?, executed_at=?
            WHERE id=?""",
            (status, entry_price, size_filled, fees, order_id,
             executed_at, bot_trade_id))
    else:
        ph = ",".join("?" * len(TERMINAL_STATUSES))
        c.execute(f"""UPDATE bot_trades
            SET status=?, entry_price=?, size_filled=?, fees=?,
                order_id=?, executed_at=?
            WHERE id=? AND status NOT IN ({ph})""",
            (status, entry_price, size_filled, fees, order_id,
             executed_at, bot_trade_id, *TERMINAL_STATUSES))
    affected = c.rowcount
    conn.commit()
    conn.close()
    if affected == 0 and not force:
        print(f"[DB] update_trade_filled({bot_trade_id} → {status}) "
              f"rechazado: trade en estado terminal")
    return affected > 0
