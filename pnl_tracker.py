"""
pnl_tracker.py — Tracking de P&L real de todos los trades copiados
==================================================================
Tablas nuevas: live_trades, daily_performance
Se integra con el flujo de ejecución en monitor.py y executor.py.
"""

import json
import sqlite3
from datetime import datetime, date, timezone

import database


# ── Schema ────────────────────────────────────────────────────────────────────

_DDL = [
    """
    CREATE TABLE IF NOT EXISTS live_trades (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_timestamp         TEXT NOT NULL,
        copied_from_wallet      TEXT NOT NULL,
        copied_from_name        TEXT DEFAULT '',
        market_id               TEXT NOT NULL,
        market_question         TEXT NOT NULL,
        outcome_bought          TEXT NOT NULL,
        entry_price             REAL NOT NULL,
        position_size           REAL NOT NULL,
        shares_bought           REAL NOT NULL,
        their_trade_time        TEXT,
        our_trade_time          TEXT,
        execution_lag_seconds   REAL,
        resolved_timestamp      TEXT,
        resolved_outcome        TEXT,
        final_price             REAL,
        pnl                     REAL,
        pnl_percentage          REAL,
        won                     INTEGER,
        status                  TEXT DEFAULT 'OPEN',
        notes                   TEXT,
        created_at              TEXT DEFAULT (datetime('now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_lt_status  ON live_trades(status)",
    "CREATE INDEX IF NOT EXISTS idx_lt_market  ON live_trades(market_id)",
    "CREATE INDEX IF NOT EXISTS idx_lt_wallet  ON live_trades(copied_from_wallet)",
    """
    CREATE TABLE IF NOT EXISTS daily_performance (
        date              TEXT PRIMARY KEY,
        trades_executed   INTEGER DEFAULT 0,
        trades_resolved   INTEGER DEFAULT 0,
        trades_open       INTEGER DEFAULT 0,
        capital_deployed  REAL DEFAULT 0,
        realized_pnl      REAL DEFAULT 0,
        unrealized_pnl    REAL DEFAULT 0,
        win_rate          REAL DEFAULT 0,
        avg_win           REAL DEFAULT 0,
        avg_loss          REAL DEFAULT 0,
        largest_win       REAL DEFAULT 0,
        largest_loss      REAL DEFAULT 0,
        trader_breakdown  TEXT DEFAULT '{}',
        updated_at        TEXT DEFAULT (datetime('now'))
    )
    """,
]


def init_pnl_tables():
    """Crea las tablas si no existen. Llamar en database.init_db()."""
    conn = database.get_connection()
    c = conn.cursor()
    for ddl in _DDL:
        c.execute(ddl)
    conn.commit()
    conn.close()


# ── Registro de entrada ───────────────────────────────────────────────────────

def record_entry(
    copied_from_wallet: str,
    copied_from_name: str,
    market_id: str,
    market_question: str,
    outcome_bought: str,
    entry_price: float,
    position_size: float,
    their_trade_time=None,
    our_trade_time=None,
) -> int:
    """
    Registra un nuevo trade copiado. Devuelve el ID de la fila.

    outcome_bought  — 'YES' | 'NO'
    entry_price     — entre 0 y 1
    position_size   — USDC invertidos
    """
    shares = position_size / entry_price if entry_price > 0 else 0

    lag = None
    if their_trade_time and our_trade_time:
        try:
            if isinstance(their_trade_time, (int, float)):
                their_dt = datetime.utcfromtimestamp(their_trade_time)
            else:
                their_dt = their_trade_time
            if isinstance(our_trade_time, (int, float)):
                our_dt = datetime.utcfromtimestamp(our_trade_time)
            else:
                our_dt = our_trade_time
            lag = (our_dt - their_dt).total_seconds()
        except Exception:
            lag = None

    now_str = datetime.utcnow().isoformat()

    conn = database.get_connection()
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO live_trades (
            trade_timestamp, copied_from_wallet, copied_from_name,
            market_id, market_question, outcome_bought,
            entry_price, position_size, shares_bought,
            their_trade_time, our_trade_time, execution_lag_seconds,
            status
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'OPEN')
        """,
        (
            now_str,
            copied_from_wallet,
            copied_from_name,
            market_id,
            market_question,
            outcome_bought,
            entry_price,
            position_size,
            shares,
            str(their_trade_time) if their_trade_time else None,
            str(our_trade_time) if our_trade_time else None,
            lag,
        ),
    )
    trade_id = c.lastrowid
    conn.commit()
    conn.close()

    _update_daily(date.today().isoformat())

    lag_str = f"  Lag: {lag:.0f}s" if lag is not None else ""
    print(
        f"[PNL] #{trade_id} entrada: {outcome_bought} @ {entry_price:.3f} "
        f"({position_size:.2f} USDC) | {market_question[:55]}{lag_str}"
    )
    return trade_id


