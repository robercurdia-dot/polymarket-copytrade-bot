"""Genera los datos públicos de la simulación en papel para la web de progreso.

    python tools/exportar_progreso.py [--salida publico]

Lee la base de datos de la simulación (POLY_DB_PATH) y escribe en la carpeta de salida:
  * progreso.json  — foto actual: agentes, monederos copiados, operaciones recientes;
  * historial.json — un punto por ejecución con el PnL realizado de cada agente (la curva de la web).
Solo publica datos de la simulación en papel: nada de saldos, claves ni operaciones reales.

Reglas de LESSONS.md que aplica:
  * §7 — la cifra principal es el PnL realizado; el no realizado va aparte y nunca se suma a la curva.
  * §6 — todo lleva su ventana (inicio/fin en UTC) y su fuente.
  * §10 — se publican las comisiones acumuladas.
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
DESCARTADAS = ("cancelled",)
MAX_HISTORIAL = 5000
MAX_PUNTOS = 3000
FUENTE = ("Simulación en papel (BD local del bot). Entradas al mejor ask del CLOB y salidas al precio de "
          "venta del trader copiado; no realizado al punto medio del CLOB.")


def _filas(conn, sql, *args):
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _realizado(t: dict) -> float:
    """Incluye las salidas parciales ya hechas en posiciones que siguen abiertas."""
    return float(t.get("realized_pnl_net") or 0)


def _no_realizado(t: dict) -> float:
    """PnL latente de lo que queda abierto, al último precio visto (0 si aún no se ha comprobado)."""
    if t["status"] != ABIERTA:
        return 0.0
    precio = t.get("last_check_price")
    if precio is None or not t.get("entry_shares"):
        return 0.0
    return float(precio) * float(t["entry_shares"]) - float(t.get("entry_size_usdc") or 0)


def _resumen(trades: list[dict]) -> dict:
    cerradas = [t for t in trades if t["status"] != ABIERTA]
    abiertas = [t for t in trades if t["status"] == ABIERTA]
    ganadas = sum(1 for t in cerradas if _realizado(t) > 0)
    realizado = sum(_realizado(t) for t in trades)
    return {
        "operaciones": len(trades),
        "abiertas": len(abiertas),
        "cerradas": len(cerradas),
        "ganadas": ganadas,
        "perdidas": len(cerradas) - ganadas,
        "acierto": round(ganadas / len(cerradas), 4) if cerradas else None,
        "realizado": round(realizado, 2),
        "no_realizado": round(sum(_no_realizado(t) for t in abiertas), 2),
        "comisiones": round(sum(float(t.get("fees_estimated") or 0) for t in trades), 2),
        "apostado": round(sum(float(t.get("entry_size_usdc") or 0) for t in trades), 2),
    }


def _corta(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}" if addr and len(addr) > 12 else (addr or "")


def _monederos_copiados(trades: list[dict]) -> list[dict]:
    import copy_wallets
    seleccion = {}
    try:
        datos = json.loads((RAIZ / "sim" / "seleccion.json").read_text(encoding="utf-8"))
        seleccion = {f["monedero"]: f for f in datos.get("elegidos", [])}
    except (FileNotFoundError, ValueError):
        pass
    copia = [t for t in trades if t["agent_id"] == "DEF_COPY"]
    out = []
    for addr, cfg in copy_wallets.para_def_copy().items():
        s = seleccion.get(addr, {})
        out.append({
            "nombre": cfg["name"], "monedero": _corta(addr), "escala": cfg["scale"],
            "perfil": f"https://polymarket.com/profile/{addr}",
            # Criterios de selección (README «Known-good trader selection»), medidos al elegirlo.
            "sharpe_30d": s.get("sharpe"), "pnl_30d": s.get("pnl_30d"), "meses_activos_12": s.get("meses_activos_12"),
            "mercados_30d": s.get("mercados_30d"), "ventas_30d": s.get("ventas_30d"), "compras_30d": s.get("compras_30d"),
            **_resumen([t for t in copia if (t["wallet_copied"] or "").lower() == addr]),
        })
    return out


def exportar(db: str, salida: Path) -> dict:
    conn = sqlite3.connect(db)
    agentes = _filas(conn, "SELECT id, name, description FROM agents WHERE active = 1 ORDER BY id")
    trades = _filas(conn, f"""SELECT id, agent_id, wallet_copied, market_question, outcome_name, side, niche,
                                     entry_at, entry_price, entry_size_usdc, entry_shares, status, exit_at,
                                     exit_price, exit_reason, last_check_price, realized_pnl_net, fees_estimated
                              FROM paper_trades
                              WHERE agent_id != 'A_legacy' AND status NOT IN ({','.join('?' * len(DESCARTADAS))})
                              ORDER BY entry_at""", *DESCARTADAS)
    conn.close()

    ids = [a["id"] for a in agentes]
    trades = [t for t in trades if t["agent_id"] in ids]
    ahora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # Primario primero (README: DEF_COPY decide; DEF_TP / DEF_HOLD son líneas base de comparación).
    agentes.sort(key=lambda a: (a["id"] != "DEF_COPY", a["id"]))
    resumen = [{"id": a["id"], "nombre": a["name"], "descripcion": a["description"],
                "principal": a["id"] == "DEF_COPY",
                **_resumen([t for t in trades if t["agent_id"] == a["id"]])} for a in agentes]
    recientes = sorted(trades, key=lambda t: t.get("exit_at") or t["entry_at"], reverse=True)[:60]
    progreso = {
        "actualizado": ahora,
        "ventana": {"inicio": (trades[0]["entry_at"] + "Z") if trades else None, "fin": ahora, "zona": "UTC"},
        "fuente": FUENTE,
        "aviso": "Simulación en papel con precios reales de Polymarket. No se arriesga dinero. "
                 "No es asesoramiento financiero.",
        "agentes": resumen,
        "copiados": _monederos_copiados(trades),
        "recientes": [{
            "agente": t["agent_id"], "mercado": t["market_question"], "apuesta": t["outcome_name"],
            "nicho": t["niche"], "entrada": t["entry_at"] + "Z", "precio_entrada": t["entry_price"],
            "importe": t["entry_size_usdc"], "estado": t["status"],
            "salida": (t["exit_at"] + "Z") if t["exit_at"] else None, "precio_salida": t["exit_price"],
            "motivo_salida": t["exit_reason"],
            "realizado": round(_realizado(t), 2), "no_realizado": round(_no_realizado(t), 2),
            "monedero": _corta(t["wallet_copied"]),
        } for t in recientes],
        # Todas las operaciones en formato compacto para las gráficas:
        # [precio, importe, entrada, realizado + no realizado, agente].
        "puntos": [[t["entry_price"], t["entry_size_usdc"], t["entry_at"] + "Z",
                    round(_realizado(t) + _no_realizado(t), 2), t["agent_id"]] for t in trades[-MAX_PUNTOS:]],
    }

    salida.mkdir(parents=True, exist_ok=True)
    (salida / "progreso.json").write_text(json.dumps(progreso, ensure_ascii=False, indent=1), encoding="utf-8")

    ruta_hist = salida / "historial.json"
    try:
        historial = json.loads(ruta_hist.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        historial = []
    # La curva es solo PnL realizado (LESSONS §7).
    historial.append({"t": ahora, **{a["id"]: a["realizado"] for a in resumen}})
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
        print(f"{ag['id']} {ag['nombre']}: {ag['operaciones']} op · realizado {ag['realizado']:+.2f} · "
              f"no realizado {ag['no_realizado']:+.2f} USDC")


if __name__ == "__main__":
    main()
