"""
my_wallet_tracker.py — Rastrea la wallet propia del usuario en Polymarket
=========================================================================
Consulta la Data API de Polymarket para obtener:
  - Posiciones abiertas (con valor actual y PnL latente)
  - Posiciones cerradas (con PnL realizado)

Se llama desde data_exporter.py cada 60s.
No requiere autenticación — usa la dirección pública de la wallet.
"""

import time
import requests
from datetime import datetime
from collections import defaultdict

import config

_DATA_API     = "https://data-api.polymarket.com"
_GAMMA_API    = "https://gamma-api.polymarket.com"
_CLOB_API     = "https://clob.polymarket.com"
_USER_PNL_API = "https://user-pnl-api.polymarket.com"
_TIMEOUT      = 15

# Cache PnL oficial (60s) — endpoint user-pnl-api se actualiza horariamente
_OFFICIAL_PNL_CACHE_TTL = 60
_official_pnl_cache: dict = {"value": None, "ts": 0.0}

# Hook de testing: si está seteado, _fetch_official_pnl lo invoca para simular fallo
_official_pnl_force_fail: bool = False

WALLET = config.POLY_WALLET_ADDRESS  # desde .env

# Cache en memoria para /activity — evita pegar a la API en cada export (60s)
_ACTIVITY_CACHE_TTL = 300  # 5 min
_activity_cache = {"data": None, "ts": 0.0}

# Cache permanente: una vez que un mercado está closed=true, no vuelve a abrir.
# Solo se reconsulta CLOB para los cids con respuesta False/None (mercado abierto
# o error transitorio). Persiste durante la vida del proceso.
_market_closed_cache: dict = {}


def _fetch_positions(status: str = "OPEN", limit: int = 500) -> list:
    """
    Obtiene posiciones de la wallet del usuario.
    status: 'OPEN' (snapshot abiertas) o 'CLOSED' (DEPRECATED — el flag
    `redeemed=true` no captura ventas pre-resolución; usar
    _fetch_closed_from_activity en su lugar).
    """
    if not WALLET:
        return []
    try:
        params = {
            "user":          WALLET,
            "limit":         limit,
            "sortBy":        "CASHPNL",
            "sortDirection": "DESC",
        }
        if status == "CLOSED":
            params["redeemed"] = "true"

        r = requests.get(
            f"{_DATA_API}/positions",
            params=params,
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"[MY_WALLET] Error fetching {status} positions: {e}")
        return []


def _parse_position(pos: dict, is_open: bool) -> dict:
    """Convierte una posición raw de la API en un dict limpio."""
    condition_id = pos.get("conditionId") or pos.get("market", {}).get("conditionId", "")

    # Datos de mercado (pueden venir embebidos)
    market_info = pos.get("market") or {}
    question    = (
        market_info.get("question")
        or pos.get("title")
        or pos.get("marketSlug")
        or ""
    )

    outcome  = pos.get("outcome") or pos.get("side") or "YES"
    size     = float(pos.get("size") or pos.get("sharesOwned") or 0)
    avg_price= float(pos.get("avgPrice") or pos.get("pricePerShare") or 0)
    cur_price= float(pos.get("curPrice") or pos.get("currentPrice") or avg_price)

    # PnL
    cash_pnl = float(pos.get("cashPnl") or pos.get("pnl") or 0)

    # Valor actual de la posición
    cur_value = float(pos.get("curValue") or pos.get("currentValue") or (size * cur_price))

    # Fechas
    opened_at  = pos.get("startDate") or pos.get("createdAt") or ""
    resolved_at= pos.get("endDate") or pos.get("resolvedAt") or ""

    # Estado
    if not is_open:
        won_flag = cash_pnl > 0
        status   = "WON" if won_flag else "LOST"
    else:
        won_flag = None
        status   = "OPEN"

    return {
        "condition_id": condition_id,
        "market":       (question or condition_id)[:80],
        "outcome":      outcome.upper(),
        "size":         round(size, 2),
        "avg_price":    round(avg_price, 4),
        "cur_price":    round(cur_price, 4),
        "cur_value":    round(cur_value, 2),
        "cash_pnl":     round(cash_pnl, 2),
        "status":       status,
        "won":          won_flag,
        "opened_at":    opened_at,
        "resolved_at":  resolved_at,
    }


