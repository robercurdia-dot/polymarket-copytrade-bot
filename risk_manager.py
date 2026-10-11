import config
import database
from datetime import datetime

def check_daily_loss_limit(max_loss_usdc=40):
    """L36 (2026-06-01) — subido $10 → $40.
    El default $10 era para tamaño $5 sin escalado y vetó el bot tras 6 SLs
    legítimos hoy ($19.74 gross loss), aunque el NET diario era +$4.12.
    A nuestra escala actual ($5/trade, 6-8 trades/día) los SLs típicos
    consumen $2-5 cada uno; $40 da margen de 8 SLs antes de pausar.
    Sigue vigilando estampidas reales (>$40 brutas en un día = revisión)."""
    conn = database.get_connection()
    c = conn.cursor()
    today = datetime.now().strftime("%Y-%m-%d")
    c.execute("""SELECT SUM(pnl_usdc) FROM bot_trades
                 WHERE DATE(executed_at)=? AND pnl_usdc < 0""",
              (today,))
    result = c.fetchone()[0] or 0
    conn.close()
    if abs(result) >= max_loss_usdc:
        print("LIMITE DIARIO ALCANZADO: " + str(round(abs(result))) + " USDC perdidos hoy")
        return False
    return True

def check_open_positions(agent_id=None):
    """Verifica posiciones abiertas.
    Para agentes distintos de DEF_COPY usa MAX_OPEN_POSITIONS global.

    NOTA L81 (2026-06-10): el circuit breaker per-DEF_COPY (REAL_COPY_MAX_OPEN)
    se eliminó de can_trade() — la lógica delta $300 en real_signal_router es
    la protección principal. Esta función ya NO está en el hot path de can_trade
    para DEF_COPY. Se mantiene para diagnósticos/Telegram solamente.
    """
    conn = database.get_connection()
    c = conn.cursor()
    # General: todos los agentes reales
    c.execute("""SELECT COUNT(*) FROM bot_trades
                 WHERE paper_trade=0 AND status='open'""")
    count = c.fetchone()[0]
    conn.close()
    if count >= config.MAX_OPEN_POSITIONS:
        print("MAX POSICIONES ABIERTAS: " + str(count))
        return False
    return True

def check_trade_size(size_usdc):
    if size_usdc > config.MAX_TRADE_SIZE_USDC:
        print("TRADE BLOQUEADO: " + str(size_usdc) + " supera max " + str(config.MAX_TRADE_SIZE_USDC))
        return False
    return True

def can_trade(size_usdc, agent_id=None):
    """
    Verificación completa pre-trade. Devuelve (bool, motivo).

    Orden de checks:
      1. Veto del Risk Manager Fase 2.5 (prioridad máxima — siempre se evalúa
         incluso en PAPER_TRADING). Razón: si hay drawdown/manual pause activos
         el sistema debe parar TODO trading (paper o real), no solo real.
      2. Si PAPER_TRADING → permitido (no toca CLOB).
      3. Checks de trading real: daily loss, max open, trade size.
    """
    # 1. Veto del Risk Manager Fase 2.5 (prioridad máxima)
    has_v, veto_type, countdown = has_veto()
    if has_v:
        cd = f" (countdown={countdown}s)" if countdown else " (permanente)"
        return False, f"veto_{veto_type}{cd}"

    # 2. Modo paper → permitido sin más checks
    # L81 FIX: hot-reload para coherencia con executor. config.PAPER_TRADING
    # es el valor de startup; si el usuario activa real via .env sin reiniciar,
    # este check lo detecta correctamente.
    try:
        from pathlib import Path as _Path
        from dotenv import load_dotenv as _ldenv
        import os as _os2
        _ldenv(dotenv_path=_Path(__file__).resolve().parent / ".env", override=True)
        _paper_live = _os2.getenv("PAPER_TRADING", "true").strip().lower() != "false"
    except Exception:
        _paper_live = config.PAPER_TRADING
    if _paper_live:
        return True, "paper_mode"

    # 3. Checks de trading real
    if agent_id == "DEF_COPY":
        import os as _os
        from pathlib import Path as _Path2
        from dotenv import load_dotenv as _ldenv
        _ldenv(dotenv_path=_Path2(__file__).resolve().parent / ".env", override=True)
        # Cap de tamaño por trade
        def_max = float(_os.getenv('DEFINITIVE_MAX_PER_TRADE', '20'))
        if size_usdc > def_max:
            return False, "trade_size_exceeded"
        # C-1 FIX (2026-06-10): DEF_COPY también respeta daily loss limit.
        # Antes DEF_COPY lo saltaba completamente — el bot podía seguir
        # abriendo reales aunque ya hubiera quemado $40 en el día.
        _def_loss_limit = float(_os.getenv('DEF_COPY_DAILY_LOSS_LIMIT', '60'))
        if not check_daily_loss_limit(max_loss_usdc=_def_loss_limit):
            return False, "daily_loss_limit"
    elif not check_trade_size(size_usdc):
        return False, "trade_size_exceeded"

    return True, "ok"

