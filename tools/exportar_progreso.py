"""Genera los datos públicos de la simulación en papel para la web de progreso.

    python tools/exportar_progreso.py [--salida publico]

Lee la base de datos de la simulación (POLY_DB_PATH) y la Data API pública, y escribe en la carpeta de salida:
  * progreso.json  — foto actual: cada trader copiado (criterios, últimos movimientos, resultado de su copia),
                     el total y las copias recientes;
  * historial.json — un punto por ejecución con el PnL realizado de la copia de cada trader (la curva de la web).
Solo publica la copia (DEF_COPY) en papel: nada de saldos, claves ni operaciones reales.

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

import requests

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

AGENTE = "DEF_COPY"
ABIERTA = "open"
MAX_HISTORIAL = 5000
MAX_PUNTOS = 3000
MOVIMIENTOS = 8
FUENTE = ("Simulación en papel (BD local del bot). Operaciones del trader detectadas en la cadena de Polygon, con la "
          "Data API de respaldo. Entradas al mejor ask del CLOB, salidas al precio de venta del trader; latente al "
          "punto medio del CLOB.")


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
    return {
        "operaciones": len(trades),
        "abiertas": len(abiertas),
        "cerradas": len(cerradas),
        "ganadas": ganadas,
        "acierto": round(ganadas / len(cerradas), 4) if cerradas else None,
        "realizado": round(sum(_realizado(t) for t in trades), 2),
        "no_realizado": round(sum(_no_realizado(t) for t in abiertas), 2),
        "comisiones": round(sum(float(t.get("fees_estimated") or 0) for t in trades), 2),
        "en_juego": round(sum(float(t.get("entry_size_usdc") or 0) for t in abiertas), 2),
    }


def _corta(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}" if addr and len(addr) > 12 else (addr or "")


def _movimientos(addr: str) -> list[dict]:
    """Últimos movimientos públicos del trader (Data API v2), para ver qué hace él además de lo que copiamos."""
    try:
        r = requests.get("https://data-api.polymarket.com/v2/activity", params={"user": addr, "limit": 25}, timeout=10)
        datos = (r.json().get("data") or []) if r.status_code == 200 else []
    except (requests.RequestException, ValueError):
        return []
    out = []
    for a in datos:
        if a.get("type") not in ("TRADE", "MERGE", "REDEEM"):
            continue
        out.append({
            "t": datetime.fromtimestamp(int(a["timestamp"]), timezone.utc).isoformat(timespec="seconds"),
            "tipo": a["type"].lower() if a["type"] != "TRADE" else (a.get("side") or "").lower(),
            "mercado": a.get("title"), "resultado": a.get("outcome"),
            "usdc": round(float(a.get("usdc_size") or 0), 2), "precio": a.get("price"),
        })
        if len(out) >= MOVIMIENTOS:
            break
    return out


def _traders(trades: list[dict]) -> list[dict]:
    import copy_wallets
    seleccion = {}
    try:
        datos = json.loads((RAIZ / "sim" / "seleccion.json").read_text(encoding="utf-8"))
        seleccion = {f["monedero"]: f for f in datos.get("elegidos", []) + datos.get("pasan", [])}
    except (FileNotFoundError, ValueError):
        pass
    out = []
    for addr, cfg in copy_wallets.para_def_copy().items():
        s = seleccion.get(addr, {})
        out.append({
            "nombre": cfg["name"], "monedero": _corta(addr), "escala": cfg["scale"],
            "perfil": f"https://polymarket.com/profile/{addr}",
            # Criterios de selección (README «Known-good trader selection»), medidos al elegirlo.
            "criterios": {"sharpe_30d": s.get("sharpe"), "pnl_30d": s.get("pnl_30d"),
                          "meses_activos_12": s.get("meses_activos_12"), "mercados_30d": s.get("mercados_30d"),
                          "ventas_30d": s.get("ventas_30d"), "merges_30d": s.get("merges_30d"),
                          "compras_30d": s.get("compras_30d"), "proporcion": s.get("proporcion"),
                          "posicion_mediana": s.get("posicion_mediana")},
            "copia": _resumen([t for t in trades if (t["wallet_copied"] or "").lower() == addr]),
            "movimientos": _movimientos(addr),
        })
    return out


def exportar(db: str, salida: Path) -> dict:
    import copy_wallets
    conn = sqlite3.connect(db)
    trades = _filas(conn, """SELECT id, wallet_copied, market_question, outcome_name, niche, entry_at, entry_price,
                                    entry_size_usdc, entry_shares, status, exit_at, exit_price, exit_reason,
                                    last_check_price, realized_pnl_net, fees_estimated
                             FROM paper_trades WHERE agent_id = ? AND status != 'cancelled'
                             ORDER BY entry_at""", AGENTE)
    conn.close()

    seguidos = copy_wallets.para_def_copy()
    trades = [t for t in trades if (t["wallet_copied"] or "").lower() in seguidos]
    nombre = lambda t: seguidos.get((t["wallet_copied"] or "").lower(), {}).get("name", _corta(t["wallet_copied"]))
    ahora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    traders = _traders(trades)
    recientes = sorted(trades, key=lambda t: t.get("exit_at") or t["entry_at"], reverse=True)[:40]
    progreso = {
        "actualizado": ahora,
        "ventana": {"inicio": (trades[0]["entry_at"] + "Z") if trades else None, "fin": ahora, "zona": "UTC"},
        "fuente": FUENTE,
        "aviso": "Simulación en papel con precios reales de Polymarket. No se arriesga dinero. "
                 "No es asesoramiento financiero.",
        "total": _resumen(trades),
        "traders": traders,
        "recientes": [{
            "trader": nombre(t), "mercado": t["market_question"], "apuesta": t["outcome_name"],
            "entrada": t["entry_at"] + "Z", "precio_entrada": t["entry_price"], "importe": t["entry_size_usdc"],
            "estado": t["status"], "salida": (t["exit_at"] + "Z") if t["exit_at"] else None,
            "precio_salida": t["exit_price"], "motivo_salida": t["exit_reason"],
            "realizado": round(_realizado(t), 2), "no_realizado": round(_no_realizado(t), 2),
        } for t in recientes],
        # Todas las copias en formato compacto para las gráficas: [precio, importe, entrada, realizado + latente, trader].
        "puntos": [[t["entry_price"], t["entry_size_usdc"], t["entry_at"] + "Z",
                    round(_realizado(t) + _no_realizado(t), 2), nombre(t)] for t in trades[-MAX_PUNTOS:]],
    }

    salida.mkdir(parents=True, exist_ok=True)
    (salida / "progreso.json").write_text(json.dumps(progreso, ensure_ascii=False, indent=1), encoding="utf-8")

    ruta_hist = salida / "historial.json"
    try:
        historial = json.loads(ruta_hist.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        historial = []
    # La curva es solo PnL realizado (LESSONS §7), por trader.
    historial.append({"t": ahora, **{tr["nombre"]: tr["copia"]["realizado"] for tr in traders}})
    ruta_hist.write_text(json.dumps(historial[-MAX_HISTORIAL:], separators=(",", ":")), encoding="utf-8")
    return progreso


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--salida", default=str(RAIZ / "publico"))
    p.add_argument("--db", default=os.getenv("POLY_DB_PATH", "polymarket_insider.db"))
    a = p.parse_args()
    db = a.db if os.path.isabs(a.db) else str(RAIZ / a.db)
    progreso = exportar(db, Path(a.salida))
    for tr in progreso["traders"]:
        c = tr["copia"]
        print(f"{tr['nombre']}: {c['operaciones']} copias · realizado {c['realizado']:+.2f} · "
              f"latente {c['no_realizado']:+.2f} USDC · {len(tr['movimientos'])} movimientos recientes")


if __name__ == "__main__":
    main()
