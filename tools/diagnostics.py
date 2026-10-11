"""
diagnostics.py — Suite de chequeos automáticos del sistema.

Detecta las clases de bugs L24-L27 ANTES de perder oportunidades.

Uso:
  python tools/diagnostics.py                # corre todos los checks
  python tools/diagnostics.py --check capture # solo uno
  python tools/diagnostics.py --silent       # exit code != 0 si falla
  python tools/diagnostics.py --notify       # envia alerta Telegram si falla

Integrable también desde safety.py para correr periódicamente en el bot.
"""
import os
import sys
import sqlite3
import re
import subprocess
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

import requests
import config

DB_PATH = Path(__file__).resolve().parent.parent / "polymarket_insider.db"
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
SRC_DIR = Path(__file__).resolve().parent.parent


# ─── Helpers ────────────────────────────────────────────────────────────
def _today_log() -> Path:
    return LOG_DIR / f"bot-{datetime.now().strftime('%Y%m%d')}.log"


def _whitelist(env_var: str) -> set:
    raw = os.environ.get(env_var, "") or ""
    return {a.strip().lower() for a in raw.split(",") if a.strip()}


def _fetch_polymarket_buys(wallet: str, hours: int = 24) -> list:
    """Fetch TRADE BUYs del wallet últimas N horas."""
    out = []
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp()
    for offset in range(0, 2000, 500):
        try:
            r = requests.get("https://data-api.polymarket.com/activity",
                             params={"user": wallet, "limit": 500, "offset": offset},
                             timeout=10)
            if r.status_code != 200:
                break
            d = r.json() or []
            if not d:
                break
            out.extend(d)
            if len(d) < 500:
                break
        except Exception:
            break
    return [a for a in out if a.get("type") == "TRADE"
            and str(a.get("side", "")).upper() == "BUY"
            and int(a.get("timestamp", 0)) >= cutoff]


# ─── Chequeos individuales ─────────────────────────────────────────────
def check_capture_rate(hours: int = 24, min_capture_pct: float = 30.0) -> dict:
    """
    Verifica que estamos capturando trades de las wallets whitelist.

    Para cada wallet en whitelist B o D:
      - Cuenta BUYs en Polymarket últimas N horas
      - Cuenta detected_trades nuestras en la misma ventana
      - Si la wallet tiene >5 BUYs en Polymarket pero <30% se detectaron → FALLA

    Detecta bugs tipo L27 (REDEEMs ocultan BUYs), L26 (wallet desactivada),
    L24 (signal incompleto), etc.
    """
    result = {"name": "capture_rate", "status": "OK", "issues": []}
    wl_b = _whitelist("REAL_WALLET_WHITELIST")
    wl_d = _whitelist("REAL_D_WALLET_WHITELIST")
    all_w = wl_b | wl_d
    if not all_w:
        result["status"] = "SKIP"
        result["issues"].append("Sin whitelist configurada")
        return result

    since_iso = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    con = sqlite3.connect(DB_PATH)
    for wallet in all_w:
        try:
            poly_buys = len(_fetch_polymarket_buys(wallet, hours=hours))
            our_detected = con.execute(
                "SELECT COUNT(*) FROM detected_trades WHERE LOWER(wallet_address)=? AND detected_at >= ?",
                (wallet, since_iso),
            ).fetchone()[0]
            if poly_buys >= 5:
                capture_pct = (our_detected / poly_buys * 100) if poly_buys else 0
                if capture_pct < min_capture_pct:
                    result["status"] = "FAIL"
                    result["issues"].append(
                        f"Wallet {wallet[:10]}…: capture {capture_pct:.0f}% "
                        f"({our_detected}/{poly_buys}) < {min_capture_pct}%"
                    )
        except Exception as e:
            result["issues"].append(f"err {wallet[:10]}…: {e}")
    con.close()
    return result