# ── Resolución ────────────────────────────────────────────────────────────────

def record_resolution(trade_id: int, resolved_outcome: str, final_price: float = None):
    """
    Cierra un trade cuando el mercado se resuelve.

    resolved_outcome — 'YES' | 'NO'
    final_price      — 0.0 ó 1.0 (opcional; se infiere si no se pasa)
    """
    conn = database.get_connection()
    c = conn.cursor()
    c.execute(
        "SELECT outcome_bought, position_size, shares_bought FROM live_trades WHERE id=?",
        (trade_id,),
    )
    row = c.fetchone()
    if not row:
        conn.close()
        print(f"[PNL] Trade #{trade_id} no encontrado")
        return None

    outcome_bought, position_size, shares = row

    won = outcome_bought.upper() == resolved_outcome.upper()

    if final_price is None:
        final_price = 1.0 if won else 0.0

    pnl = (shares * 1.0 - position_size) if won else -position_size
    pnl_pct = (pnl / position_size * 100) if position_size > 0 else 0

    c.execute(
        """
        UPDATE live_trades SET
            resolved_timestamp = ?,
            resolved_outcome   = ?,
            final_price        = ?,
            pnl                = ?,
            pnl_percentage     = ?,
            won                = ?,
            status             = 'RESOLVED'
        WHERE id = ?
        """,
        (
            datetime.utcnow().isoformat(),
            resolved_outcome,
            final_price,
            round(pnl, 4),
            round(pnl_pct, 2),
            1 if won else 0,
            trade_id,
        ),
    )
    conn.commit()
    conn.close()

    _update_daily(date.today().isoformat())

    tag = "WIN" if won else "LOSS"
    print(f"[PNL] #{trade_id} {tag} → pnl={pnl:+.2f} USDC ({pnl_pct:+.1f}%)")
    return pnl


# ── Chequear y resolver trades abiertos ──────────────────────────────────────

def check_open_trades():
    """
    Consulta la Gamma API por cada trade abierto.
    Si el mercado está resuelto, registra el resultado.
    Llamar periódicamente desde un loop asyncio.
    """
    import requests as _req

    conn = database.get_connection()
    c = conn.cursor()
    c.execute(
        "SELECT id, market_id, outcome_bought FROM live_trades WHERE status='OPEN'"
    )
    open_trades = c.fetchall()
    conn.close()

    if not open_trades:
        return

    print(f"[PNL] Verificando {len(open_trades)} trade(s) abierto(s)...")

    for trade_id, market_id, outcome_bought in open_trades:
        try:
            r = _req.get(
                f"https://gamma-api.polymarket.com/markets?conditionId={market_id}",
                timeout=10,
            )
            if r.status_code != 200:
                continue
            data = r.json()
            mkt = data[0] if isinstance(data, list) and data else data
            if not mkt:
                continue

            # Mercado cerrado → outcomePrices tiene 1.0 en el ganador
            if not mkt.get("closed", False):
                continue

            raw_prices = mkt.get("outcomePrices")
            outcomes_raw = mkt.get("outcomes")
            if not raw_prices or not outcomes_raw:
                continue

            if isinstance(raw_prices, str):
                try:
                    import json as _json
                    raw_prices = _json.loads(raw_prices.replace("'", '"'))
                except Exception:
                    continue

            if isinstance(outcomes_raw, str):
                try:
                    import json as _json
                    outcomes_raw = _json.loads(outcomes_raw.replace("'", '"'))
                except Exception:
                    continue

            prices = [float(p) for p in raw_prices]
            outcomes = list(outcomes_raw)

            # Ganador: precio ≈ 1.0
            winner_idx = None
            for i, p in enumerate(prices):
                if p >= 0.99:
                    winner_idx = i
                    break

            if winner_idx is None:
                continue  # sin resolución clara todavía

            winning_label = outcomes[winner_idx] if winner_idx < len(outcomes) else "YES"
            record_resolution(trade_id, winning_label, float(prices[winner_idx]))

        except Exception as e:
            print(f"[PNL] Error verificando trade #{trade_id}: {e}")


# ── Resolución de outcomes de user_feedback ──────────────────────────────────