def get_status():
    conn = database.get_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM bot_trades WHERE outcome = 'open'")
    open_pos = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM bot_trades WHERE paper_trade = 0")
    real_trades = c.fetchone()[0]
    c.execute("SELECT SUM(pnl_usdc) FROM bot_trades WHERE paper_trade = 0")
    total_pnl = c.fetchone()[0] or 0
    c.execute("SELECT COUNT(*) FROM detected_trades WHERE action_taken = 'copied'")
    copied = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM detected_trades WHERE action_taken = 'ignored'")
    ignored = c.fetchone()[0]
    conn.close()
    print("=" * 40)
    print("ESTADO DEL BOT")
    print("Posiciones abiertas: " + str(open_pos))
    print("Trades reales: " + str(real_trades))
    print("PNL total: " + str(round(total_pnl, 2)) + " USDC")
    print("Copiados: " + str(copied) + " | Ignorados: " + str(ignored))
    print("Mode: " + ("PAPER" if config.PAPER_TRADING else "REAL"))
    print("=" * 40)

# ═════════════════════════════════════════════════════════════════════
# Fase 2.5 — Circuit breakers REALES
# ═════════════════════════════════════════════════════════════════════
# Reemplaza los stubs antiguos. 4 circuit breakers, evaluados en orden:
#   1. DRAWDOWN     → permanente · -(TRADE_SIZE × DRAWDOWN_MULTIPLIER)
#   2. MANUAL_PAUSE → permanente · activado por /pause en Telegram
#   3. LOW_BALANCE  → permanente · balance wallet < MIN_WALLET_BALANCE_USDC
#   4. API_ERRORS   → 1h        · >MAX_API_ERRORS_HOUR errores en última hora
#
# Persistencia: tabla risk_events. resolved_at IS NULL → veto activo.
# Vetos PERMANENTES (veto_duration_seconds=0): solo se liberan manualmente.
# Vetos TEMPORALES: expiran cuando timestamp + duration < now.
#
# Decisión explícita de diseño (2026-05-07):
#   NO incluir MAX_CONSECUTIVE_LOSSES — incompatible con la asimetría de
#   payoff actual (TP 1.8× / SL 0.8× → reward/risk = 4.0). Las rachas de
#   losses son comportamiento normal del sistema.
#   NO incluir MAX_DAILY_LOSS_USDC fijo — la regla del multiplicador lo
#   reemplaza y es proporcional al tamaño de trade activo.

import json
from datetime import datetime, timedelta