def check_active_whitelist_wallets() -> dict:
    """Verifica que las wallets whitelist tienen active=1 en BD."""
    result = {"name": "active_whitelist", "status": "OK", "issues": []}
    wl = _whitelist("REAL_WALLET_WHITELIST") | _whitelist("REAL_D_WALLET_WHITELIST")
    con = sqlite3.connect(DB_PATH)
    for wallet in wl:
        r = con.execute("SELECT active, insider_score FROM wallets WHERE LOWER(address)=?", (wallet,)).fetchone()
        if not r:
            result["status"] = "FAIL"
            result["issues"].append(f"Wallet {wallet[:10]}… no en tabla wallets")
        elif r[0] == 0:
            result["status"] = "FAIL"
            result["issues"].append(f"Wallet {wallet[:10]}… active=0 (iscore={r[1]})")
    con.close()
    return result


def check_router_activity(hours: int = 6) -> dict:
    """
    Verifica que router B/D están procesando señales.

    Si en últimas N horas hay <X entradas de [ROUTER_B] o [ROUTER_D],
    probablemente el monitor no llega a procesar wallets whitelist.
    Detecta bugs tipo L27.
    """
    result = {"name": "router_activity", "status": "OK", "issues": []}
    log = _today_log()
    if not log.exists():
        result["status"] = "SKIP"
        return result
    try:
        # Counts de logs ROUTER_B y ROUTER_D
        b_count = int(subprocess.run(["grep", "-c", "REAL_ROUTER\\]\\|REAL_ROUTER ", str(log)],
                                       capture_output=True, text=True).stdout.strip() or "0")
        d_count = int(subprocess.run(["grep", "-c", "ROUTER_D", str(log)],
                                       capture_output=True, text=True).stdout.strip() or "0")
        wl_b = _whitelist("REAL_WALLET_WHITELIST")
        wl_d = _whitelist("REAL_D_WALLET_WHITELIST")
        # B con 5 whitelist wallets debería tener cierta actividad/día
        if wl_b and b_count == 0:
            result["status"] = "WARN"
            result["issues"].append(f"[ROUTER B] 0 entradas hoy con {len(wl_b)} wallets B")
        if wl_d and d_count <= 1:
            result["status"] = "WARN"
            result["issues"].append(
                f"[ROUTER_D] solo {d_count} entradas hoy con {len(wl_d)} wallets D"
            )
    except Exception as e:
        result["issues"].append(f"err parsing log: {e}")
    return result


def check_magic_numbers() -> dict:
    """
    Busca magic numbers que deberían ser config.

    Greps por thresholds hardcoded comunes: 0.75 (WR), 0.50 (entry), 100 (size),
    etc. Detecta drift tipo L25.
    """
    result = {"name": "magic_numbers", "status": "OK", "issues": []}
    suspicious = []
    # Patrones a buscar (file_glob, regex, descripcion)
    patterns = [
        ("*.py", r"recent_wr\s*<\s*0\.7\d", "WR threshold hardcoded — debe ser config.MIN_WIN_RATE"),
        ("*.py", r"insider_score\s*>=\s*0\.7\d", "insider_score threshold hardcoded — debe ser config.MIN_INSIDER_SCORE"),
    ]
    for glob_pat, regex, desc in patterns:
        try:
            for f in SRC_DIR.glob(glob_pat):
                if f.name in ("config.py",) or f.is_dir():
                    continue
                content = f.read_text(encoding="utf-8", errors="ignore")
                for i, line in enumerate(content.split("\n"), 1):
                    if re.search(regex, line):
                        # Si la línea referencia config, no es magic
                        if "config." in line:
                            continue
                        suspicious.append(f"{f.name}:{i} — {desc}: {line.strip()[:80]}")
        except Exception:
            pass
    if suspicious:
        result["status"] = "WARN"
        result["issues"] = suspicious[:5]  # cap top 5
    return result