def resolve_feedback_outcomes():
    """
    Para cada entrada de user_feedback sin outcome, consulta la Gamma API y
    determina si el mercado se resolvió.  Actualiza el outcome según:

      copied  + insider ganó  → 'copied_won'
      copied  + insider perdió→ 'copied_lost'
      ignored + insider ganó  → 'ignored_would_have_won'
      ignored + insider perdió→ 'ignored_good_decision'

    Llamar periódicamente desde pnl_check_loop en main.py.
    """
    import requests as _req

    unresolved = database.get_unresolved_feedback()
    if not unresolved:
        return

    print(f"[LEARN] Verificando outcomes de {len(unresolved)} feedback(s) sin resolver...")

    resolved_count = 0
    for entry in unresolved:
        market_id  = entry.get("market_id", "")
        action     = entry.get("action", "")
        side       = (entry.get("side") or "").upper().strip()
        trade_id   = entry["trade_id"]

        if not market_id or not side:
            continue

        try:
            r = _req.get(
                f"https://gamma-api.polymarket.com/markets?conditionId={market_id}",
                timeout=10,
            )
            if r.status_code != 200:
                continue
            data = r.json()
            mkt = data[0] if isinstance(data, list) and data else data
            if not mkt or not mkt.get("closed", False):
                continue  # mercado aún abierto

            raw_prices   = mkt.get("outcomePrices")
            outcomes_raw = mkt.get("outcomes")
            if not raw_prices or not outcomes_raw:
                continue

            if isinstance(raw_prices, str):
                try:
                    raw_prices = json.loads(raw_prices.replace("'", '"'))
                except Exception:
                    continue
            if isinstance(outcomes_raw, str):
                try:
                    outcomes_raw = json.loads(outcomes_raw.replace("'", '"'))
                except Exception:
                    continue

            prices   = [float(p) for p in raw_prices]
            outcomes = list(outcomes_raw)

            winner_idx = next((i for i, p in enumerate(prices) if p >= 0.99), None)
            if winner_idx is None:
                continue

            winning_label = (outcomes[winner_idx] if winner_idx < len(outcomes) else "YES").upper().strip()

            # Normalizar: BUY→YES, SELL→NO
            norm_side = "YES" if side in ("BUY", "YES") else "NO"
            insider_won = (norm_side == winning_label)

            if action == "copied":
                outcome = "copied_won" if insider_won else "copied_lost"
            else:
                outcome = "ignored_would_have_won" if insider_won else "ignored_good_decision"

            database.update_feedback_outcome(trade_id, outcome)
            resolved_count += 1
            tag = "✅" if insider_won else "❌"
            print(f"[LEARN] trade #{trade_id} ({action}) → {outcome} {tag}")

        except Exception as e:
            print(f"[LEARN] Error verificando feedback trade #{trade_id}: {e}")

    if resolved_count:
        print(f"[LEARN] {resolved_count} outcome(s) actualizados")


# ── Daily summary ─────────────────────────────────────────────────────────────

def _update_daily(day_str: str):
    conn = database.get_connection()
    c = conn.cursor()

    c.execute(
        """
        SELECT
            COUNT(*)                                                    AS total,
            COUNT(CASE WHEN status='RESOLVED' THEN 1 END)              AS resolved,
            COUNT(CASE WHEN status='OPEN'     THEN 1 END)              AS open_cnt,
            COALESCE(SUM(position_size), 0)                            AS deployed,
            COALESCE(SUM(CASE WHEN status='RESOLVED' THEN pnl ELSE 0 END), 0) AS realized,
            COALESCE(SUM(CASE WHEN won=1 AND status='RESOLVED' THEN pnl ELSE 0 END), 0) AS win_pnl,
            COALESCE(SUM(CASE WHEN won=0 AND status='RESOLVED' THEN ABS(pnl) ELSE 0 END), 0) AS loss_pnl,
            COALESCE(SUM(CASE WHEN won=1 THEN 1 ELSE 0 END), 0)        AS wins,
            COALESCE(COUNT(CASE WHEN status='RESOLVED' THEN 1 END), 0) AS res_count,
            COALESCE(MAX(CASE WHEN won=1 THEN pnl ELSE 0 END), 0)      AS largest_win,
            COALESCE(MIN(CASE WHEN won=0 THEN pnl ELSE 0 END), 0)      AS largest_loss
        FROM live_trades
        WHERE DATE(trade_timestamp) <= ?
        """,
        (day_str,),
    )
    row = c.fetchone()
    (total, resolved, open_cnt, deployed, realized,
     win_pnl, loss_pnl, wins, res_count,
     largest_win, largest_loss) = row

    avg_win  = win_pnl  / wins                  if wins > 0              else 0
    avg_loss = loss_pnl / (res_count - wins)     if (res_count - wins) > 0 else 0
    wr       = wins / res_count                  if res_count > 0         else 0

    # Trader breakdown
    c.execute(
        """
        SELECT copied_from_name,
               COUNT(*) AS trades,
               COUNT(CASE WHEN status='RESOLVED' THEN 1 END) AS resolved,
               COALESCE(SUM(CASE WHEN won=1 THEN 1 ELSE 0 END), 0) AS wins,
               COALESCE(SUM(CASE WHEN status='RESOLVED' THEN pnl ELSE 0 END), 0) AS pnl
        FROM live_trades
        WHERE DATE(trade_timestamp) <= ?
        GROUP BY copied_from_wallet
        """,
        (day_str,),
    )
    breakdown = {}
    for r in c.fetchall():
        breakdown[r[0] or "unknown"] = {
            "trades": r[1], "resolved": r[2], "wins": r[3], "pnl": round(r[4], 2)
        }

    c.execute(
        """
        INSERT OR REPLACE INTO daily_performance (
            date, trades_executed, trades_resolved, trades_open,
            capital_deployed, realized_pnl,
            win_rate, avg_win, avg_loss, largest_win, largest_loss,
            trader_breakdown, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
        """,
        (
            day_str, total, resolved, open_cnt, round(deployed, 2),
            round(realized, 2), round(wr, 4),
            round(avg_win, 2), round(avg_loss, 2),
            round(largest_win, 2), round(largest_loss, 2),
            json.dumps(breakdown),
        ),
    )
    conn.commit()
    conn.close()