# In-memory tracker de errores API (reseteo por reinicio del bot, OK porque
# el contador es de "última hora" y los persistentes pueden saturar la BD).
_api_error_log: list[float] = []  # epoch seconds


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def get_session_pnl_net() -> float:
    """
    PnL net acumulado de la sesión. Definimos "sesión" como:
    desde el último DRAWDOWN o MANUAL_PAUSE liberado vía /resume,
    o desde el primer paper_trade si no hay reseteo previo.

    Suma de paper_trades.realized_pnl_net (cerradas) en la sesión actual.
    """
    conn = database.get_connection()
    c = conn.cursor()
    # Último evento DRAWDOWN o MANUAL_PAUSE resuelto → marca el start de la sesión actual
    c.execute("""SELECT resolved_at FROM risk_events
                 WHERE event_type IN ('DRAWDOWN','MANUAL_PAUSE')
                   AND resolved_at IS NOT NULL
                 ORDER BY resolved_at DESC LIMIT 1""")
    row = c.fetchone()
    session_start = row[0] if row else None

    if session_start:
        c.execute("""SELECT COALESCE(SUM(realized_pnl_net), 0) FROM paper_trades
                     WHERE status != 'open' AND exit_at >= ?""", (session_start,))
    else:
        c.execute("""SELECT COALESCE(SUM(realized_pnl_net), 0) FROM paper_trades
                     WHERE status != 'open'""")
    paper_pnl = c.fetchone()[0] or 0.0

    # C-2 FIX (2026-06-10): incluir PnL REAL de bot_trades en el drawdown engine.
    # Antes solo leía paper_trades → el circuito de drawdown era ciego a pérdidas
    # reales de DEF_COPY. Ahora suma pnl_usdc de bot_trades cerrados (paper_trade=0).
    if session_start:
        c.execute("""SELECT COALESCE(SUM(pnl_usdc), 0) FROM bot_trades
                     WHERE paper_trade=0 AND pnl_usdc IS NOT NULL
                       AND status IN ('closed') AND executed_at >= ?""", (session_start,))
    else:
        c.execute("""SELECT COALESCE(SUM(pnl_usdc), 0) FROM bot_trades
                     WHERE paper_trade=0 AND pnl_usdc IS NOT NULL
                       AND status IN ('closed')""")
    real_pnl = c.fetchone()[0] or 0.0
    conn.close()
    return round(float(paper_pnl) + float(real_pnl), 2)


def _drawdown_threshold() -> float:
    """Umbral negativo: -(TRADE_SIZE × DRAWDOWN_MULTIPLIER)."""
    trade_size = config.PAPER_SIZE_USDC if not config.PAPER_TRADING else config.PAPER_SIZE_USDC
    return -(trade_size * config.DRAWDOWN_MULTIPLIER)


def _persist_event(event_type: str, trigger_value: float | None,
                   threshold: float | None, duration_seconds: int = 0,
                   context: dict | None = None) -> int:
    """Inserta evento de risk. duration_seconds=0 → permanente."""
    conn = database.get_connection()
    c = conn.cursor()
    c.execute("""INSERT INTO risk_events
        (timestamp, event_type, trigger_value, threshold,
         veto_duration_seconds, context_json)
        VALUES (?, ?, ?, ?, ?, ?)""",
        (_now_iso(), event_type, trigger_value, threshold,
         duration_seconds, json.dumps(context or {}, default=str)))
    eid = c.lastrowid
    conn.commit()
    conn.close()
    return eid


