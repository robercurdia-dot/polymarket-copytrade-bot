"""Elige los monederos a copiar con los tres criterios del README («Known-good trader selection»).

    python tools/seleccionar_wallets.py [--n 3] [--candidatos 250] [--escribir-env]

Candidatos: el leaderboard de Polymarket (Data API v2), mensual y de siempre, ordenado por PnL.
Un monedero pasa si cumple los tres criterios:

  1. Sharpe del periodo > 1 sobre el PnL realizado de los últimos 30 días:
     PnL realizado de cada día (posiciones cerradas por último movimiento, días sin cierres = 0),
     media / desviación típica × √30. Exige al menos --min-cierres posiciones cerradas en la ventana.
  2. Al menos un año de actividad constante y no ser una ballena de un solo mercado:
     actividad hace más de 365 días, actividad en ≥ --min-meses de los últimos 12 meses,
     ≥ --min-mercados mercados distintos en 30 días y ningún mercado con más del --max-cuota del volumen.
  3. Disciplina de salida: en 30 días, ventas + merges ≥ --min-salidas y ≥ --min-ratio-salida de las compras.

Los que pasan se ordenan por Sharpe y se quedan los --n primeros. La escala de cada uno se fija para que su
compra mediana se copie por --objetivo-usdc (README: tamaño = lo que compra el trader × ESCALA).
Con --escribir-env guarda COPY_WALLETS y las whitelists en el .env del repo (está en .gitignore).
"""
import argparse
import json
import math
import re
import statistics
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

RAIZ = Path(__file__).resolve().parent.parent
V2 = "https://data-api.polymarket.com/v2"
DIA = 86400


def _get(ruta: str, **params):
    """GET a la v2 con reintentos ante 429/5xx. Devuelve el JSON o None."""
    for intento in range(4):
        try:
            r = requests.get(f"{V2}/{ruta}", params=params, timeout=20)
        except requests.RequestException:
            time.sleep(1 + intento)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(float(r.headers.get("Retry-After", 1 + intento)))
            continue
        return r.json() if r.status_code == 200 else None
    return None


def _todas(ruta: str, max_paginas: int = 10, **params) -> list:
    """Recorre la paginación por cursor de la v2."""
    datos, cursor = [], None
    for _ in range(max_paginas):
        q = dict(params, cursor=cursor) if cursor else params
        cuerpo = _get(ruta, **q)
        if not cuerpo or not isinstance(cuerpo.get("data"), list):
            break
        datos.extend(cuerpo["data"])
        cursor = (cuerpo.get("pagination") or {}).get("next_cursor")
        if not cursor:
            break
    return datos


