"""Genera los datos públicos de la simulación en papel para la web de progreso.

    python tools/exportar_progreso.py [--salida publico]

Lee la base de datos de la simulación (POLY_DB_PATH) y escribe en la carpeta de salida:
  * progreso.json  — foto actual: agentes, operaciones recientes y monederos seguidos;
  * historial.json — un punto por ejecución con el PnL de cada agente (la curva de la web).
Solo publica datos de la simulación en papel: nada de saldos, claves ni operaciones reales.
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

ABIERTA = "open"
GANADAS = {"tp_hit", "resolved_win"}
PERDIDAS = {"sl_hit", "resolved_loss"}
MAX_HISTORIAL = 5000


def _filas(conn, sql, *args):
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _pnl_abierto(t: dict) -> float:
    """PnL latente de una operación abierta con el último precio visto (0 si aún no se ha comprobado)."""
    precio = t.get("last_check_price")
    if precio is None or not t.get("entry_shares"):
        return 0.0
    return float(precio) * float(t["entry_shares"]) - float(t.get("entry_size_usdc") or 0)


def _resumen_agente(agente: dict, trades: list[dict]) -> dict:
    cerradas = [t for t in trades if t["status"] != ABIERTA]
    abiertas = [t for t in trades if t["status"] == ABIERTA]
    ganadas = sum(1 for t in cerradas if (t.get("realized_pnl_net") or 0) > 0)
    pnl_cerrado = sum(float(t.get("realized_pnl_net") or 0) for t in cerradas)
    pnl_abierto = sum(_pnl_abierto(t) for t in abiertas)
    invertido = sum(float(t.get("entry_size_usdc") or 0) for t in trades)
    return {
        "id": agente["id"],
        "nombre": agente["name"],
        "descripcion": agente["description"],
        "operaciones": len(trades),
        "abiertas": len(abiertas),
        "cerradas": len(cerradas),
        "ganadas": ganadas,
        "perdidas": len(cerradas) - ganadas,
        "acierto": round(ganadas / len(cerradas), 4) if cerradas else None,
        "pnl_cerrado": round(pnl_cerrado, 2),
        "pnl_abierto": round(pnl_abierto, 2),
        "pnl_total": round(pnl_cerrado + pnl_abierto, 2),
        "invertido": round(invertido, 2),
        "rentabilidad": round((pnl_cerrado + pnl_abierto) / invertido, 4) if invertido else None,
    }


def _corta(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}" if addr and len(addr) > 12 else (addr or "")


def exportar(db: str, salida: Path) -> dict:
    conn = sqlite3.connect(db)
    agentes = _filas(conn, "SELECT id, name, description FROM agents WHERE active = 1 ORDER BY id")
    trades = _filas(conn, """SELECT id, agent_id, wallet_copied, market_question, outcome_name, side, niche,
                                    entry_at, entry_price, entry_size_usdc, entry_shares, status, exit_at,
                                    exit_price, exit_reason, last_check_price, realized_pnl_net
                             FROM paper_trades WHERE agent_id != 'A_legacy' ORDER BY entry_at""")
    monederos = _filas(conn, """SELECT address, username, insider_score, win_rate, profit_factor
                                FROM wallets WHERE active = 1 ORDER BY insider_score DESC""")
    conn.close()

    ahora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    resumen = [_resumen_agente(a, [t for t in trades if t["agent_id"] == a["id"]]) for a in agentes]
    recientes = sorted(trades, key=lambda t: t.get("exit_at") or t["entry_at"], reverse=True)[:60]
    progreso = {
        "actualizado": ahora,
        "inicio": trades[0]["entry_at"] if trades else None,
        "aviso": "Simulación en papel con precios reales de Polymarket. No se arriesga dinero. "
                 "No es asesoramiento financiero.",
        "agentes": resumen,
        "recientes": [{
            "agente": t["agent_id"], "mercado": t["market_question"], "apuesta": t["outcome_name"],
            "nicho": t["niche"], "entrada": t["entry_at"], "precio_entrada": t["entry_price"],
            "importe": t["entry_size_usdc"], "estado": t["status"], "salida": t["exit_at"],
            "precio_salida": t["exit_price"],
            "pnl": round(float(t["realized_pnl_net"]), 2) if t["realized_pnl_net"] is not None
            else round(_pnl_abierto(t), 2),
            "monedero": _corta(t["wallet_copied"]),
        } for t in recientes],
        "monederos": [{
            "monedero": _corta(m["address"]), "nombre": m["username"],
            "acierto_historico": m["win_rate"], "profit_factor": m["profit_factor"],
        } for m in monederos],
    }

    salida.mkdir(parents=True, exist_ok=True)
    (salida / "progreso.json").write_text(json.dumps(progreso, ensure_ascii=False, indent=1), encoding="utf-8")

    ruta_hist = salida / "historial.json"
    try:
        historial = json.loads(ruta_hist.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        historial = []
    historial.append({"t": ahora, **{a["id"]: a["pnl_total"] for a in resumen}})
    ruta_hist.write_text(json.dumps(historial[-MAX_HISTORIAL:], separators=(",", ":")), encoding="utf-8")
    return progreso


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--salida", default=str(RAIZ / "publico"))
    p.add_argument("--db", default=os.getenv("POLY_DB_PATH", "polymarket_insider.db"))
    a = p.parse_args()
    db = a.db if os.path.isabs(a.db) else str(RAIZ / a.db)
    progreso = exportar(db, Path(a.salida))
    for ag in progreso["agentes"]:
        print(f"{ag['id']} {ag['nombre']}: {ag['operaciones']} op · PnL {ag['pnl_total']:+.2f} USDC")


if __name__ == "__main__":
    main()