# ── Reporte ───────────────────────────────────────────────────────────────────

def generate_report(days: int = 7) -> str:
    """
    Genera un resumen de los últimos `days` días.
    Devuelve el texto del reporte.
    """
    conn = database.get_connection()
    c = conn.cursor()

    c.execute(
        """
        SELECT date, trades_executed, trades_resolved, realized_pnl, win_rate
        FROM daily_performance
        ORDER BY date DESC LIMIT ?
        """,
        (days,),
    )
    daily_rows = c.fetchall()

    c.execute(
        """
        SELECT market_question, outcome_bought, entry_price, pnl, won,
               copied_from_name, resolved_timestamp
        FROM live_trades
        WHERE status='RESOLVED'
        ORDER BY resolved_timestamp DESC LIMIT 20
        """,
    )
    recent = c.fetchall()

    c.execute(
        """
        SELECT market_question, outcome_bought, entry_price, position_size,
               copied_from_name, trade_timestamp
        FROM live_trades WHERE status='OPEN'
        ORDER BY trade_timestamp DESC
        """,
    )
    open_pos = c.fetchall()

    # Totals
    c.execute(
        "SELECT COUNT(*), SUM(pnl), SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) "
        "FROM live_trades WHERE status='RESOLVED'"
    )
    tot_row = c.fetchone()
    conn.close()

    lines = [
        "=" * 70,
        f"P&L REPORT — {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC",
        "=" * 70,
        "",
    ]

    if tot_row and tot_row[0]:
        tot_trades, tot_pnl, tot_wins = tot_row
        wr = tot_wins / tot_trades * 100 if tot_trades else 0
        lines += [
            f"ALL TIME: {tot_trades} resolved | WR {wr:.1f}% | Net PnL {tot_pnl:+.2f} USDC",
            "",
        ]

    if daily_rows:
        lines.append("DAILY SUMMARY (last days):")
        lines.append(f"  {'Date':<12} {'Trades':>6} {'Resolved':>8} {'PnL':>10} {'WR':>6}")
        lines.append("  " + "-" * 46)
        for dr in daily_rows:
            wr_s = f"{dr[4]*100:.0f}%" if dr[4] is not None else "—"
            lines.append(
                f"  {dr[0]:<12} {dr[1]:>6} {dr[2]:>8} {dr[3]:>+10.2f} {wr_s:>6}"
            )
        lines.append("")

    if recent:
        lines.append("LAST 20 RESOLVED TRADES:")
        for t in recent:
            tag = "WIN " if t[4] else "LOSS"
            lines.append(
                f"  [{tag}] {t[0][:55]:<55} | {t[1]} @{t[2]:.2f} | pnl {t[3]:+.2f} | {t[5]}"
            )
        lines.append("")

    if open_pos:
        lines.append(f"OPEN POSITIONS ({len(open_pos)}):")
        for p in open_pos:
            lines.append(
                f"  {p[0][:55]:<55} | {p[1]} @{p[2]:.2f} | {p[3]:.2f} USDC | {p[4]}"
            )
        lines.append("")

    text = "\n".join(lines)
    print(text)
    return text


# ── Script directo ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_pnl_tables()
    generate_report()