def candidatos(n: int) -> dict:
    vistos = {}
    for periodo, cuantos in (("month", n), ("all", n // 2)):
        for t in _todas("leaderboard", max_paginas=5, timePeriod=periodo, orderBy="PNL", limit=min(cuantos, 200)):
            vistos.setdefault(t["user_id"].lower(), t.get("user_name") or "")
            if len(vistos) >= n * 1.5:
                break
    return vistos


def sharpe_30d(addr: str, ahora: int) -> dict:
    cerradas = _todas("positions", max_paginas=3, user=addr, status="CLOSED", limit=500)
    desde = ahora - 30 * DIA
    por_dia = defaultdict(float)
    n = 0
    for p in cerradas:
        t = p.get("last_event_at") or 0
        if t >= desde and p.get("realized_pnl") is not None:
            por_dia[(t - desde) // DIA] += float(p["realized_pnl"])
            n += 1
    serie = [por_dia.get(d, 0.0) for d in range(30)]
    sd = statistics.pstdev(serie)
    sharpe = (statistics.mean(serie) / sd * math.sqrt(30)) if sd > 0 else 0.0
    return {"sharpe": round(sharpe, 2), "cierres_30d": n, "pnl_30d": round(sum(serie), 2)}


def antiguedad(addr: str, ahora: int) -> dict:
    hace_un_ano = _get("activity", user=addr, end=ahora - 365 * DIA, limit=1)
    meses = 0
    for m in range(12):
        fin, ini = ahora - m * 30 * DIA, ahora - (m + 1) * 30 * DIA
        r = _get("activity", user=addr, start=ini, end=fin, limit=1)
        meses += bool(r and r.get("data"))
    return {"mas_de_un_ano": bool(hace_un_ano and hace_un_ano.get("data")), "meses_activos_12": meses}


def actividad_30d(addr: str, ahora: int) -> dict:
    act = _todas("activity", max_paginas=8, user=addr, start=ahora - 30 * DIA, end=ahora, limit=500)
    compras = [a for a in act if a.get("type") == "TRADE" and a.get("side") == "BUY"]
    ventas = [a for a in act if a.get("type") == "TRADE" and a.get("side") == "SELL"]
    merges = [a for a in act if a.get("type") == "MERGE"]
    vol = defaultdict(float)
    for a in compras + ventas:
        vol[a.get("condition_id")] += float(a.get("usdc_size") or 0)
    total = sum(vol.values()) or 1.0
    # Tamaño de referencia: lo que compra en total en cada mercado (una posición se construye con muchas órdenes
    # sueltas; la mediana por orden infravalora su tamaño real y daría una escala > 1).
    por_mercado = defaultdict(float)
    for a in compras:
        por_mercado[a.get("condition_id")] += float(a.get("usdc_size") or 0)
    importes = sorted(v for v in por_mercado.values() if v > 0)
    return {
        "compras_30d": len(compras), "ventas_30d": len(ventas), "merges_30d": len(merges),
        "mercados_30d": len(vol), "cuota_max_mercado": round(max(vol.values()) / total, 3) if vol else 1.0,
        "posicion_mediana": round(statistics.median(importes), 2) if importes else 0.0,
        "truncado": len(act) >= 8 * 500,
    }


def evaluar(addr: str, nombre: str, a, ahora: int) -> dict:
    fila = {"monedero": addr, "nombre": nombre, "pasa": False, "motivo": ""}
    fila.update(sharpe_30d(addr, ahora))
    if fila["cierres_30d"] < a.min_cierres or fila["sharpe"] <= 1:
        fila["motivo"] = "sharpe" if fila["cierres_30d"] >= a.min_cierres else "pocos cierres"
        return fila
    fila.update(antiguedad(addr, ahora))
    if not fila["mas_de_un_ano"] or fila["meses_activos_12"] < a.min_meses:
        fila["motivo"] = "antigüedad"
        return fila
    fila.update(actividad_30d(addr, ahora))
    if fila["mercados_30d"] < a.min_mercados or fila["cuota_max_mercado"] > a.max_cuota:
        fila["motivo"] = "concentrado"
        return fila
    salidas = fila["ventas_30d"] + fila["merges_30d"]
    if salidas < a.min_salidas or salidas < a.min_ratio_salida * max(1, fila["compras_30d"]):
        fila["motivo"] = "pocas salidas"
        return fila
    fila["pasa"] = True
    return fila


def _escribir_env(valores: dict) -> None:
    ruta = RAIZ / ".env"
    texto = ruta.read_text(encoding="utf-8") if ruta.exists() else ""
    for clave, valor in valores.items():
        linea = f"{clave}={valor}"
        if re.search(rf"^{clave}=.*$", texto, re.M):
            texto = re.sub(rf"^{clave}=.*$", lambda _m: linea, texto, flags=re.M)
        else:
            texto += ("" if texto.endswith("\n") or not texto else "\n") + linea + "\n"
    ruta.write_text(texto, encoding="utf-8")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--n", type=int, default=3)
    p.add_argument("--candidatos", type=int, default=250)
    p.add_argument("--min-cierres", type=int, default=15)
    p.add_argument("--min-meses", type=int, default=10)
    p.add_argument("--min-mercados", type=int, default=15)
    p.add_argument("--max-cuota", type=float, default=0.25)
    p.add_argument("--min-salidas", type=int, default=10)
    p.add_argument("--min-ratio-salida", type=float, default=0.10)
    p.add_argument("--objetivo-usdc", type=float, default=10.0)
    p.add_argument("--escribir-env", action="store_true")
    p.add_argument("--salida", default=str(RAIZ / "sim" / "seleccion.json"))
    a = p.parse_args()

    ahora = int(time.time())
    cands = candidatos(a.candidatos)
    print(f"Candidatos del leaderboard: {len(cands)}", flush=True)
    with ThreadPoolExecutor(max_workers=6) as pool:
        filas = list(pool.map(lambda kv: evaluar(kv[0], kv[1], a, ahora), cands.items()))

    descartes = defaultdict(int)
    for f in filas:
        if not f["pasa"]:
            descartes[f["motivo"]] += 1
    pasan = sorted((f for f in filas if f["pasa"]), key=lambda f: -f["sharpe"])
    elegidos = pasan[:a.n]
    for f in elegidos:
        f["escala"] = round(a.objetivo_usdc / f["posicion_mediana"], 6) if f["posicion_mediana"] else 0.01

    print(f"Pasan los tres criterios: {len(pasan)} · descartes: {dict(descartes)}")
    for f in elegidos:
        print(f"  {f['monedero']}  {f['nombre'][:20]:20}  sharpe {f['sharpe']:5.2f}  pnl30d {f['pnl_30d']:>11,.0f}  "
              f"meses {f['meses_activos_12']}/12  mercados {f['mercados_30d']}  "
              f"salidas {f['ventas_30d']}+{f['merges_30d']}m/{f['compras_30d']}c  escala {f['escala']}")

    Path(a.salida).parent.mkdir(parents=True, exist_ok=True)
    Path(a.salida).write_text(json.dumps({"fecha": ahora, "criterios": vars(a), "elegidos": elegidos,
                                          "pasan": pasan, "descartes": descartes}, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    if a.escribir_env and elegidos:
        nombre = lambda f: re.sub(r"[^A-Za-z0-9_.-]", "", f["nombre"])[:24] or f["monedero"][:8]
        copia = ",".join(f"{f['monedero']}:{nombre(f)}:{f['escala']}" for f in elegidos)
        lista = ",".join(f["monedero"] for f in elegidos)
        _escribir_env({"COPY_WALLETS": copia, "REAL_D_WALLET_WHITELIST": lista, "REAL_WALLET_WHITELIST": lista})
        print(f"Escrito en {RAIZ / '.env'}: COPY_WALLETS y whitelists")


if __name__ == "__main__":
    main()