def _fetch_activity(limit_total: int = 2000, page_size: int = 500) -> list:
    """
    Descarga el historial completo de eventos de la wallet desde /activity.
    Pagina con offset hasta vaciar o llegar al cap. Cachea 5 min en memoria.

    Devuelve lista plana de eventos: TRADE (BUY/SELL) y REDEEM.
    """
    if not WALLET:
        return []

    now = time.time()
    if _activity_cache["data"] is not None and (now - _activity_cache["ts"]) < _ACTIVITY_CACHE_TTL:
        return _activity_cache["data"]

    events: list = []
    offset = 0
    while len(events) < limit_total:
        try:
            r = requests.get(
                f"{_DATA_API}/activity",
                params={
                    "user":   WALLET,
                    "limit":  page_size,
                    "offset": offset,
                },
                timeout=_TIMEOUT,
            )
            r.raise_for_status()
            page = r.json()
            if not isinstance(page, list) or not page:
                break
            events.extend(page)
            if len(page) < page_size:
                break  # última página
            offset += page_size
        except Exception as e:
            print(f"[MY_WALLET] Error fetching activity (offset={offset}): {e}")
            break

    events = events[:limit_total]
    _activity_cache["data"] = events
    _activity_cache["ts"]   = now
    return events


def _is_market_closed(condition_id: str) -> bool:
    """
    Verifica vía CLOB si el mercado ya resolvió (closed=true). Cachea resultados
    True permanentemente (un mercado cerrado no vuelve a abrirse). Resultados
    False/None se reconsultan en la siguiente llamada — un mercado abierto puede
    cerrar más tarde.

    Nota: se intentó gamma-api/markets/{cid} pero devuelve 422 'id is invalid';
    CLOB es el endpoint que sí indexa por conditionId.
    """
    if not condition_id:
        return False
    if _market_closed_cache.get(condition_id):
        return True
    try:
        r = requests.get(
            f"{_CLOB_API}/markets/{condition_id}",
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            return False
        closed = bool(r.json().get("closed"))
        if closed:
            _market_closed_cache[condition_id] = True
        return closed
    except Exception as e:
        print(f"[MY_WALLET] Error verificando closed para {condition_id[:14]}…: {e}")
        return False


def _aggregate_closed(activity: list, open_position_keys: set) -> list:
    """
    Agrupa eventos de /activity por (conditionId, outcome) y deriva posiciones
    cerradas. Reescrito en Pasada B (Fase 1.2, 2026-05-02) para corregir:
      1. Threshold del 99% que descartaba 38 posiciones con dust residual.
      2. Agregación por solo conditionId que fusionaba YES+NO en multi-outcome.
      3. Falta de clasificación de tipo de cierre (necesario para
         my_behavior_analyzer en Fase 2.2).

    Criterio de "cerrada" (más permisivo y robusto):
      - (cid, outcome) NO está en open_position_keys, Y
      - una de:
          * sold_pct >= 0.50                    (vendió mayoría — antes era 99%)
          * redeem_attr > 0                     (cobró payout)
          * cid tiene cualquier REDEEM event    (mercado resuelto on-chain)
          * _is_market_closed(cid) == True      (CLOB confirma cerrado)

    Atribución de REDEEM a outcomes:
      Los REDEEM events traen `outcome=""` y `outcomeIndex=999`. Se reparte el
      `redeem_total_usdc` del cid proporcionalmente a las shares pendientes
      (buy_size - sell_size) por outcome. Para cids con un solo outcome
      comprado (>98% de los casos) esto es trivialmente correcto. Multi-outcome
      (cid con BUY YES + BUY NO) tiene atribución aproximada — fix completo
      pendiente Fase 1.4.

    Clasificación `close_type` (4 sub-categorías):
      - redeem_win    : redeem_attr > 0 AND pnl > 0
      - sell_profit   : sell_usdc > 0 AND redeem_attr == 0 AND pnl > 0
      - sell_loss     : sell_usdc > 0 AND pnl < 0
      - redeem_loss   : pnl <= 0 sin SELL (sólo BUY perdió, con/sin redeem $0)

    PnL realizado = sell_usdc + redeem_attr - buy_usdc.
    """
    # ── Paso 1: agregar TRADE por (cid, outcome) y REDEEM por cid ────────
    by_key: dict = defaultdict(lambda: {
        "title":      "",
        "buy_usdc":   0.0,
        "buy_size":   0.0,
        "sell_usdc":  0.0,
        "sell_size":  0.0,
        "last_ts":    0,
        "first_ts":   10**12,
    })
    redeem_by_cid: dict = defaultdict(lambda: {"total_usdc": 0.0, "n_events": 0, "last_ts": 0})

    for ev in activity:
        cid = ev.get("conditionId")
        if not cid:
            continue
        ts = int(ev.get("timestamp") or 0)
        t = ev.get("type")
        usdc = float(ev.get("usdcSize") or 0)
        sz   = float(ev.get("size") or 0)

        if t == "TRADE":
            outcome = (ev.get("outcome") or "").upper().strip()
            key = (cid, outcome)
            e = by_key[key]
            if not e["title"]:
                e["title"] = ev.get("title") or ""
            if ts > e["last_ts"]: e["last_ts"] = ts
            if ts and ts < e["first_ts"]: e["first_ts"] = ts
            side = ev.get("side")
            if side == "BUY":
                e["buy_usdc"] += usdc
                e["buy_size"] += sz
            elif side == "SELL":
                e["sell_usdc"] += usdc
                e["sell_size"] += sz
        elif t in ("REDEEM", "REWARD", "CLAIM"):
            r = redeem_by_cid[cid]
            r["total_usdc"] += usdc
            r["n_events"] += 1
            if ts > r["last_ts"]: r["last_ts"] = ts

    # ── Paso 2: pre-calcular shares restantes por cid para repartir REDEEM ─
    remaining_by_cid: dict = defaultdict(float)
    for (cid, _), v in by_key.items():
        remaining_by_cid[cid] += max(0.0, v["buy_size"] - v["sell_size"])

    closed: list = []
    for (cid, outcome), v in by_key.items():
        # Sigue abierta — la mantenemos en open_positions
        if (cid, outcome) in open_position_keys:
            continue
        if v["buy_size"] <= 0:
            continue  # ruido (no hay BUY trail)

        # Atribución del REDEEM total del cid a este outcome
        cid_redeem = redeem_by_cid.get(cid, {"total_usdc": 0.0, "n_events": 0})
        my_remaining = max(0.0, v["buy_size"] - v["sell_size"])
        cid_remaining = remaining_by_cid[cid]
        if cid_redeem["total_usdc"] > 0 and cid_remaining > 0:
            redeem_attr = cid_redeem["total_usdc"] * (my_remaining / cid_remaining)
        else:
            redeem_attr = 0.0

        sold_pct = (v["sell_size"] / v["buy_size"]) if v["buy_size"] > 0 else 0.0

        # Criterio de cerrada (la primaria es: no está en /positions abiertas)
        market_resolved_via_redeem = cid_redeem["n_events"] > 0
        is_closed = (
            sold_pct >= 0.50
            or redeem_attr > 0
            or market_resolved_via_redeem
            or _is_market_closed(cid)
        )
        if not is_closed:
            continue

        pnl = v["sell_usdc"] + redeem_attr - v["buy_usdc"]

        # ── Clasificación close_type ─────────────────────────────────
        if redeem_attr > 0 and pnl > 0:
            close_type = "redeem_win"
        elif v["sell_usdc"] > 0 and redeem_attr == 0 and pnl > 0:
            close_type = "sell_profit"
        elif v["sell_usdc"] > 0 and pnl < 0:
            close_type = "sell_loss"
        else:
            close_type = "redeem_loss"  # buy-only loss (con o sin redeem $0)

        won = pnl > 0
        last_ts = max(v["last_ts"], cid_redeem.get("last_ts", 0))
        resolved_at = (datetime.utcfromtimestamp(last_ts).strftime("%Y-%m-%d")
                       if last_ts else "")
        opened_at = (datetime.utcfromtimestamp(v["first_ts"]).strftime("%Y-%m-%d")
                     if v["first_ts"] != 10**12 else "")
        avg_buy_price = v["buy_usdc"] / v["buy_size"] if v["buy_size"] > 0 else 0.0

        closed.append({
            "condition_id":  cid,
            "market":        (v["title"] or cid)[:80],
            "outcome":       outcome,
            "size":          round(v["buy_size"], 2),
            "avg_price":     round(avg_buy_price, 4),
            "cur_price":     0.0,
            "cur_value":     0.0,
            "cash_pnl":      round(pnl, 2),
            "status":        "WON" if won else "LOST",
            "won":           won,
            "close_type":    close_type,
            "sold_pct":      round(sold_pct, 4),
            "redeem_usdc":   round(redeem_attr, 2),
            "buy_usdc":      round(v["buy_usdc"], 2),
            "sell_usdc":     round(v["sell_usdc"], 2),
            "opened_at":     opened_at,
            "resolved_at":   resolved_at,
            "_last_ts":      last_ts,
        })

    closed.sort(key=lambda x: x.get("_last_ts", 0), reverse=True)
    for c in closed:
        c.pop("_last_ts", None)
    return closed


def _fetch_official_pnl(wallet: str) -> float | None:
    """
    Lee el PnL all-time desde el endpoint oficial de Polymarket
    (user-pnl-api.polymarket.com). Es la **fuente de verdad única** para
    `total_pnl` — coincide bit-a-bit con la UI de polymarket.com.

    Cachea 60s en memoria. Devuelve None ante cualquier fallo (timeout,
    HTTP error, JSON vacío) — el caller decide fallback a cálculo local.

    Endpoint: GET /user-pnl?user_address={proxy}&interval=all
    Respuesta: [{"t": unix_ts, "p": pnl_acumulado_usdc}, ...] ordenado.
    El último punto es el PnL all-time actual.
    """
    if not wallet:
        return None

    # Hook de testing — fuerza fallo controlado
    if _official_pnl_force_fail:
        return None

    now = time.time()
    if (
        _official_pnl_cache["value"] is not None
        and (now - _official_pnl_cache["ts"]) < _OFFICIAL_PNL_CACHE_TTL
    ):
        return _official_pnl_cache["value"]

    try:
        r = requests.get(
            f"{_USER_PNL_API}/user-pnl",
            params={"user_address": wallet, "interval": "all"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list) or not data:
            return None
        last = data[-1]
        p = last.get("p")
        if p is None:
            return None
        value = float(p)
        _official_pnl_cache["value"] = value
        _official_pnl_cache["ts"]    = now
        return value
    except Exception as e:
        print(f"[MY_WALLET] Error fetching official PnL: {e}")
        return None


def get_my_wallet_data() -> dict:
    """
    Función principal. Devuelve un dict con:
      - open_positions: lista de posiciones abiertas
      - closed_positions: lista de posiciones cerradas (últimas 100)
      - summary: estadísticas agregadas
    """
    if not WALLET:
        return _empty_result("POLY_WALLET_ADDRESS no configurado")

    open_raw = _fetch_positions(status="OPEN", limit=500)
    all_open = [_parse_position(p, is_open=True) for p in open_raw]

    # Filtrar posiciones "abiertas pero ya perdidas" (cur_price ≈ 0, percent_pnl < -90%):
    # son posiciones cuyo mercado resolvió en contra y siguen apareciendo en /positions
    # solo porque el usuario aún no ha llamado a redeem on-chain. Reclasificarlas
    # como cerradas (redeem_loss) refleja mejor la realidad sin alterar el total.
    open_positions = []
    pending_redeem_losses = []
    for p, raw in zip(all_open, open_raw):
        cur_price = float(raw.get("curPrice") or raw.get("currentPrice") or 0)
        percent_pnl = float(raw.get("percentPnl") or 0)
        if cur_price < 0.01 and percent_pnl < -90:
            # Pérdida latente equivale a -cost_basis; clasificar como redeem_loss
            cost_basis = p["size"] * p["avg_price"]
            pending_redeem_losses.append({
                "condition_id":  p["condition_id"],
                "market":        p["market"],
                "outcome":       p["outcome"],
                "size":          p["size"],
                "avg_price":     p["avg_price"],
                "cur_price":     cur_price,
                "cur_value":     0.0,
                "cash_pnl":      round(p["cash_pnl"], 2),  # ~ -cost_basis
                "status":        "LOST",
                "won":           False,
                "close_type":    "redeem_loss",
                "sold_pct":      0.0,
                "redeem_usdc":   0.0,
                "buy_usdc":      round(cost_basis, 2),
                "sell_usdc":     0.0,
                "opened_at":     p["opened_at"],
                "resolved_at":   "",
                "_pending_onchain_redeem": True,
            })
        else:
            open_positions.append(p)

    # Cerradas: derivadas de /activity, agrupadas por (cid, outcome).
    # La señal primaria de "cerrada" es: no aparece en /positions abiertas O
    # aparece pero a precio ~$0 (perdida pendiente de redeem on-chain).
    open_position_keys = {
        (p["condition_id"], (p["outcome"] or "").upper().strip())
        for p in open_positions if p["condition_id"]
    }
    activity = _fetch_activity(limit_total=2000)
    closed_from_activity = _aggregate_closed(activity, open_position_keys)
    # Combinar cerradas reales + pending_redeem_losses (evitando duplicados por cid+outcome)
    seen_keys = {(c["condition_id"], c["outcome"]) for c in closed_from_activity}
    closed_positions = closed_from_activity + [
        p for p in pending_redeem_losses
        if (p["condition_id"], p["outcome"]) not in seen_keys
    ]
    # Reordenar por más reciente
    closed_positions.sort(key=lambda x: x.get("resolved_at") or x.get("opened_at") or "", reverse=True)

    # Estadísticas
    total_open_value = sum(p["cur_value"] for p in open_positions)
    total_open_pnl   = sum(p["cash_pnl"] for p in open_positions)

    won_count  = sum(1 for p in closed_positions if p["won"])
    lost_count = sum(1 for p in closed_positions if not p["won"])
    resolved   = won_count + lost_count
    win_rate   = round(won_count / resolved * 100, 1) if resolved > 0 else 0
    closed_pnl = sum(p["cash_pnl"] for p in closed_positions)

    # PnL oficial (fuente de verdad). Si falla → fallback al cálculo local.
    # Ver REGLA INMUTABLE 1 en context.md.
    official_pnl     = _fetch_official_pnl(WALLET)
    computed_pnl     = round(total_open_pnl + closed_pnl, 2)
    if official_pnl is not None:
        total_pnl        = round(official_pnl, 2)
        total_pnl_source = "official"
    else:
        total_pnl        = computed_pnl
        total_pnl_source = "computed_fallback"

    # open_pnl / closed_pnl: cálculo local desde /activity + cashPnl latente.
    # NO autoritativo (ver REGLA INMUTABLE 2 refinada). Pueden diferir hasta
    # ~$30 del official_pnl. Fuente de verdad sigue siendo user-pnl-api.
    summary = {
        "wallet":             WALLET,
        "wallet_short":       WALLET[:8] + "…" + WALLET[-4:],
        "open_count":         len(open_positions),
        "closed_count":       resolved,
        "won":                won_count,
        "lost":               lost_count,
        "win_rate":           win_rate,
        "total_open_value":   round(total_open_value, 2),
        "open_pnl":           round(total_open_pnl, 2),
        "closed_pnl":         round(closed_pnl, 2),
        "_pnl_breakdown_note": "estimado_de_actividad",  # open_pnl/closed_pnl no son autoritativos
        "total_pnl":          total_pnl,
        "total_pnl_source":   total_pnl_source,
        "last_updated":       datetime.now().isoformat(),
        "empty":              (len(open_positions) + resolved) == 0,
    }

    return {
        "summary":           summary,
        "open_positions":    open_positions,
        "closed_positions":  closed_positions[:50],  # últimas 50 en el JSON
    }


def _empty_result(reason: str = "") -> dict:
    return {
        "summary": {
            "wallet":           WALLET or "",
            "wallet_short":     (WALLET[:8] + "…" + WALLET[-4:]) if WALLET else "",
            "open_count":       0,
            "closed_count":     0,
            "won":              0,
            "lost":             0,
            "win_rate":         0,
            "total_open_value": 0,
            "open_pnl":         0,
            "closed_pnl":       0,
            "total_pnl":        0,
            "total_pnl_source": "computed_fallback",
            "last_updated":     datetime.now().isoformat(),
            "empty":            True,
            "reason":           reason,
        },
        "open_positions":   [],
        "closed_positions": [],
    }


# ── Test standalone ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    print("=" * 55)
    print(f"MY WALLET TRACKER — {WALLET}")
    print("=" * 55)

    data = get_my_wallet_data()
    s = data["summary"]

    print(f"\nWallet      : {s['wallet_short']}")
    print(f"Open        : {s['open_count']} posiciones (valor: ${s['total_open_value']} USDC)")
    print(f"Cerradas    : {s['closed_count']} ({s['won']}W / {s['lost']}L — {s['win_rate']}% WR)")
    print(f"PnL abierto : {s['open_pnl']:+.2f} USDC")
    print(f"PnL cerrado : {s['closed_pnl']:+.2f} USDC")
    print(f"PnL total   : {s['total_pnl']:+.2f} USDC")

    if data["open_positions"]:
        print("\n── Posiciones abiertas ──")
        for p in data["open_positions"][:5]:
            print(f"  {p['outcome']:4s} {p['market'][:50]:50s}  "
                  f"size={p['size']:.1f}  pnl={p['cash_pnl']:+.2f}")

    if data["closed_positions"]:
        print("\n── Últimas posiciones cerradas ──")
        for p in data["closed_positions"][:5]:
            icon = "✅" if p["won"] else "❌"
            print(f"  {icon} {p['outcome']:4s} {p['market'][:50]:50s}  "
                  f"pnl={p['cash_pnl']:+.2f}")

    if s["empty"]:
        print("\n⚠️  La wallet no tiene posiciones todavía.")
        print("   El tracker está listo — se actualizará automáticamente cuando operes.")