def check_redeem_vs_trade_balance(hours: int = 24, min_trades_per_5_redeems: int = 1) -> dict:
    """
    Detecta bug L27: si wallet whitelist tiene muchos REDEEMs pero pocos TRADEs
    procesados, sospechar que limit del monitor está bloqueando.

    Para cada wallet whitelist con ≥5 REDEEMs en últimas N horas, requerir
    que el bot HAYA procesado al menos 1 trade suyo en el mismo período.
    """
    result = {"name": "redeem_vs_trade_balance", "status": "OK", "issues": []}
    wl = _whitelist("REAL_WALLET_WHITELIST") | _whitelist("REAL_D_WALLET_WHITELIST")
    cutoff_ts = (datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp()
    since_iso = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    con = sqlite3.connect(DB_PATH)
    for wallet in wl:
        try:
            r = requests.get("https://data-api.polymarket.com/activity",
                             params={"user": wallet, "limit": 100}, timeout=10).json() or []
            redeems = sum(1 for a in r if a.get("type") == "REDEEM"
                          and int(a.get("timestamp", 0)) >= cutoff_ts)
            buys = sum(1 for a in r if a.get("type") == "TRADE"
                       and str(a.get("side", "")).upper() == "BUY"
                       and int(a.get("timestamp", 0)) >= cutoff_ts)
            our_processed = con.execute(
                "SELECT COUNT(*) FROM detected_trades WHERE LOWER(wallet_address)=? AND detected_at >= ?",
                (wallet, since_iso),
            ).fetchone()[0]
            if redeems >= 5 and buys >= 2 and our_processed == 0:
                result["status"] = "FAIL"
                result["issues"].append(
                    f"Wallet {wallet[:10]}…: {redeems} REDEEMs + {buys} BUYs Polymarket "
                    f"pero 0 procesados — posible L27 (limit oculta BUYs)"
                )
        except Exception as e:
            result["issues"].append(f"err {wallet[:10]}…: {e}")
    con.close()
    return result


def check_phantom_positions() -> dict:
    """Verifica que no haya posiciones fantasma (open + 0 shares > grace 30min)."""
    result = {"name": "phantoms", "status": "OK", "issues": []}
    try:
        import safety
        phantoms = safety.phantom_open_positions()
        if phantoms:
            result["status"] = "FAIL"
            for p in phantoms[:5]:
                result["issues"].append(
                    f"bt#{p['id']} ({p['age_min']:.0f}min sin shares) — possible L07/L18"
                )
    except Exception as e:
        result["issues"].append(f"err: {e}")
    return result


def check_db_terminal_states() -> dict:
    """Verifica que no haya trades en estados intermedios persistentes."""
    result = {"name": "db_terminal", "status": "OK", "issues": []}
    con = sqlite3.connect(DB_PATH)
    # close_failed sin progresar en >2h
    rows = con.execute("""SELECT COUNT(*) FROM bot_trades
                           WHERE paper_trade=0 AND status='close_failed'
                             AND executed_at < datetime('now','-2 hours')""").fetchone()[0]
    if rows >= 3:
        result["status"] = "WARN"
        result["issues"].append(f"{rows} trades close_failed >2h sin reconcile")
    # closing stuck
    rows = con.execute("""SELECT COUNT(*) FROM bot_trades
                           WHERE paper_trade=0 AND status='closing'
                             AND executed_at < datetime('now','-15 minutes')""").fetchone()[0]
    if rows >= 1:
        result["status"] = "FAIL"
        result["issues"].append(f"{rows} trades stuck en status='closing'")
    con.close()
    return result


def check_paper_real_divergence() -> dict:
    """Wrapper sobre safety.divergence_monitor."""
    result = {"name": "divergence", "status": "OK", "issues": []}
    try:
        import safety
        d = safety.divergence_monitor(window=20, alert_threshold_pp=25.0, notify=False)
        if d.get("alert"):
            result["status"] = "WARN"
            result["issues"].append(
                f"Gap paper-real = {d.get('gap_pp')}pp sobre {d.get('n')} pares"
            )
    except Exception as e:
        result["issues"].append(f"err: {e}")
    return result


def check_paper_real_mirror_B(hours: int = 24) -> dict:
    """L52 — detector de divergencia ENTRADA paper B vs real B.

    Paper B y Real B deben recibir las MISMAS señales (post fix L52).
    Si una señal abre paper B pero NO real B → bug en router/executor real.
    Si una señal abre real B pero NO paper B → bug en mirror filter paper.
    Ambos casos indican que algo cambió la simetría → alerta.
    """
    import sqlite3
    import config as _cfg
    result = {"name": "mirror_B", "status": "OK", "issues": []}
    real_agent = (getattr(_cfg, "REAL_TRADING_AGENT", "NONE") or "NONE").upper()
    if real_agent != "B":
        result["issues"].append(f"REAL_TRADING_AGENT={real_agent}, skip mirror_B")
        return result
    try:
        con = sqlite3.connect("polymarket_insider.db")
        # Señales con paper_B abierto pero NO real_B
        # L60: paper B ahora abre para muchas wallets (mirror filter off).
        # El check solo debe alertar para wallets en REAL_WALLET_WHITELIST
        # — el resto son research-mode y no se esperan en real B.
        real_wl = {a.strip().lower() for a in os.environ.get('REAL_WALLET_WHITELIST','').split(',') if a.strip()}
        if not real_wl:
            paper_only = []
        else:
            wl_placeholder = ','.join(['?']*len(real_wl))
            params = list(real_wl)
            paper_only = con.execute(f"""
                SELECT pt.detected_trade_id FROM paper_trades pt
                JOIN detected_trades dt ON dt.id=pt.detected_trade_id
                WHERE pt.agent_id='B'
                  AND datetime(pt.entry_at) >= datetime('now', '-{hours} hours')
                  AND LOWER(dt.wallet_address) IN ({wl_placeholder})
                  AND NOT EXISTS (
                    SELECT 1 FROM bot_trades bt
                    WHERE bt.detected_trade_id=pt.detected_trade_id
                      AND bt.paper_trade=0 AND bt.agent_id='B'
                  )
            """, params).fetchall()
        # Señales con real_B abierto pero NO paper_B
        real_only = con.execute(f"""
            SELECT bt.detected_trade_id FROM bot_trades bt
            WHERE bt.paper_trade=0 AND bt.agent_id='B'
              AND datetime(bt.executed_at) >= datetime('now', '-{hours} hours')
              AND NOT EXISTS (
                SELECT 1 FROM paper_trades pt
                WHERE pt.detected_trade_id=bt.detected_trade_id
                  AND pt.agent_id='B'
              )
        """).fetchall()
        con.close()
        n_paper_only = len(paper_only)
        n_real_only = len(real_only)
        if n_paper_only > 0:
            result["status"] = "WARN"
            ids = [r[0] for r in paper_only[:5]]
            result["issues"].append(
                f"{n_paper_only} señales con paper B pero SIN real B (24h). "
                f"Sample ids: {ids}")
        if n_real_only > 0:
            result["status"] = "FAIL"
            ids = [r[0] for r in real_only[:5]]
            result["issues"].append(
                f"{n_real_only} señales con real B pero SIN paper B (24h). "
                f"Sample ids: {ids} — bug en mirror filter")
    except Exception as e:
        result["issues"].append(f"err: {e}")
    return result


def check_equity_consistency() -> dict:
    """L46 (2026-06-02) — alerta si dashboard equity diverge de Polymarket API
    en > $5. Casos típicos: bug L46 (best_bid sub-valora), bug L46b (suma
    solo agente B), o tokens con currentValue desconocido en API."""
    result = {"name": "equity_consistency", "status": "OK", "issues": []}
    try:
        import requests, json as _json
        from pathlib import Path
        # Load data.json equity
        dj = _json.loads(Path("web/data.json").read_text())
        dash_eq = float(dj.get("real_trading", {}).get("equity_total_usdc") or 0)
        # Audit real: pUSD libre + PM positions
        from web3 import Web3
        DW = os.environ.get("DEPOSIT_WALLET_ADDRESS","")
        PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
        w3 = Web3(Web3.HTTPProvider("https://polygon-bor-rpc.publicnode.com", request_kwargs={"timeout":5}))
        abi = [{"name":"balanceOf","type":"function","inputs":[{"name":"o","type":"address"}],"outputs":[{"name":"","type":"uint256"}],"stateMutability":"view"}]
        bal_pusd = w3.eth.contract(address=Web3.to_checksum_address(PUSD), abi=abi).functions.balanceOf(Web3.to_checksum_address(DW)).call()/1e6
        r = requests.get(f"https://data-api.polymarket.com/positions?user={DW}&limit=500", timeout=8)
        pm_val = sum(float(p.get('currentValue',0) or 0) for p in r.json() or [])
        real_eq = bal_pusd + pm_val
        diff = abs(real_eq - dash_eq)
        if diff > 5:
            result["status"] = "FAIL"
            result["issues"].append(
                f"dashboard=${dash_eq:.2f} vs real=${real_eq:.2f} → diff ${diff:.2f}"
            )
    except Exception as e:
        result["issues"].append(f"err: {e}")
    return result


def check_oco_coverage() -> dict:
    """L54 (2026-06-03) — DEPRECATED. OCO SL post-fill fue eliminado
    deliberadamente (L51 lo neutralizó porque LIMIT SELL al SL price
    cruza inmediatamente con el bid). TP/SL ahora se gestionan via
    fast_price_check_b polling + close_position.

    Retorna OK siempre (cero falsos positivos)."""
    return {"name": "oco_coverage", "status": "OK",
            "issues": ["check deprecated · L51+L54 eliminó OCO post-fill"]}


def check_tp_sl_inversion(hours: int = 48, min_pairs: int = 3) -> dict:
    """L28 (2026-05-30) — detecta divergencia tóxica paper-tp_hit / real-loss.

    Cuando paper marca tp_hit y el real correspondiente cerró en loss,
    es síntoma del bug L28: cadencia de check del paper (60s) más lenta
    que la del executor real (15s) → el paper "se salta" el dip, el real
    lo coge con slippage.

    FAIL si hay ≥ min_pairs casos en las últimas `hours`.
    """
    result = {"name": "tp_sl_inversion", "status": "OK", "issues": []}
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(f"""
            SELECT pt.id, bt.id, ROUND(pt.realized_pnl_net,2), ROUND(bt.pnl_usdc,2),
                   substr(pt.market_question,1,40)
            FROM paper_trades pt
            JOIN bot_trades bt
              ON bt.detected_trade_id = pt.detected_trade_id
             AND bt.paper_trade = 0
             AND COALESCE(bt.agent_id,'B') = pt.agent_id
            WHERE pt.agent_id='B'
              AND pt.status='tp_hit'
              AND bt.status='closed'
              AND bt.pnl_usdc < 0
              AND pt.entry_at >= datetime('now', ?)
            ORDER BY pt.entry_at DESC
        """, (f"-{hours} hours",))
        rows = c.fetchall()
        conn.close()
        if len(rows) >= min_pairs:
            result["status"] = "FAIL"
            for pid, rid, ppnl, rpnl, mkt in rows[:5]:
                result["issues"].append(
                    f"paper#{pid} tp_hit +{ppnl} vs real#{rid} loss {rpnl} · {mkt}"
                )
        elif rows:
            result["status"] = "WARN"
            result["issues"].append(
                f"{len(rows)} inversion(es) tp_hit/real_loss en {hours}h "
                f"(umbral FAIL: ≥{min_pairs})"
            )
    except Exception as e:
        result["issues"].append(f"err: {e}")
    return result



def check_close_failure_rate(hours: int = 6) -> dict:
    """L58 (2026-06-03) — detecta L58-style bugs.
    Si demasiados close attempts fallan con 'no_bids' o 'orderbook' 
    cuando el book SÍ tiene liquidez → bug SDK/network.
    FAIL si > 5 fallos no_bids en últimas 6h."""
    import sqlite3
    result = {"name": "close_failure_rate", "status": "OK", "issues": []}
    try:
        con = sqlite3.connect(DB_PATH)
        n = con.execute(f"""
            SELECT COUNT(*) FROM bot_trades 
            WHERE error LIKE '%no_bids%' 
              AND datetime(executed_at) >= datetime('now','-{hours} hours')
        """).fetchone()[0]
        con.close()
        if n > 5:
            result["status"] = "FAIL"
            result["issues"].append(
                f"{n} trades fallaron al cerrar con 'no_bids' en {hours}h — "
                f"revisar SDK get_order_book (bug L58 reincidente)")
        elif n > 0:
            result["status"] = "WARN"
            result["issues"].append(f"{n} no_bids fallas en {hours}h (monitor)")
    except Exception as e:
        result["issues"].append(f"err: {e}")
    return result



# ─── Orquestador ────────────────────────────────────────────────────────
ALL_CHECKS = {
    "capture":     check_capture_rate,
    "active":      check_active_whitelist_wallets,
    "router":     check_router_activity,
    "magic":       check_magic_numbers,
    "redeem":      check_redeem_vs_trade_balance,
    "phantoms":    check_phantom_positions,
    "terminal":    check_db_terminal_states,
    "divergence":  check_paper_real_divergence,
    "inversion":   check_tp_sl_inversion,
    "oco":         check_oco_coverage,
    "equity":      check_equity_consistency,
    "mirror_B":    check_paper_real_mirror_B,
    "close_fail":  check_close_failure_rate,
}


def run_all(notify: bool = False) -> dict:
    """Corre todos los checks. Devuelve dict con resumen."""
    results = []
    failed = []
    warned = []
    for name, fn in ALL_CHECKS.items():
        try:
            r = fn()
        except Exception as e:
            r = {"name": name, "status": "ERROR", "issues": [str(e)[:200]]}
        results.append(r)
        if r["status"] == "FAIL":
            failed.append(r)
        elif r["status"] == "WARN":
            warned.append(r)
    summary = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "total": len(results),
        "fails": len(failed),
        "warns": len(warned),
        "results": results,
    }
    if notify and (failed or warned):
        try:
            import safety
            lines = ["<b>🚨 Diagnostics</b>"]
            for r in failed + warned:
                tag = "🛑" if r["status"] == "FAIL" else "⚠️"
                lines.append(f"{tag} <b>{r['name']}</b>")
                for issue in r["issues"][:3]:
                    lines.append(f"   • {issue[:120]}")
            safety.tg_alert("\n".join(lines)[:3900])
        except Exception:
            pass
    return summary


