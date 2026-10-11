"""
whale_tracker.py — monitor de posiciones de ballenas copiadas
=============================================================
Cuando el usuario pulsa "Registrar" en una alerta, telegram_bot inserta una
fila en `tracked_positions` con la posición actual de la ballena (initial_size).

Este módulo corre como async loop cada WHALE_CHECK_INTERVAL_SEC y, para cada
posición activa:
  - Consulta /positions?user={wallet} de la ballena.
  - Compara el size actual con initial_size:
      * < initial * WHALE_SELL_THRESHOLD       → status='whale_sold'      + alerta
      * > initial * WHALE_REINFORCE_THRESHOLD  → status='whale_reinforced'+ alerta
  - Si la posición ya no aparece en /positions → status='resolved' (sin alerta).
  - Mantiene un event_log con el histórico (ballena puede reforzar y luego vender).
"""

import asyncio
import requests
from collections import defaultdict

import config
import database

_DATA_API = "https://data-api.polymarket.com"
_TIMEOUT  = 15


def _fetch_whale_positions(wallet: str) -> list:
    """Devuelve las posiciones abiertas de una wallet (ballena)."""
    try:
        r = requests.get(
            f"{_DATA_API}/positions",
            params={"user": wallet, "limit": 500, "sortBy": "CURRENT", "sortDirection": "DESC"},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"[WHALE] Error fetching positions for {wallet[:10]}…: {e}")
        return []


def _match_position(positions: list, condition_id: str, outcome: str) -> dict | None:
    """
    Encuentra la posición de la ballena correspondiente al (condition_id, outcome).
    El matching es por conditionId + outcome (case-insensitive).
    """
    target_oc = (outcome or "").upper().strip()
    for p in positions:
        cid = p.get("conditionId") or ""
        oc  = (p.get("outcome") or "").upper().strip()
        if cid == condition_id and (target_oc == "" or oc == target_oc):
            return p
    return None


async def _send_alert(text: str):
    """Envía un mensaje al chat del usuario sin tumbar el loop si Telegram falla."""
    try:
        from telegram import Bot
        bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
        await bot.send_message(chat_id=config.TELEGRAM_CHAT_ID, text=text)
    except Exception as e:
        print(f"[WHALE] Error enviando alerta Telegram: {e}")


def _wallet_short(addr: str) -> str:
    return (addr[:6] + "…" + addr[-4:]) if addr and len(addr) > 12 else (addr or "?")


async def _process_tracked(row: dict, positions: list):
    """Evalúa una fila tracking y dispara alerta + status si toca."""
    cur = _match_position(positions, row["condition_id"], row.get("outcome") or "")
    initial = float(row.get("initial_size") or 0)

    if initial <= 0:
        # Sin baseline no podemos comparar — marca error y salir
        database.update_tracked_position(
            row["id"], status="error",
            event={"type": "error", "reason": "initial_size <= 0"},
        )
        return

    # Posición ya no aparece → resolved (sin alerta)
    if cur is None:
        database.update_tracked_position(
            row["id"], status="resolved",
            event={"type": "resolved", "reason": "position not found in /positions"},
        )
        return

    cur_size  = float(cur.get("size") or 0)
    cur_price = float(cur.get("curPrice") or cur.get("currentPrice") or 0)

    sell_factor      = config.WHALE_SELL_THRESHOLD       # p.ej. 0.5
    reinforce_factor = config.WHALE_REINFORCE_THRESHOLD  # p.ej. 1.2
    title = cur.get("title") or row["condition_id"][:16]
    outcome = (cur.get("outcome") or row.get("outcome") or "").upper()
    wshort = _wallet_short(row["wallet_address"])

    # Vendiendo
    if cur_size < initial * sell_factor:
        sold_pct = max(0.0, 1.0 - (cur_size / initial)) if initial > 0 else 0.0
        msg = (
            f"🐋 BALLENA VENDIENDO\n"
            f"{wshort} — {title[:60]}\n"
            f"Outcome: {outcome}\n"
            f"Vendido {sold_pct*100:.0f}% ({initial:.0f} → {cur_size:.0f} shares)\n"
            f"Precio: {row.get('initial_price') or '?'} → {cur_price:.3f}"
        )
        await _send_alert(msg)
        database.update_tracked_position(
            row["id"], last_size=cur_size, last_price=cur_price,
            status="whale_sold",
            event={"type": "whale_sold", "size": cur_size, "price": cur_price,
                   "sold_pct": round(sold_pct, 4)},
        )
        return

    # Reforzando
    if cur_size > initial * reinforce_factor:
        added_pct = (cur_size / initial) - 1.0
        msg = (
            f"🐋 BALLENA REFORZANDO\n"
            f"{wshort} — {title[:60]}\n"
            f"Outcome: {outcome}\n"
            f"Aumentó {added_pct*100:.0f}% ({initial:.0f} → {cur_size:.0f} shares)\n"
            f"Precio: {row.get('initial_price') or '?'} → {cur_price:.3f}"
        )
        await _send_alert(msg)
        database.update_tracked_position(
            row["id"], last_size=cur_size, last_price=cur_price,
            status="whale_reinforced",
            event={"type": "whale_reinforced", "size": cur_size, "price": cur_price,
                   "added_pct": round(added_pct, 4)},
        )
        return

    # Sin cambio relevante — solo actualizar last_*
    database.update_tracked_position(
        row["id"], last_size=cur_size, last_price=cur_price,
    )


async def _run_once():
    tracked = database.get_active_tracked_positions()
    if not tracked:
        return 0

    by_wallet: dict = defaultdict(list)
    for row in tracked:
        by_wallet[row["wallet_address"]].append(row)

    processed = 0
    for wallet, rows in by_wallet.items():
        positions = await asyncio.get_event_loop().run_in_executor(
            None, _fetch_whale_positions, wallet,
        )
        for row in rows:
            try:
                await _process_tracked(row, positions)
                processed += 1
            except Exception as e:
                print(f"[WHALE] Error procesando tracked id={row['id']}: {e}")
    return processed


async def whale_loop():
    """Loop principal — corre como tarea async dentro de main.startup()."""
    interval = config.WHALE_CHECK_INTERVAL_SEC
    print(f"[WHALE] Loop iniciado (intervalo={interval}s, "
          f"sell={config.WHALE_SELL_THRESHOLD}, reinforce={config.WHALE_REINFORCE_THRESHOLD})")
    while True:
        try:
            n = await _run_once()
            if n > 0:
                print(f"[WHALE] {n} posición(es) trackeada(s) revisada(s)")
        except Exception as e:
            print(f"[WHALE] Error en whale_loop: {e}")
        await asyncio.sleep(interval)


# ── Test standalone ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    n = asyncio.run(_run_once())
    print(f"Procesadas: {n}")
