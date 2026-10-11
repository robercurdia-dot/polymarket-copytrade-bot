import asyncio
import json
import sys
import threading
import time
import os
from datetime import datetime, timedelta
from http.server import HTTPServer, SimpleHTTPRequestHandler
import database
import monitor
import leaderboard_analyzer
import config
import telegram_bot
import eonet_monitor
import reputation_tracker
import executor
import trusted_scanner
import pnl_tracker
import hot_streak_scanner
import data_exporter
import whale_tracker
import my_behavior_analyzer
import paper_executor
import auto_tuner
import safety
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

# ── Dashboard HTTP server ─────────────────────────────────────────────────────
WEB_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
WEB_PORT = int(os.getenv("DASHBOARD_PORT", 8080))

class _SilentHandler(SimpleHTTPRequestHandler):
    """Sirve web/ + endpoint API mínimo para withdraw externo (Fase 4.4)."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEB_DIR, **kwargs)
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _mismo_origen(self):
        # La IP local no basta: cualquier web abierta en el navegador también llega
        # desde 127.0.0.1. Exigir Host local (anti DNS rebinding), Origin propio
        # y JSON (fuerza preflight CORS, que aquí nunca se concede).
        locales = {f"localhost:{WEB_PORT}", f"127.0.0.1:{WEB_PORT}", f"[::1]:{WEB_PORT}"}
        if self.headers.get("Host", "") not in locales:
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin.removeprefix("http://") not in locales:
            return False
        return self.headers.get("Content-Type", "").split(";")[0].strip() == "application/json"

    def do_POST(self):
        if self.path == "/api/withdraw":
            return self._handle_withdraw()
        self._send_json(404, {"ok": False, "msg": "endpoint not found"})

    def _handle_withdraw(self):
        # Seguridad: solo aceptar desde localhost
        client_ip = self.client_address[0]
        if client_ip not in ("127.0.0.1", "::1", "localhost"):
            return self._send_json(403, {"ok": False,
                                         "msg": f"withdraw only allowed from localhost (got {client_ip})"})
        if not self._mismo_origen():
            return self._send_json(403, {"ok": False,
                                         "msg": "withdraw only allowed from the local dashboard"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            data = json.loads(raw)
        except Exception as e:
            return self._send_json(400, {"ok": False, "msg": f"bad json: {e}"})

        to = data.get("to")
        amount = data.get("amount")
        asset = data.get("asset", "pUSD")
        confirm = data.get("confirm", False)
        if not to or amount is None:
            return self._send_json(400, {"ok": False,
                                         "msg": "missing 'to' or 'amount'"})
        try:
            amount = float(amount)
        except Exception:
            return self._send_json(400, {"ok": False, "msg": "amount must be number"})

        # Ejecutar withdraw (dry_run si no confirm)
        try:
            import importlib
            wm = importlib.import_module("tools.withdraw_external")
            res = wm.withdraw_external(to=to, amount=amount, asset=asset,
                                        dry_run=(not confirm))
            print(f"[WITHDRAW] {asset} {amount} → {to[:12]}…  confirm={confirm}  result={res.get('ok')} msg={res.get('msg','')}")
            code = 200 if res.get("ok") else 400
            return self._send_json(code, res)
        except Exception as e:
            print(f"[WITHDRAW] exception: {e}")
            return self._send_json(500, {"ok": False, "msg": f"server error: {e}"})

def _start_web_server():
    try:
        server = HTTPServer(("0.0.0.0", WEB_PORT), _SilentHandler)
        print(f"[WEB] 🌐 Dashboard en http://localhost:{WEB_PORT}/dashboard.html")
        server.serve_forever()
    except OSError as e:
        print(f"[WEB] ⚠️  No se pudo arrancar el servidor web en puerto {WEB_PORT}: {e}")

# ── Telegram bot (handler de callbacks) ──────────────────────────────────────
_telegram_started = False   # guard contra doble arranque

async def run_telegram_bot(stop_event):
    global _telegram_started
    if _telegram_started:
        print("[TELEGRAM] ⚠️ Instancia duplicada detectada — ignorando segunda llamada")
        await stop_event.wait()
        return
    _telegram_started = True

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CallbackQueryHandler(telegram_bot.handle_callback))
    app.add_handler(CommandHandler("stats",          telegram_bot.stats_command))
    app.add_handler(CommandHandler("logtrade",       telegram_bot.logtrade_command))
    app.add_handler(CommandHandler("trades",         telegram_bot.trades_command))
    app.add_handler(CommandHandler("traderesult",    telegram_bot.traderesult_command))
    app.add_handler(CommandHandler("tradepatterns",  telegram_bot.tradepatterns_command))
    app.add_handler(CommandHandler("pause",          telegram_bot.pause_command))
    app.add_handler(CommandHandler("resume",         telegram_bot.resume_command))
    app.add_handler(CommandHandler("risk_status",    telegram_bot.risk_status_command))
    await app.initialize()
    await app.start()
    # drop_pending_updates evita el 409 Conflict al reiniciar antes de que
    # Telegram cierre la conexión anterior (~60 s de timeout en su lado).
    await app.updater.start_polling(
        drop_pending_updates=True,
        allowed_updates=["callback_query", "message"]
    )
    print("[TELEGRAM] ✅ Bot escuchando callbacks + comandos (drop_pending_updates=True)")
    await stop_event.wait()
    await app.updater.stop()
    await app.stop()
    await app.shutdown()
    _telegram_started = False

# ── Weekly review ─────────────────────────────────────────────────────────────

def weekly_review():
    """
    Ejecuta cada lunes a las 00:00.
    1. Recalcula scores de todos los traders.
    2. Desactiva wallets con win_rate < 0.75 en las últimas 4 semanas.
    3. Actualiza elite_traders por nicho.
    """
    print("=" * 50)
    print("WEEKLY REVIEW — " + datetime.now().strftime("%Y-%m-%d %H:%M"))
    print("=" * 50)

    cutoff = (datetime.now() - timedelta(weeks=4)).isoformat()

    conn = database.get_connection()
    c = conn.cursor()

    # Wallets con actividad reciente
    c.execute("SELECT address, win_rate, insider_score FROM wallets WHERE active = 1")
    wallets = c.fetchall()

    deactivated = 0
    for address, win_rate, score in wallets:
        # Verificar rendimiento reciente en bot_trades
        c.execute("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN pnl_usdc > 0 THEN 1 ELSE 0 END) as wins
            FROM bot_trades
            WHERE detected_trade_id IN (
                SELECT id FROM detected_trades WHERE wallet_address = ?
            )
            AND paper_trade = 0
            AND executed_at >= ?
            AND pnl_usdc != 0
        """, (address, cutoff))
        row = c.fetchone()
        total_recent, wins_recent = row[0], row[1] or 0

        # L37 (2026-06-01) — añadido GUARD adicional: NUNCA desactivar wallets
        # de whitelist real, sin importar el sample/WR. La lógica original
        # asume traders descubiertos al vuelo; nuestras whitelisted están
        # curadas manualmente. Si una empezara a sangrar, lo detectamos por
        # PnL real, no por win_rate de bot_trades (que mezcla cierres TP/SL
        # con la tesis del trader). Incidente que motivó esto: 1 jun 00:01,
        # weekly_review desactivó dbdd/Lilybaeum/swisstony de golpe.
        # 2026-05-28 — sample mínimo subido 5→10 tras incidente previo.
        try:
            real_whitelist = set()
            for envvar in ("REAL_WALLET_WHITELIST", "REAL_D_WALLET_WHITELIST"):
                raw = os.getenv(envvar, "") or ""
                real_whitelist |= {a.strip().lower() for a in raw.split(",") if a.strip()}
        except Exception:
            real_whitelist = set()
        if address.lower() in real_whitelist:
            # NO desactivar — wallet curada manualmente
            continue
        if total_recent >= 10:
            recent_wr = wins_recent / total_recent
            if recent_wr < config.MIN_WIN_RATE:
                c.execute("UPDATE wallets SET active = 0 WHERE address = ?", (address,))
                deactivated += 1
                print(f"  Desactivada: {address[:10]}... "
                      f"win_rate_reciente={round(recent_wr*100)}% ({total_recent} trades) "
                      f"< {round(config.MIN_WIN_RATE*100)}%")

    conn.commit()
    conn.close()

    print(f"Wallets desactivadas: {deactivated}")
    print("Recalculando leaderboard y elite_traders...")
    leaderboard_analyzer.run_analysis(200)
    print("Weekly review completada.")