def print_report(summary: dict):
    print(f"\n{'='*70}")
    print(f"DIAGNOSTICS — {summary['checked_at'][:19]}")
    print(f"{'='*70}")
    print(f"Total: {summary['total']}  ·  Fails: {summary['fails']}  ·  Warns: {summary['warns']}\n")
    for r in summary["results"]:
        icon = {"OK": "✓", "WARN": "⚠", "FAIL": "🛑", "SKIP": "—", "ERROR": "💥"}.get(r["status"], "?")
        print(f"  {icon} {r['name']:<22} [{r['status']}]")
        for issue in r["issues"][:3]:
            print(f"      · {issue}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", help="Solo un check específico", default=None)
    ap.add_argument("--silent", action="store_true", help="Exit code != 0 si fail")
    ap.add_argument("--notify", action="store_true", help="Telegram alert si fail")
    args = ap.parse_args()

    if args.check:
        if args.check not in ALL_CHECKS:
            print(f"Unknown check. Disponibles: {', '.join(ALL_CHECKS)}")
            sys.exit(2)
        r = ALL_CHECKS[args.check]()
        print(f"  {r['status']}: {r['name']}")
        for issue in r["issues"]:
            print(f"    · {issue}")
        sys.exit(0 if r["status"] in ("OK", "SKIP") else 1)

    summary = run_all(notify=args.notify)
    if not args.silent:
        print_report(summary)
    sys.exit(1 if summary["fails"] > 0 else 0)