def _active_event_of(event_type: str) -> dict | None:
    """Devuelve el evento activo más reciente de un tipo, o None."""
    conn = database.get_connection()
    c = conn.cursor()
    c.execute("""SELECT id, timestamp, event_type, trigger_value, threshold,
                        veto_duration_seconds, resolved_at, context_json
                 FROM risk_events
                 WHERE event_type = ? AND resolved_at IS NULL
                 ORDER BY id DESC LIMIT 1""", (event_type,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    duration = row[5] or 0
    if duration > 0:
        # Temporal: comprobar si ya expiró
        try:
            ts = datetime.fromisoformat(row[1])
            if (ts + timedelta(seconds=duration)) < datetime.utcnow():
                # Auto-resolución
                _resolve_event(row[0], reason="auto_expired")
                return None
        except Exception:
            pass
    return {
        "id": row[0], "timestamp": row[1], "event_type": row[2],
        "trigger_value": row[3], "threshold": row[4],
        "duration_seconds": duration, "resolved_at": row[6],
        "context": json.loads(row[7] or "{}"),
    }


def _resolve_event(event_id: int, reason: str = "manual"):
    conn = database.get_connection()
    c = conn.cursor()
    c.execute("UPDATE risk_events SET resolved_at = ? WHERE id = ?",
              (_now_iso(), event_id))
    conn.commit()
    conn.close()
    print(f"[RISK] ✓ event #{event_id} resuelto ({reason})")


def _check_low_balance() -> tuple[bool, dict | None]:
    """Comprueba balance wallet contra MIN_WALLET_BALANCE_USDC.
    SOLO aplica cuando hay automatización real activa (Fase 3a en adelante).
    La flag explícita RISK_CHECK_LOW_BALANCE controla esto, NO PAPER_TRADING
    (que es legacy del executor manual de Telegram)."""
    if not getattr(config, "RISK_CHECK_LOW_BALANCE", False):
        return False, None

    active = _active_event_of("LOW_BALANCE")
    try:
        import requests
        # Chequear DW (donde vive el pUSD libre) + proxy como fallback
        addr = getattr(config, 'DEPOSIT_WALLET_ADDRESS', None) or config.POLY_WALLET_ADDRESS
        r = requests.get(f"https://data-api.polymarket.com/value?user={addr}",
                         timeout=5)
        balance = float(r.json()[0].get("value", 0)) if r.status_code == 200 else None
        # Si DW no tiene posiciones abiertas (solo pUSD libre), la API devuelve 0.
        # En ese caso usar el balance CLOB como alternativa.
        if balance is not None and balance == 0:
            try:
                r2 = requests.get(f"https://data-api.polymarket.com/value?user={config.POLY_WALLET_ADDRESS}",
                                   timeout=5)
                proxy_bal = float(r2.json()[0].get("value", 0)) if r2.status_code == 200 else 0
                balance = max(balance, proxy_bal)
            except Exception:
                pass
    except Exception:
        balance = None
    if balance is not None and balance < config.MIN_WALLET_BALANCE_USDC:
        if not active:
            _persist_event("LOW_BALANCE", trigger_value=balance,
                           threshold=config.MIN_WALLET_BALANCE_USDC,
                           duration_seconds=0,
                           context={"endpoint": "data-api/value"})
            print(f"[RISK] 🛑 LOW_BALANCE VETO PERMANENTE — balance=${balance} < ${config.MIN_WALLET_BALANCE_USDC}")
        active = _active_event_of("LOW_BALANCE")
    return (active is not None), active


def record_api_error():
    """Registra un error de API. Llamar desde catch blocks de Polymarket."""
    import time
    now = time.time()
    cutoff = now - 3600
    # Mantener solo eventos de la última hora
    _api_error_log[:] = [t for t in _api_error_log if t >= cutoff]
    _api_error_log.append(now)
    if len(_api_error_log) > config.MAX_API_ERRORS_HOUR:
        active = _active_event_of("API_ERRORS")
        if not active:
            _persist_event("API_ERRORS", trigger_value=len(_api_error_log),
                           threshold=config.MAX_API_ERRORS_HOUR,
                           duration_seconds=3600,
                           context={"recent_errors": len(_api_error_log)})
            print(f"[RISK] ⚠️  API_ERRORS VETO 1h — {len(_api_error_log)} errores en última hora")


def _check_scale_rollback() -> tuple[bool, dict | None]:
    """Kill-switch de fase de escalado.
    Si PnL real (paper_trade=0) últimas 24h <= SCALE_24H_LOSS_THRESHOLD,
    activa MANUAL_PAUSE permanente. Operador debe revisar y resetear.
    Solo aplica cuando SCALE_PHASE != "OFF"."""
    if not config.SCALE_PHASE or config.SCALE_PHASE == "OFF":
        return False, None
    active = _active_event_of("SCALE_ROLLBACK")
    if active:
        return True, active
    conn = database.get_connection()
    c = conn.cursor()
    c.execute("""SELECT COALESCE(SUM(pnl_usdc), 0) FROM bot_trades
                 WHERE paper_trade=0 AND pnl_usdc IS NOT NULL
                   AND executed_at >= datetime('now','-24 hours')""")
    pnl_24h = float(c.fetchone()[0] or 0.0)
    conn.close()
    threshold = config.SCALE_24H_LOSS_THRESHOLD
    if pnl_24h <= threshold:
        _persist_event("SCALE_ROLLBACK",
                       trigger_value=pnl_24h, threshold=threshold,
                       duration_seconds=0,
                       context={"phase": config.SCALE_PHASE,
                                "trade_size": config.MAX_TRADE_SIZE_USDC,
                                "rule": "24h_real_pnl"})
        print(f"[RISK] 🛑 SCALE ROLLBACK — pnl_24h={pnl_24h:.2f} <= {threshold} "
              f"(fase {config.SCALE_PHASE})")
        return True, _active_event_of("SCALE_ROLLBACK")
    return False, None


def _check_manual_pause() -> tuple[bool, dict | None]:
    active = _active_event_of("MANUAL_PAUSE")
    return (active is not None), active


# ── API pública ──────────────────────────────────────────────────────

def has_veto() -> tuple[bool, str | None, int | None]:
    """
    Devuelve (active, reason, countdown_seconds).
    Orden: DRAWDOWN → MANUAL_PAUSE → LOW_BALANCE → API_ERRORS.
    Disabled → siempre False.
    """
    if not config.RISK_MANAGER_ENABLED:
        return False, None, None
    # L54 (2026-06-03) — orquestador simplificado.
    # DRAWDOWN eliminado (L53 lo neutralizó por decisión del usuario).
    # API_ERRORS sin uso (nunca activado en histórico).
    # Quedan: SCALE_ROLLBACK (auto-tuner safety), MANUAL_PAUSE (Telegram),
    # LOW_BALANCE (solo si RISK_CHECK_LOW_BALANCE=true).
    for fn, name in [
        (_check_scale_rollback, "SCALE_ROLLBACK"),
        (_check_manual_pause,   "MANUAL_PAUSE"),
        (_check_low_balance,    "LOW_BALANCE"),
    ]:
        active, evt = fn()
        if active and evt:
            countdown = None
            if evt["duration_seconds"]:
                try:
                    expires = datetime.fromisoformat(evt["timestamp"]) + timedelta(seconds=evt["duration_seconds"])
                    countdown = max(0, int((expires - datetime.utcnow()).total_seconds()))
                except Exception:
                    pass
            return True, name, countdown
    return False, None, None


def get_active_vetoes() -> list[dict]:
    """Lista de vetos activos para dashboard/Telegram."""
    out = []
    for name in ("DRAWDOWN", "SCALE_ROLLBACK", "MANUAL_PAUSE", "LOW_BALANCE", "API_ERRORS"):
        evt = _active_event_of(name)
        if evt:
            countdown = None
            if evt["duration_seconds"]:
                try:
                    expires = datetime.fromisoformat(evt["timestamp"]) + timedelta(seconds=evt["duration_seconds"])
                    countdown = max(0, int((expires - datetime.utcnow()).total_seconds()))
                except Exception:
                    pass
            out.append({
                "type":       evt["event_type"],
                "since":      evt["timestamp"],
                "trigger":    evt["trigger_value"],
                "threshold":  evt["threshold"],
                "permanent":  evt["duration_seconds"] == 0,
                "countdown_s": countdown,
                "context":    evt["context"],
            })
    return out


# ── Acciones manuales (Telegram) ────────────────────────────────────

def manual_pause(reason: str = "user_request") -> int:
    """Activa MANUAL_PAUSE permanente. Devuelve event_id."""
    active = _active_event_of("MANUAL_PAUSE")
    if active:
        return active["id"]
    eid = _persist_event("MANUAL_PAUSE", trigger_value=None, threshold=None,
                         duration_seconds=0, context={"reason": reason})
    print(f"[RISK] ⏸  MANUAL_PAUSE activado (event #{eid})")
    return eid


def manual_resume() -> dict:
    """
    Libera SOLO el veto manual. Si hay otros vetos, se mantienen y se reportan.
    Devuelve dict con resultado para que /resume lo formatee.
    """
    active = _active_event_of("MANUAL_PAUSE")
    if active:
        _resolve_event(active["id"], reason="manual_resume")
    other = [v for v in get_active_vetoes() if v["type"] != "MANUAL_PAUSE"]
    return {
        "manual_released": active is not None,
        "other_vetoes":    other,
    }


def reset_drawdown_event() -> bool:
    """Libera el veto DRAWDOWN. Pensado para uso desde Telegram con confirmación
    explícita o tras intervención manual del usuario."""
    active = _active_event_of("DRAWDOWN")
    if not active:
        return False
    _resolve_event(active["id"], reason="manual_drawdown_reset")
    return True


# ── Dashboard helper ────────────────────────────────────────────────

def get_dashboard_state() -> dict:
    """Estado completo para data.risk en JSON."""
    pnl = get_session_pnl_net()
    threshold = _drawdown_threshold()
    distance = pnl - threshold  # cuánto queda hasta tocar el stop (positivo = OK)
    vetoes = get_active_vetoes()

    # Wallet balance (puede fallar)
    balance = None
    try:
        import requests
        r = requests.get(f"https://data-api.polymarket.com/value?user={config.POLY_WALLET_ADDRESS}",
                         timeout=5)
        if r.status_code == 200:
            balance = float(r.json()[0].get("value", 0))
    except Exception:
        pass

    # Conteo de risk_events recientes
    conn = database.get_connection()
    c = conn.cursor()
    cutoff_14d = (datetime.utcnow() - timedelta(days=14)).isoformat()
    c.execute("SELECT event_type, COUNT(*) FROM risk_events WHERE timestamp >= ? GROUP BY event_type",
              (cutoff_14d,))
    by_type = dict(c.fetchall())
    c.execute("SELECT id, timestamp, event_type, trigger_value, threshold, resolved_at FROM risk_events ORDER BY timestamp DESC LIMIT 20")
    history = []
    for r in c.fetchall():
        history.append({"id": r[0], "timestamp": r[1], "event_type": r[2],
                        "trigger_value": r[3], "threshold": r[4], "resolved_at": r[5]})
    conn.close()

    return {
        "enabled":           config.RISK_MANAGER_ENABLED,
        "active_vetoes":     vetoes,
        "session_pnl_net":   pnl,
        "drawdown_threshold": threshold,
        "drawdown_distance": round(distance, 2),
        "trade_size":        config.PAPER_SIZE_USDC,
        "drawdown_multiplier": config.DRAWDOWN_MULTIPLIER,
        "wallet_balance":    balance,
        "min_wallet_balance": config.MIN_WALLET_BALANCE_USDC,
        "api_errors_last_hour": len(_api_error_log),
        "max_api_errors_hour":  config.MAX_API_ERRORS_HOUR,
        "events_14d_by_type":   by_type,
        "events_14d_total":     sum(by_type.values()),
        "history":              history,
    }


if __name__ == "__main__":
    get_status()
    print("\n=== RISK STATE ===")
    import json as _json
    print(_json.dumps(get_dashboard_state(), indent=2, default=str))