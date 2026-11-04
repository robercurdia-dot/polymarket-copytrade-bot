"""
Capa de compatibilidad con la Data API v2 de Polymarket.

La v1 (https://data-api.polymarket.com/{activity,positions,value,trades,holders,v1/leaderboard})
devuelve 410 desde noviembre de 2026. La v2 cambia tres cosas:
  * rutas bajo /v2/ y la respuesta envuelta en {"data": ..., "pagination": ...};
  * paginación solo por cursor (no hay `offset`);
  * campos en snake_case (proxy_wallet, condition_id, token_id, usdc_size...).

`get()` acepta la misma URL y los mismos parámetros que se le pasaban a `requests.get()` contra la v1 y
devuelve un objeto con `status_code`, `ok`, `json()`, `text` y `raise_for_status()` cuyo `json()` tiene la
forma de la v1 (lista de dicts con claves camelCase). Así cada llamada se migra cambiando `requests.get` por
`data_api.get`, sin tocar el código que lee la respuesta.
"""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qsl, urlsplit

import requests

BASE_V2 = "https://data-api.polymarket.com/v2"
_MAX_PAGINAS = 20

# Renombres de la v2 a los nombres de la v1, después de pasar snake_case a camelCase.
_RENOMBRES_COMUNES = {"tokenId": "asset", "oppositeTokenId": "oppositeAsset"}
_RENOMBRES = {
    "positions": {"currentSize": "size", "currentPrice": "curPrice", "entryCostUsdc": "initialValue",
                  "unrealizedPnl": "cashPnl", "totalSize": "totalBought"},
    "leaderboard": {"userId": "proxyWallet", "volume": "vol", "verified": "verifiedBadge"},
}
# Parámetros de la v1 que la v2 llama de otra forma.
_PARAMS = {"market": "condition"}
# sortBy de /positions: v1 -> v2 (CASHPNL de la v1 es el PnL no realizado).
_ORDEN_POSICIONES = {"CASHPNL": "UNREALIZED_PNL", "CURRENT": "CURRENT_VALUE", "TOKENS": "TOKENS"}


def _camel(clave: str) -> str:
    return re.sub(r"_([a-z0-9])", lambda m: m.group(1).upper(), clave)


def _a_v1(item, recurso: str):
    if isinstance(item, list):
        return [_a_v1(x, recurso) for x in item]
    if not isinstance(item, dict):
        return item
    renombres = {**_RENOMBRES_COMUNES, **_RENOMBRES.get(recurso, {})}
    out = dict(item)  # se conservan las claves originales de la v2
    for k, v in item.items():
        c = _camel(k)
        out[renombres.get(c, c)] = _a_v1(v, recurso) if isinstance(v, (list, dict)) else v
    if recurso == "holders":
        out.setdefault("token", item.get("token_id"))
    if recurso == "positions" and item.get("status") == "CLOSED":
        # Una posición cerrada ya no tiene PnL no realizado ni coste vivo: el resultado es el realizado y la
        # inversión, lo que se llegó a comprar.
        out["cashPnl"] = item.get("realized_pnl", 0)
        out["initialValue"] = float(item.get("total_size") or 0) * float(item.get("avg_price") or 0)
    return out


class _Respuesta:
    def __init__(self, status_code: int, payload, url: str):
        self.status_code = status_code
        self._payload = payload
        self.url = url

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400

    def json(self):
        return self._payload

    @property
    def text(self) -> str:
        return json.dumps(self._payload)

    def raise_for_status(self) -> None:
        if not self.ok:
            raise requests.HTTPError(f"{self.status_code} en {self.url}: {self._payload}")


def _recurso(ruta: str) -> str:
    ruta = ruta.rstrip("/")
    ruta = re.sub(r"^/(v1|v2)(?=/)", "", ruta)
    return ruta.lstrip("/")


def get(url: str, params: dict | None = None, timeout: float = 10, **kw) -> _Respuesta:
    """`requests.get` contra la Data API v1, servido por la v2 con la forma de la v1."""
    partes = urlsplit(url)
    recurso = _recurso(partes.path)
    p = dict(parse_qsl(partes.query, keep_blank_values=True))
    p.update({k: v for k, v in (params or {}).items() if v is not None})
    p = {_PARAMS.get(k, k): v for k, v in p.items()}

    offset = int(p.pop("offset", 0) or 0)
    limite = int(p.get("limit", 100) or 100)
    umbral = p.pop("sizeThreshold", None) if recurso == "positions" else None
    if recurso == "leaderboard" and str(p.get("orderBy", "")).upper() == "VOL":
        p["orderBy"] = "VOLUME"
    if recurso == "positions" and "sortBy" in p:
        orden = _ORDEN_POSICIONES.get(str(p["sortBy"]).upper())
        if orden:
            p["sortBy"] = orden
        else:  # la v2 rechaza los órdenes de la v1 que no tienen equivalente
            p.pop("sortBy")
            p.pop("sortDirection", None)
    # Se pide de una vez lo que la v1 devolvía con offset+limit y luego se recorta.
    p["limit"] = offset + limite
    destino = f"{BASE_V2}/{recurso}"
    sesion_kw = {k: v for k, v in kw.items() if k in ("verify", "headers")}

    datos, cursor = [], None
    for _ in range(_MAX_PAGINAS):
        q = dict(p, cursor=cursor) if cursor else p
        r = requests.get(destino, params=q, timeout=timeout, **sesion_kw)
        try:
            cuerpo = r.json()
        except ValueError:
            return _Respuesta(r.status_code, None, r.url)
        if r.status_code != 200:
            return _Respuesta(r.status_code, cuerpo, r.url)
        pagina = cuerpo.get("data") if isinstance(cuerpo, dict) else cuerpo
        if not isinstance(pagina, list):
            # /value devuelve un objeto; la v1 devolvía [{"user", "value"}].
            if recurso == "value" and isinstance(pagina, dict):
                return _Respuesta(200, [{"user": pagina.get("proxy_wallet"), "value": pagina.get("value")}], r.url)
            return _Respuesta(200, _a_v1(pagina, recurso) if pagina is not None else [], r.url)
        datos.extend(pagina)
        cursor = (cuerpo.get("pagination") or {}).get("next_cursor")
        if not cursor or len(datos) >= offset + limite:
            break

    datos = _a_v1(datos[offset:offset + limite], recurso)
    if umbral is not None:
        try:
            u = float(umbral)
            datos = [d for d in datos if float(d.get("size") or 0) >= u]
        except ValueError:
            pass
    return _Respuesta(200, datos, destino)
