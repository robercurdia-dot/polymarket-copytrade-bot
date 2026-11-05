"""
Detección de operaciones directamente en la cadena de Polygon.

La Data API tarda entre 3 y 10 s en mostrar una operación (mediana ~7 s, medido el 2026-11-05). La cadena la
registra en el bloque en que ocurre (uno cada ~2 s), así que leyendo los bloques nuevos vemos la operación del
trader al confirmarse. La Data API queda como respaldo: si el RPC falla, el sondeo de siempre sigue funcionando.

Cada orden ejecutada en el exchange de Polymarket emite un evento de llenado con el creador de la orden como
`maker` (tanto si su orden entró contra el libro como si estaba en él), así que filtrar por maker cubre todas
las operaciones de un monedero sin contarlas dos veces. Formato del evento (exchange actual, verificado contra
la Data API en 50 operaciones):

    topics: [TOPIC_LLENADO, hash de la orden, maker, taker]
    data:   [lado (0 compra, 1 venta), token, cantidad entregada, cantidad recibida, comisión, ·, ·]

En una compra el maker entrega USDC y recibe participaciones; en una venta, al revés. Todo con 6 decimales.
"""
from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque

import requests

RPC = os.getenv("POLYGON_RPC_URL", "https://polygon-bor-rpc.publicnode.com")
EXCHANGES = [a.strip().lower() for a in os.getenv(
    "POLY_EXCHANGES",
    "0xe111180000d2663c0091e4f400237545b87b996b,0xe2222d279d744050d28e00520010520000310f59").split(",") if a.strip()]
TOPIC_LLENADO = "0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee"
MAX_BLOQUES = 40          # rango máximo por consulta; si nos quedamos atrás se avanza por tramos
_TIMEOUT = 8

_mercados: dict[str, dict] = {}
_ventas: dict[str, deque] = defaultdict(lambda: deque(maxlen=500))
_cerrojo = threading.Lock()


def _rpc(metodo: str, params: list):
    r = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": metodo, "params": params}, timeout=_TIMEOUT)
    cuerpo = r.json()
    if "error" in cuerpo:
        raise RuntimeError(f"RPC {metodo}: {cuerpo['error']}")
    return cuerpo["result"]


def bloque_actual() -> int:
    return int(_rpc("eth_blockNumber", []), 16)


def _fecha_bloque(numero: int, cache: dict) -> int:
    if numero not in cache:
        cache[numero] = int(_rpc("eth_getBlockByNumber", [hex(numero), False])["timestamp"], 16)
    return cache[numero]


def mercado(token_id: str) -> dict:
    """conditionId, título, slug y nombre del resultado de un token (Gamma, con caché)."""
    if token_id in _mercados:
        return _mercados[token_id]
    info = {}
    try:
        r = requests.get("https://gamma-api.polymarket.com/markets", params={"clob_token_ids": token_id}, timeout=_TIMEOUT)
        m = (r.json() or [{}])[0] if r.status_code == 200 else {}
        import json as _json
        tokens = _json.loads(m.get("clobTokenIds") or "[]")
        resultados = _json.loads(m.get("outcomes") or "[]")
        idx = tokens.index(token_id) if token_id in tokens else None
        info = {"conditionId": m.get("conditionId"), "title": m.get("question"), "slug": m.get("slug"),
                "outcome": resultados[idx] if idx is not None and idx < len(resultados) else None,
                "outcomeIndex": idx}
    except Exception:
        pass
    if info.get("conditionId"):
        _mercados[token_id] = info
    return info


def leer(desde: int, hasta: int, monederos: set[str]) -> list[dict]:
    """Operaciones de `monederos` en los bloques [desde, hasta], agregadas por (tx, monedero, token) y con la
    forma de la Data API v1 que usa el monitor (proxyWallet, side, asset, conditionId, size, usdcSize, price...)."""
    filtro_maker = ["0x" + "0" * 24 + m[2:].lower() for m in monederos]
    logs = _rpc("eth_getLogs", [{"fromBlock": hex(desde), "toBlock": hex(hasta), "address": EXCHANGES,
                                 "topics": [TOPIC_LLENADO, None, filtro_maker]}])
    agregadas: dict[tuple, dict] = {}
    fechas: dict[int, int] = {}
    for l in logs:
        maker = "0x" + l["topics"][2][-40:]
        d = l["data"][2:]
        w = [int(d[i:i + 64], 16) for i in range(0, len(d), 64)]
        lado = "BUY" if w[0] == 0 else "SELL"
        token = str(w[1])
        acciones, usdc = (w[3], w[2]) if lado == "BUY" else (w[2], w[3])
        clave = (l["transactionHash"], maker, token, lado)
        op = agregadas.setdefault(clave, {
            "proxyWallet": maker, "side": lado, "asset": token, "size": 0.0, "usdcSize": 0.0,
            "transactionHash": l["transactionHash"], "type": "TRADE",
            "timestamp": _fecha_bloque(int(l["blockNumber"], 16), fechas), "fuente": "cadena",
        })
        op["size"] += acciones / 1e6
        op["usdcSize"] += usdc / 1e6
    out = []
    for op in agregadas.values():
        op["price"] = round(op["usdcSize"] / op["size"], 6) if op["size"] else 0.0
        op.update({k: v for k, v in mercado(op["asset"]).items() if v is not None})
        if op.get("conditionId"):
            out.append(op)
    return out


def guardar_venta(op: dict) -> None:
    """Las ventas detectadas en la cadena se guardan para que el watcher las aplique sin esperar a la Data API."""
    with _cerrojo:
        _ventas[op["proxyWallet"].lower()].append(op)


def ventas_desde(monedero: str, desde_ts: int) -> list[dict]:
    with _cerrojo:
        return [v for v in _ventas.get(monedero.lower(), ()) if int(v["timestamp"]) >= desde_ts]