async def weekly_behavior_report():
    """Envía el reporte de aprendizaje de Claude cada lunes junto con el weekly review."""
    try:
        import behavior_analyzer
        await behavior_analyzer.generate_weekly_report()
    except Exception as e:
        print(f"[LEARN] Error en weekly behavior report: {e}")

async def weekly_review_loop():
    """Duerme hasta el próximo lunes 00:00 y ejecuta weekly_review en bucle."""
    while True:
        now = datetime.now()
        # days_until_monday: 0=lunes ... 6=domingo
        days_until_monday = (7 - now.weekday()) % 7
        if days_until_monday == 0 and now.hour == 0 and now.minute < 5:
            # Estamos en el slot de lunes 00:00 — ejecutar y dormir 1 semana
            weekly_review()
            await weekly_behavior_report()
            await asyncio.sleep(7 * 24 * 3600)
        else:
            # Calcular segundos hasta el próximo lunes 00:00
            if days_until_monday == 0:
                days_until_monday = 7  # ya pasó el slot de hoy
            next_monday = (now + timedelta(days=days_until_monday)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            sleep_secs = (next_monday - now).total_seconds()
            print(f"Weekly review programada para {next_monday.strftime('%Y-%m-%d %H:%M')} "
                  f"({round(sleep_secs/3600, 1)}h)")
            await asyncio.sleep(sleep_secs)

# ── Credentials refresh ───────────────────────────────────────────────────────

async def experiment_final_report_loop():
    """
    Vigila la duración del experimento (Fase 2.6). Cuando se cumplan
    los 3 días Y haya sample suficiente (≥150 cerrados A y B, ≥50 C),
    dispara `tools/experiment_final_report.py` UNA SOLA VEZ. El reporte
    se persiste en `reports/experiment_final_<ts>.md` y se loguea.
    Marca `reports/.experiment_final_done` para no repetir.
    """
    import subprocess
    from pathlib import Path

    ROOT = Path(__file__).resolve().parent
    DONE_FLAG = ROOT / "reports" / ".experiment_final_done"
    SCRIPT = ROOT / "tools" / "experiment_final_report.py"
    DURATION_TARGET_DAYS = 3
    MIN_AB = 150
    MIN_C = 50

    print("[FINAL-REPORT] Loop iniciado · check cada 30min")
    while True:
        try:
            if DONE_FLAG.exists():
                # Ya emitido, dormir 1h y seguir vigilando
                await asyncio.sleep(3600)
                continue

            conn = database.get_connection()
            c = conn.cursor()
            c.execute("SELECT MIN(entry_at) FROM paper_trades WHERE agent_id != 'A_legacy'")
            started = (c.fetchone() or [None])[0]
            if not started:
                conn.close()
                await asyncio.sleep(1800)
                continue
            c.execute("""SELECT agent_id, COUNT(*) FROM paper_trades
                         WHERE agent_id != 'A_legacy' AND status != 'open'
                         GROUP BY agent_id""")
            closed_by_agent = dict(c.fetchall())
            conn.close()

            try:
                started_dt = datetime.fromisoformat(started)
            except Exception:
                started_dt = None
            now = datetime.utcnow()
            duration_days = (now - started_dt).total_seconds() / 86400 if started_dt else 0

            ok_dur = duration_days >= DURATION_TARGET_DAYS
            ok_a   = closed_by_agent.get("A", 0) >= MIN_AB
            ok_b   = closed_by_agent.get("B", 0) >= MIN_AB
            ok_c   = closed_by_agent.get("C", 0) >= MIN_C

            if ok_dur and ok_a and ok_b and ok_c:
                print(f"[FINAL-REPORT] Criterios cumplidos "
                      f"(dur={duration_days:.2f}d, "
                      f"A={closed_by_agent.get('A',0)}, "
                      f"B={closed_by_agent.get('B',0)}, "
                      f"C={closed_by_agent.get('C',0)}). Lanzando reporte...")
                ts = now.strftime("%Y%m%d-%H%M")
                out_path = ROOT / "reports" / f"experiment_final_{ts}.md"
                try:
                    venv_py = ROOT / "venv" / "bin" / "python"
                    py_exec = str(venv_py) if venv_py.exists() else sys.executable
                    res = subprocess.run(
                        [py_exec, str(SCRIPT), "--markdown-only", "-o", str(out_path)],
                        cwd=str(ROOT),
                        capture_output=True, text=True, timeout=120,
                    )
                    if res.returncode == 0:
                        DONE_FLAG.write_text(f"{ts}\n{out_path}\n")
                        print(f"[FINAL-REPORT] ✅ Generado: {out_path}")
                        print(f"[FINAL-REPORT] (flag de control: {DONE_FLAG})")
                    else:
                        print(f"[FINAL-REPORT] ❌ subprocess error rc={res.returncode}")
                        print(f"[FINAL-REPORT] stderr: {res.stderr[:500]}")
                except Exception as e:
                    print(f"[FINAL-REPORT] ❌ exception: {e}")
            else:
                print(f"[FINAL-REPORT] Aún no: "
                      f"dur={duration_days:.2f}d (≥{DURATION_TARGET_DAYS}? {ok_dur}) · "
                      f"A={closed_by_agent.get('A',0)}/{MIN_AB} · "
                      f"B={closed_by_agent.get('B',0)}/{MIN_AB} · "
                      f"C={closed_by_agent.get('C',0)}/{MIN_C}")
        except Exception as e:
            print(f"[FINAL-REPORT] error en loop: {e}")
        await asyncio.sleep(1800)  # 30 min


async def db_backup_loop():
    """Backup horario de la BD a backups/. Mantiene 48 horarios + 30 diarios.
    Creado tras el incidente 2026-05-04 (DELETE accidental sin backup)."""
    import shutil, glob
    from pathlib import Path
    backup_dir = Path("backups")
    backup_dir.mkdir(exist_ok=True)
    while True:
        try:
            now = datetime.now()
            stamp = now.strftime("%Y%m%d-%H")
            dest = backup_dir / f"polymarket_insider.{stamp}.db"
            if not dest.exists():
                shutil.copy2("polymarket_insider.db", dest)
                # Rotación: limpiar > 48h (mantenemos los daily 00h aparte)
                cutoff_hourly = now - timedelta(hours=48)
                cutoff_daily  = now - timedelta(days=30)
                for f in backup_dir.glob("polymarket_insider.*.db"):
                    try:
                        # Parse YYYYMMDD-HH
                        ts = datetime.strptime(f.stem.split(".")[1], "%Y%m%d-%H")
                        is_daily_00 = ts.hour == 0
                        if is_daily_00:
                            if ts < cutoff_daily: f.unlink()
                        else:
                            if ts < cutoff_hourly: f.unlink()
                    except Exception:
                        continue
                size_mb = dest.stat().st_size / (1024*1024)
                print(f"[BACKUP] 💾 {dest.name} ({size_mb:.1f} MB)")
        except Exception as e:
            print(f"[BACKUP] error: {e}")
        await asyncio.sleep(3600)  # cada hora


async def paper_monitor_loop():
    """Revisa cada PAPER_CHECK_INTERVAL_SEC los paper_trades abiertos:
    aplica TP/SL, resuelve cuando el mercado cierra, marca timeout."""
    interval = config.PAPER_CHECK_INTERVAL_SEC
    print(f"[PAPER] Loop iniciado (intervalo={interval}s · "
          f"TP={config.PAPER_TP_MULTIPLIER}× · SL={config.PAPER_SL_MULTIPLIER}× · "
          f"timeout={config.PAPER_TIMEOUT_DAYS}d)")
    while True:
        try:
            stats = paper_executor.check_open_paper_trades()
            if stats.get("closed", 0) > 0:
                print(f"[PAPER] Loop: checked={stats['checked']} "
                      f"tp_hit={stats.get('tp_hit',0)} "
                      f"sl_hit={stats.get('sl_hit',0)} "
                      f"resolved_win={stats.get('resolved_win',0)} "
                      f"resolved_loss={stats.get('resolved_loss',0)} "
                      f"timeout={stats.get('timeout',0)}")
        except Exception as e:
            print(f"[PAPER] Error en paper_monitor_loop: {e}")
        await asyncio.sleep(interval)


async def paper_b_fast_loop():
    """Fase L28 (2026-05-30) — paper B a misma cadencia que fast_price_check
    real (15s por defecto). Fix de divergencia paper-tp_hit vs real-sl_hit
    que ocurre cuando el precio dipea y rebota entre dos checks lentos del
    paper loop (60s). El paper de B es nuestro mirror, debe ver lo mismo
    que el real para que su PnL sea predictivo."""
    interval = int(os.getenv("PAPER_CHECK_INTERVAL_B_SEC",
                             os.getenv("B_PRICE_CHECK_INTERVAL_SECONDS", "15")))
    print(f"[PAPER_B] Loop rápido iniciado (cada {interval}s, solo agente B)")
    while True:
        try:
            stats = await asyncio.get_event_loop().run_in_executor(
                None, lambda: paper_executor.check_open_paper_trades(only_agent="B"))
            if stats and stats.get("closed", 0) > 0:
                print(f"[PAPER_B] tp={stats.get('tp_hit',0)} "
                      f"sl={stats.get('sl_hit',0)} "
                      f"resolved_w={stats.get('resolved_win',0)} "
                      f"timeout={stats.get('timeout',0)}")
        except Exception as e:
            print(f"[PAPER_B] error: {e}")
        await asyncio.sleep(interval)


async def definitive_copy_loop():
    """L69 (2026-06-04) — vigila SELLs de Lily + Swisstony y cierra DEF_COPY opens.
    L87 (2026-06-06) — intervalo 3s para gap señal→cierre <3s."""
    import os as _os
    interval = float(_os.getenv('DEF_COPY_INTERVAL', '3'))
    print(f"[DEF_COPY] Loop iniciado (cada {interval}s · Lily + Swisstony)")
    import definitive_copy_watcher
    while True:
        try:
            stats = await asyncio.get_event_loop().run_in_executor(
                None, definitive_copy_watcher.check_definitive_copy_exits)
            # L81 FIX: la función devuelve 'closed_sell'+'closed_merge', no 'closed'
            if stats:
                _tot = stats.get("closed_sell", 0) + stats.get("closed_merge", 0)
                if _tot > 0:
                    print(f"[DEF_COPY] closed={_tot} "
                          f"(sell={stats['closed_sell']} merge={stats['closed_merge']}) "
                          f"de {stats['checked']} opens")
        except Exception as e:
            print(f"[DEF_COPY] error: {e}")
        await asyncio.sleep(interval)


async def reconcile_real_trades_loop():
    """Fase 3.2 — Reconcilia fills reales contra /trades del CLOB.
    Intervalo: RECONCILE_INTERVAL_SECONDS env (default 60s, antes 300s)."""
    interval = int(os.getenv("RECONCILE_INTERVAL_SECONDS", "60"))
    print(f"[RECONCILE] Loop iniciado (cada {interval}s)")
    while True:
        try:
            stats = await asyncio.get_event_loop().run_in_executor(
                None, executor.reconcile_real_trades)
            if stats and stats.get("reconciled", 0) > 0:
                print(f"[RECONCILE] {stats['reconciled']}/{stats['checked']} reconciliados")
        except Exception as e:
            print(f"[RECONCILE] Error en loop: {e}")
        await asyncio.sleep(interval)


async def poll_pending_sells_loop():
    """Fase L18 (2026-05-24) — polling activo de SELLs posteadas.
    Cada 30s consulta CLOB get_order sobre las LIMIT SELLs que el fallback
    posteó. Cierre inmediato cuando filla, sin esperar grace period ni
    activity API. Evita el bug recurrente DB-desincronizada-con-on-chain."""
    interval = int(os.getenv("POLL_SELL_INTERVAL_SECONDS", "30"))
    print(f"[POLL_SELL] Loop iniciado (cada {interval}s)")
    while True:
        try:
            stats = await asyncio.get_event_loop().run_in_executor(
                None, executor.poll_pending_sells)
            if stats and (stats.get("filled", 0) + stats.get("stale", 0)) > 0:
                print(f"[POLL_SELL] checked={stats['checked']} "
                      f"filled={stats['filled']} stale={stats['stale']} "
                      f"still_live={stats['still_live']}")
        except Exception as e:
            print(f"[POLL_SELL] Error en loop: {e}")
        await asyncio.sleep(interval)


async def safety_health_check_loop():
    """Fase 4.6 — sweep defensivo cada 5 min.
    Detecta posiciones fantasma (open en DB pero 0 shares on-chain) y las
    reconcilia desde Polymarket activity API. Previene 'agujero negro v2'
    donde un fill 'delayed' pasaba sin registrarse."""
    interval = int(os.getenv("SAFETY_HEALTH_INTERVAL_SECONDS", "300"))
    print(f"[SAFETY] Health-check loop iniciado (cada {interval}s)")
    while True:
        try:
            stats = await asyncio.get_event_loop().run_in_executor(
                None, lambda: safety.health_check_open_trades(notify=True))
            if stats and stats.get("phantom", 0) > 0:
                print(f"[SAFETY] phantom={stats['phantom']} "
                      f"rescued={stats['rescued']} errors={stats['errors']}")
        except Exception as e:
            print(f"[SAFETY] Error en loop: {e}")
        await asyncio.sleep(interval)


async def diagnostics_loop():
    """Fase L27 (2026-05-29) — suite automática de chequeos de salud.
    Ejecuta `tools/diagnostics.run_all(notify=True)` cada DIAGNOSTICS_INTERVAL_SECONDS
    (default 1800s = 30 min). Alerta Telegram si algún check falla.
    Previene incidentes silenciosos como L24 (size_usdc=0), L26 (wallet top
    desactivada por sample chico) o L27 (REDEEMs ocultando BUYs)."""
    interval = int(os.getenv("DIAGNOSTICS_INTERVAL_SECONDS", "1800"))
    print(f"[DIAGNOSTICS] Loop iniciado (cada {interval}s)")
    # primera pasada con 60s de gracia para que el bot termine de arrancar
    await asyncio.sleep(10)
    while True:
        try:
            from tools import diagnostics
            summary = await asyncio.get_event_loop().run_in_executor(
                None, lambda: diagnostics.run_all(notify=True))
            fails = summary.get("fails", 0) if summary else 0
            warns = summary.get("warns", 0) if summary else 0
            if fails > 0 or warns > 0:
                print(f"[DIAGNOSTICS] fails={fails} warns={warns} "
                      f"checks={summary.get('total', 0)}")
        except Exception as e:
            print(f"[DIAGNOSTICS] error en loop: {e}")
        await asyncio.sleep(interval)


async def divergence_monitor_loop():
    """Fase 4.7 — monitor de divergencia paper vs real (cada 30 min).
    Si gap medio supera SAFETY_DIVERGENCE_THRESHOLD_PP (default 25 pp)
    sobre últimas 20 ventas cerradas, envía alerta Telegram."""
    interval = int(os.getenv("DIVERGENCE_MONITOR_INTERVAL_SECONDS", "1800"))
    window   = int(os.getenv("DIVERGENCE_MONITOR_WINDOW", "20"))
    thr      = float(os.getenv("DIVERGENCE_MONITOR_THRESHOLD_PP", "25"))
    print(f"[DIVERGENCE] Monitor loop iniciado (cada {interval}s, "
          f"window={window}, threshold={thr}pp)")
    while True:
        try:
            stats = await asyncio.get_event_loop().run_in_executor(
                None, lambda: safety.divergence_monitor(
                    window=window, alert_threshold_pp=thr, notify=True))
            if stats and stats.get("n", 0) > 0:
                print(f"[DIVERGENCE] n={stats['n']} paper={stats['avg_paper_pct']}% "
                      f"real={stats['avg_real_pct']}% gap={stats['gap_pp']}pp "
                      f"alert={stats['alert']}")
        except Exception as e:
            print(f"[DIVERGENCE] Error en loop: {e}")
        await asyncio.sleep(interval)


async def fast_price_check_b_loop():
    """Agente B real desactivado — loop vacío para no romper el gather."""
    while True:
        await asyncio.sleep(3600)


async def daily_session_loop():
    """A las 22:00 guarda snapshot de equity y reinicia contador diario de PnL."""
    # Catch-up: si el bot arrancó después de las 22:00 de hoy, tomar snapshot inmediato
    # para no perder el registro del día (el INSERT OR IGNORE en _take_daily_snapshot
    # garantiza que no duplicamos si ya existía la fila).
    _now_startup = datetime.now()
    _today_22 = _now_startup.replace(hour=22, minute=0, second=0, microsecond=0)
    if _now_startup >= _today_22:
        try:
            await asyncio.get_event_loop().run_in_executor(None, _take_daily_snapshot)
        except Exception as e:
            print(f"[DAILY] Error en snapshot catch-up: {e}")
    while True:
        now = datetime.now()
        target = now.replace(hour=22, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            await asyncio.get_event_loop().run_in_executor(None, _take_daily_snapshot)
        except Exception as e:
            print(f"[DAILY] Error en snapshot diario: {e}")


def _take_daily_snapshot():
    """Calcula equity actual y la registra en daily_sessions."""
    import requests as _req
    from web3 import Web3 as _W3
    from dotenv import load_dotenv
    load_dotenv()

    _rpc = "https://polygon-bor-rpc.publicnode.com"
    _w3  = _W3(_W3.HTTPProvider(_rpc))
    _ABI = [{"name":"balanceOf","type":"function","inputs":[{"name":"o","type":"address"}],
             "outputs":[{"name":"","type":"uint256"}],"stateMutability":"view"}]
    _PUSD = _W3.to_checksum_address("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")
    _dw   = _W3.to_checksum_address(config.DEPOSIT_WALLET_ADDRESS)

    try:
        _libre = _w3.eth.contract(address=_PUSD, abi=_ABI).functions.balanceOf(_dw).call() / 1e6
    except Exception:
        _libre = 0.0

    conn = database.get_connection()
    _cap = float(conn.execute(
        "SELECT COALESCE(SUM(size_filled),0) FROM bot_trades "
        "WHERE agent_id='DEF_COPY' AND paper_trade=0 AND status='open'"
    ).fetchone()[0] or 0)

    _open_toks = set(r[0] for r in conn.execute(
        "SELECT token_id FROM bot_trades WHERE agent_id='DEF_COPY' AND paper_trade=0 AND status='open'"
    ).fetchall())

    _redeem = 0.0
    try:
        _pos = _req.get(
            f"https://data-api.polymarket.com/positions?user={config.DEPOSIT_WALLET_ADDRESS}&sizeThreshold=.01",
            timeout=15).json() or []
        _redeem = sum(float(p.get('currentValue', 0) or 0)
                      for p in _pos if p.get('asset') not in _open_toks and float(p.get('currentValue', 0) or 0) > 0.01)
    except Exception:
        pass

    _locked = 0.0
    try:
        _orders = executor.get_client().get_orders() or []
        for _o in _orders:
            if _o.get('side') != 'BUY':
                continue
            _orig = float(_o.get('original_size', 0) or 0)
            _matched = float(_o.get('size_matched', 0) or 0)
            _price = float(_o.get('price', 0) or 0)
            _locked += max(0.0, (_orig - _matched) * _price)
    except Exception:
        pass

    equity = round(_libre + _cap + _redeem + _locked, 2)

    # PnL del día = todos los trades cerrados con executed_at en el día de hoy (fecha local)
    today_date = datetime.now().strftime("%Y-%m-%d")
    _pnl_day = float(conn.execute(
        "SELECT COALESCE(SUM(pnl_usdc),0) FROM bot_trades "
        "WHERE agent_id='DEF_COPY' AND paper_trade=0 AND status='closed' "
        "AND DATE(executed_at)=?",
        (today_date,)
    ).fetchone()[0] or 0)
    _won = conn.execute(
        "SELECT COUNT(*) FROM bot_trades "
        "WHERE agent_id='DEF_COPY' AND paper_trade=0 AND status='closed' "
        "AND pnl_usdc > 0 AND DATE(executed_at)=?",
        (today_date,)
    ).fetchone()[0]
    _lost = conn.execute(
        "SELECT COUNT(*) FROM bot_trades "
        "WHERE agent_id='DEF_COPY' AND paper_trade=0 AND status='closed' "
        "AND pnl_usdc <= 0 AND DATE(executed_at)=?",
        (today_date,)
    ).fetchone()[0]

    # Cerrar el día actual con su PnL final (INSERT OR IGNORE garantiza que la fila existe)
    conn.execute(
        "INSERT OR IGNORE INTO daily_sessions (date, start_ts, equity_start) VALUES (?,?,?)",
        (today_date, datetime.now().replace(hour=22, minute=0, second=0, microsecond=0).isoformat(), equity)
    )
    conn.execute(
        "UPDATE daily_sessions SET equity_end=?, pnl_day=?, trades_won=?, trades_lost=? WHERE date=?",
        (equity, round(_pnl_day, 4), _won, _lost, today_date)
    )

    # Abrir nuevo día (mañana)
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    conn.execute(
        "INSERT OR REPLACE INTO daily_sessions (date, start_ts, equity_start) VALUES (?,?,?)",
        (tomorrow, (datetime.now() + timedelta(days=1)).replace(hour=22, minute=0, second=0, microsecond=0).isoformat(), equity)
    )
    conn.commit()
    conn.close()
    print(f"[DAILY] Snapshot 22:00 → equity={equity} pnl_dia={round(_pnl_day,2)} won={_won} lost={_lost}")


async def credentials_refresh_loop():
    """Renueva las API keys de Polymarket cada 23 horas para evitar expiración."""
    while True:
        await asyncio.sleep(23 * 3600)
        ok = executor.refresh_poly_credentials()
        if not ok:
            print("[CREDS] refresh falló — reintentando en 5 min")
            await asyncio.sleep(300)
            executor.refresh_poly_credentials()

async def pnl_check_loop():
    """Verifica resolución de trades abiertos y actualiza outcomes de feedback cada 15 min."""
    while True:
        try:
            await asyncio.get_event_loop().run_in_executor(None, pnl_tracker.check_open_trades)
        except Exception as e:
            print(f"[PNL] Error en check_open_trades: {e}")
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, pnl_tracker.resolve_feedback_outcomes
            )
        except Exception as e:
            print(f"[LEARN] Error en resolve_feedback_outcomes: {e}")
        try:
            import manual_trade_tracker as _mtt
            await asyncio.get_event_loop().run_in_executor(
                None, _mtt.resolve_open_trades
            )
        except Exception as e:
            print(f"[MANUAL] Error en resolve_open_trades: {e}")
        await asyncio.sleep(15 * 60)

# ── Startup ───────────────────────────────────────────────────────────────────

async def startup():
    print("=" * 50)
    print("POLYMARKET INSIDER AGENT  [MODO AUTOMÁTICO]")
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("Paper trading : " + str(config.PAPER_TRADING))
    print("Max trade     : " + str(config.MAX_TRADE_SIZE_USDC) + " USDC")
    print("=" * 50)

    database.init_db()

    # Cargar overrides del auto-tuner sobre config.* en runtime (Fase 2.4)
    try:
        auto_tuner.apply_runtime_overrides()
    except Exception as e:
        print(f"[TUNER] apply_runtime_overrides error: {e}")

    # Renovar credentials al arrancar — garantiza keys válidas desde el inicio
    executor.refresh_poly_credentials()

    # M-2 FIX (2026-06-10): pre-popula _COPY_LAST_AGG desde la BD para evitar
    # REINFORCE falso en el primer ciclo tras un restart del bot.
    try:
        import real_signal_router as _rsr
        _rsr._populate_copy_last_agg_from_db()
    except Exception as _e:
        print(f"[STARTUP] _populate_copy_last_agg warn: {_e}")

    # ── Pre-flight de seguridad real (Fase 4.6, 2026-05-23) ──────────
    # Verifica/repara approvals CTF y rescata posiciones fantasma.
    # Si approvals no se pueden reparar → auto-desactiva REAL_TRADING_ENABLED
    # para no abrir trades que luego no podríamos cerrar.
    if not config.PAPER_TRADING and config.REAL_TRADING_ENABLED:
        try:
            ok = safety.startup_preflight()
            if not ok:
                print("[SAFETY] Pre-flight fallido — real trading desactivado")
        except Exception as e:
            print(f"[SAFETY] Pre-flight error: {e}")

    wallets = database.get_top_wallets(50)
    if not wallets:
        print("Sin wallets en BD. Escaneando leaderboard (bloqueante)...")
        leaderboard_analyzer.run_analysis(200)
    else:
        # Wallets ya en BD → lanzar leaderboard en background para no
        # bloquear el monitor. El monitor arranca de inmediato con los
        # datos existentes; el leaderboard los actualiza en ~4 min.
        print(f"Wallets en BD: {len(wallets)} · leaderboard en background...")
        threading.Thread(
            target=leaderboard_analyzer.run_analysis,
            args=(200,),
            daemon=True,
            name="leaderboard-bg"
        ).start()

    stop_event = asyncio.Event()

    # Dashboard web server: thread daemon
    _web_t = threading.Thread(target=_start_web_server, daemon=True, name="web-server")
    _web_t.start()

    # Export loop: thread daemon, no bloquea asyncio.
    # L33 (2026-05-31): importlib.reload en cada iteración para que cualquier
    # parche al exporter (cálculo de equity, pending_redeem, etc.) entre en
    # producción sin restart completo. El módulo es lightweight, la recarga
    # cuesta <50ms y vale más que arrastrar versiones obsoletas en memoria.
    def _export_loop():
        import importlib as _il
        data_exporter.export_data()
        while True:
            time.sleep(60)
            try:
                _il.reload(data_exporter)
            except Exception as e:
                print(f"[EXPORT] reload err: {e}")
            data_exporter.export_data()

    _t = threading.Thread(target=_export_loop, daemon=True, name="export-loop")
    _t.start()
    print("[EXPORT] Loop de exportación iniciado (cada 60s → web/data.json)")

    # Wallet rotation study: cada 48h genera reporte (no aplica cambios)
    def _wallet_rotation_loop():
        import subprocess
        ROTATION_INTERVAL_SEC = int(os.getenv("WALLET_ROTATION_INTERVAL_SEC", 48*3600))
        # primera ejecución 5 min tras arranque (dar tiempo al bot)
        time.sleep(300)
        while True:
            try:
                print(f"[ROTATOR] Lanzando estudio recurrente de traders…")
                result = subprocess.run(
                    [sys.executable, "tools/wallet_rotator.py"],
                    cwd=str(os.path.dirname(os.path.abspath(__file__))),
                    capture_output=True, text=True, timeout=600,
                )
                for line in (result.stdout or "").splitlines()[-5:]:
                    print(f"[ROTATOR] {line}")
                if result.returncode != 0:
                    print(f"[ROTATOR] err: {result.stderr[:200]}")
            except Exception as e:
                print(f"[ROTATOR] excepción: {e}")
            time.sleep(ROTATION_INTERVAL_SEC)
    _rot_t = threading.Thread(target=_wallet_rotation_loop, daemon=True, name="wallet-rotator")
    _rot_t.start()
    print(f"[ROTATOR] Loop iniciado (cada {int(os.getenv('WALLET_ROTATION_INTERVAL_SEC', 48*3600))//3600}h → reports/wallet_rotation_*.md)")

    print("Arrancando todos los módulos...")
    while True:
        try:
            tasks = [
                monitor.monitor_loop(),
                run_telegram_bot(stop_event),
                weekly_review_loop(),
                eonet_monitor.eonet_loop(),
                reputation_tracker.reputation_loop(),
                credentials_refresh_loop(),
                trusted_scanner.trusted_scan_loop(),
                pnl_check_loop(),
                hot_streak_scanner.hot_streak_loop(),
                whale_tracker.whale_loop(),
                my_behavior_analyzer.behavior_snapshot_loop(),
                paper_monitor_loop(),
                paper_b_fast_loop(),
                auto_tuner.auto_tuner_loop(),
                db_backup_loop(),
                experiment_final_report_loop(),
                reconcile_real_trades_loop(),
                fast_price_check_b_loop(),
                safety_health_check_loop(),
                poll_pending_sells_loop(),
                divergence_monitor_loop(),
                diagnostics_loop(),
                definitive_copy_loop(),
                monitor.whitelist_fast_loop(),
                daily_session_loop(),
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            # Loguear qué tarea falló sin tumbar el bot
            for i, r in enumerate(results):
                if isinstance(r, Exception):
                    print(f"[MAIN] ⚠️  Tarea {i} terminó con error: {r}")
        except Exception as e:
            print(f"[MAIN] Error en ciclo principal: {e}")
        print("[MAIN] Reiniciando ciclo en 10s...")
        await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(startup())
