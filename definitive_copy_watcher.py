"""
definitive_copy_watcher.py — L79 (2026-06-06)
=============================================
Background loop que cierra los DEF_COPY paper trades cuando el trader hace
una operación de salida.

Cada 10s:
  1. Fetch últimas activities de cada trader (SELLs + MERGEs)
  2. Para cada DEF_COPY open:
     - Si hay SELL del trader en el MISMO token → cierra al precio de SELL
     - Si hay MERGE del trader en el MISMO market_id → cierra a 0.50
       (Swisstony mergea cuando tiene cobertura YES+NO → libera capital real)
       Datos: merges generan +$67 netos (34.4% WR pero avg +$1.11/trade).
  3. Si nada → mantener open hasta resolución del mercado

Fixes L79:
  - BUG1 CRÍTICO: ahora cierra el bot_trade real via close_position() (CLOB SELL),
    no solo en DB. Sin esto las posiciones reales quedaban abiertas on-chain.
  - BUG2 CRÍTICO: usa _persist_trade_close() para el cierre DB del bot_trade,
    unificando status/outcome y previniendo race conditions con reconciler.
  - BUG4 ALTO: añade AND paper_trade=0 al buscar bot_trades reales a cerrar.
  - BUG5 ALTO: PnL del bot_trade se calcula con sus propios datos (entry_price,
    size_filled), no con los del paper_trade.
"""

import os as _os
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from datetime import datetime, timezone, timedelta
import database

DEF_WALLETS = {
    '0x204f72f35326db932158cba6adff0b9a1da95e14': 'Swisstony',
    '0x84dbb7103982e3617704a2ed7d5b39691952aeeb': 'Soarin22',
    '0xc96e5287ab294ac0388c2ddb00180fc464cff1f9': 'wigglew',
    '0xa6d9b55a6a3a54a9d50ca94c64f00af69c50fd2f': 'kinderSman',
}


def _now_iso():
    return datetime.utcnow().isoformat()


def _fetch_exits(addr: str, cutoff_ts: int) -> dict:
    """Devuelve {'sells': [...], 'merges': [...]} en la ventana de cutoff.
    Nunca lanza excepción — devuelve vacío ante cualquier fallo de red o parseo.
    Retry automático x3 con backoff 1s para recuperar SSL/DNS transitorios.
    """
    import time as _time
    empty = {'sells': [], 'merges': []}
    last_err = None
    for _attempt in range(3):
        try:
            resp = requests.get('https://data-api.polymarket.com/activity',
                params={'user': addr, 'limit': 300}, timeout=12, verify=False)
            if resp.status_code != 200:
                print(f"[COPY_WATCHER] activity API {resp.status_code} para {addr[:10]}")
                return empty
            data = resp.json()
            if not isinstance(data, list):
                return empty
            sells, merges = [], []
            for a in data:
                if not isinstance(a, dict):
                    continue
                try:
                    ts = int(a.get('timestamp', 0))
                except (TypeError, ValueError):
                    continue
                if ts < cutoff_ts:
                    continue
                atype = str(a.get('type', '')).upper()
                if atype == 'TRADE' and str(a.get('side', '')).upper() == 'SELL':
                    sells.append(a)
                elif atype == 'MERGE':
                    merges.append(a)
            return {'sells': sells, 'merges': merges}
        except Exception as e:
            last_err = e
            if _attempt < 2:
                _time.sleep(1)
    print(f"[COPY_WATCHER] _fetch_exits error {addr[:10]}: {last_err}")
    return empty


def _close_real_bot_trades(market_id: str, exit_price: float, exit_reason: str,
                           swis_event_usdc: float = 0.0):
    """
    L79 — Cierra los bot_trades reales de DEF_COPY para un market_id dado.
    swis_event_usdc: USDC que Swis vendió/mergeó. Si > 0, cierre proporcional
    (igual que paper): swis_event_usdc × SCALE = nuestro_cierre_usdc.
    Si 0, cierre total (comportamiento previo).
    """
    try:
        import executor as _exec
        SCALE = float(_os.getenv('DEFINITIVE_SWIS_SCALE', '0.00784'))
        conn = database.get_connection()
        real_trades = conn.execute(
            """SELECT id, entry_price, size_filled, size_usdc FROM bot_trades
               WHERE market_id=? AND agent_id='DEF_COPY'
                 AND paper_trade=0 AND status='open'""",
            (market_id,)).fetchall()
        conn.close()

        for bt_id, bt_entry, bt_filled, bt_size in real_trades:
            our_total_usdc = float(bt_filled or bt_size or 0)
            size_override = None
            if swis_event_usdc > 0 and our_total_usdc > 0:
                our_exit_usdc = swis_event_usdc * SCALE
                fraction = our_exit_usdc / our_total_usdc
                if fraction < 0.95:
                    size_override = round(our_exit_usdc, 4)
                    print(f"[COPY_EXIT_REAL] bt#{bt_id} cierre parcial {fraction:.0%} "
                          f"(swis ${swis_event_usdc:.0f} × {SCALE} = ${our_exit_usdc:.2f})")
            result = _exec.close_position(bt_id, size_usdc_override=size_override)
            status_r = (result or {}).get('status', 'error')
            if status_r in ('ok', 'paper_ok'):
                # Sincronizar exit_price del paper con el fill real del CLOB.
                # close_position ejecuta al best_bid real → ese precio es más
                # fiable que el bid instantáneo que usó el copy_watcher para
                # el paper. Actualizamos el paper para que PnL real ≈ PnL paper.
                actual_fill = (result or {}).get('close_price')
                if actual_fill and float(actual_fill) > 0:
                    try:
                        _pc = database.get_connection()
                        _pc.execute(
                            """UPDATE paper_trades
                               SET exit_price=?,
                                   realized_pnl_gross = entry_shares * (? - entry_price),
                                   realized_pnl_net   = entry_shares * (? - entry_price) - COALESCE(fees_estimated,0)
                               WHERE market_id=? AND agent_id='DEF_COPY'
                                 AND status IN ('tp_hit','sl_hit')
                                 AND ABS(exit_price - ?) > 0.01""",
                            (actual_fill, actual_fill, actual_fill, market_id, actual_fill))
                        _pc.commit()
                        _pc.close()
                    except Exception:
                        pass
                print(f"[COPY_EXIT_REAL] bt#{bt_id} cerrado en CLOB @ {actual_fill or exit_price:.4f} · {exit_reason}")
            elif status_r == 'close_failed':
                # close_position no encontró bids → el mercado está en resolución.
                # El trader ya salió (SELL/MERGE detectado). Forzar cierre en DB
                # al exit_price del paper (determinado por CLOB bid o fallback 0.50)
                # en lugar de dejar el real como 'open' indefinidamente.
                sz = float(bt_filled or bt_size or 0)
                ep = float(bt_entry or 0)
                real_gross = round(sz * (exit_price - ep), 4) if ep > 0 else 0.0
                reason_r = (result or {}).get('reason', '')
                if reason_r == 'no_bids':
                    _exec._persist_trade_close(
                        bt_id,
                        outcome='won' if real_gross >= 0 else 'lost',
                        pnl_usdc=real_gross,
                        source='copy_watcher_no_bids',
                        extra_note=f'{exit_reason}@{exit_price:.4f}_no_bids',
                    )
                    print(f"[COPY_EXIT_REAL] bt#{bt_id} cerrado en DB (no_bids) @ {exit_price:.4f} · pnl ${real_gross:+.2f}")
                else:
                    # Otro fallo (spread, timeout) — reconciler reintentará
                    print(f"[COPY_EXIT_REAL] bt#{bt_id} close_failed ({reason_r}) · reconciler reintentará")
            else:
                # Error inesperado — usar _persist_trade_close como fallback DB
                sz = float(bt_filled or bt_size or 0)
                ep = float(bt_entry or 0)
                real_gross = round(sz * (exit_price - ep), 4) if ep > 0 else 0.0
                _exec._persist_trade_close(
                    bt_id,
                    outcome='won' if real_gross >= 0 else 'lost',
                    pnl_usdc=real_gross,
                    source='copy_watcher',
                    extra_note=f'{exit_reason}@{exit_price:.4f}',
                )
    except Exception as e:
        print(f"[COPY_EXIT_REAL] error cerrando bot_trades: {e}")


def check_definitive_copy_exits() -> dict:
    """L79 — cierra paper_trades y bot_trades reales cuando el trader vende/mergea.
    L86 (2026-06-06) — Paper es la verdad: NO se cancela paper por fallos del real.
    El watcher solo cierra cuando el trader hace SELL/MERGE (señal real del trader)."""
    out = {"checked": 0, "closed_sell": 0, "closed_merge": 0, "errors": 0}

    # Limpieza preventiva: cancelar trades de wallets ajenas que el bot viejo
    # pueda haber abierto en DEF_COPY (Lily) o DEF_COPY_05 (wallets no Lily/Swis)
    ALLOWED = {
        'DEF_COPY':    {'0x204f72f35326db932158cba6adff0b9a1da95e14'},  # solo Swisstony
        'DEF_COPY_05': {'0x84dbb7103982e3617704a2ed7d5b39691952aeeb'},  # solo Soarin22
        'DEF_WIGGLEW': {'0xc96e5287ab294ac0388c2ddb00180fc464cff1f9'},  # solo wigglew
        'DEF_KINDER':  {'0xa6d9b55a6a3a54a9d50ca94c64f00af69c50fd2f'},  # solo kinderSman
    }
    conn = database.get_connection()
    for ag, allowed_wallets in ALLOWED.items():
        placeholders = ','.join('?' * len(allowed_wallets))
        conn.execute(f"""
            UPDATE paper_trades SET status='cancelled'
            WHERE agent_id=? AND status='open'
              AND LOWER(wallet_copied) NOT IN ({placeholders})
        """, [ag] + list(allowed_wallets))
    conn.commit()
    conn.close()

    conn = database.get_connection()
    opens = conn.execute("""
        SELECT id, token_id, market_id, entry_price, entry_shares,
               fees_estimated, entry_size_usdc, wallet_copied
        FROM paper_trades
        WHERE agent_id IN ('DEF_COPY','DEF_COPY_05','DEF_WIGGLEW','DEF_KINDER','B') AND status='open'
    """).fetchall()

    # Hold-to-resolve: cerrar papers cuyo mercado ya resolvió sin SELL/MERGE.
    # Swisstony hold-to-resolve → no hay evento en activity API → copy_watcher
    # nunca dispara. Detectamos resolución por curPrice extremo:
    #   < 0.02 → outcome perdido (resuelto a NO)
    #   > 0.98 → outcome ganado (resuelto a YES)
    resolved_count = 0
    for pt_id, tok, mid, ep, shares, fees, sz, wc in opens:
        lcp = conn.execute(
            "SELECT last_check_price FROM paper_trades WHERE id=?", (pt_id,)
        ).fetchone()
        if not lcp or lcp[0] is None:
            continue
        cur = float(lcp[0])
        if cur < 0.02:
            exit_p, outcome = 0.0, 'sl_hit'
        elif cur > 0.98:
            exit_p, outcome = 1.0, 'tp_hit'
        else:
            continue
        gross = round(float(shares or 0) * (exit_p - float(ep or 0)), 4)
        net   = round(gross - float(fees or 0), 4)
        conn.execute("""UPDATE paper_trades
            SET status=?, exit_at=?, exit_price=?, exit_reason=?,
                realized_pnl_gross=?, realized_pnl_net=?
            WHERE id=? AND status='open'""",
            (outcome, _now_iso(), exit_p, 'market_resolved',
             gross, net, pt_id))
        resolved_count += 1
        print(f"[COPY_WATCHER] paper#{pt_id} cerrado por resolución de mercado "
              f"@ {exit_p:.2f} · pnl ${net:+.4f}")
    if resolved_count:
        conn.commit()

    # Hold-to-resolve para bot_trades REALES: cerrar los que el mercado ya resolvió.
    real_opens = conn.execute("""
        SELECT id, token_id, market_id, entry_price, size_filled
        FROM bot_trades
        WHERE agent_id='DEF_COPY' AND paper_trade=0 AND status='open'
    """).fetchall()
    real_resolved = 0
    _mkt_cache = {}  # market_id → mdata dict (deduplica HTTP calls por mercado)
    import requests as _req
    for bt_id, tok, mid, ep, filled in real_opens:
        try:
            if mid not in _mkt_cache:
                r = _req.get(f"https://clob.polymarket.com/markets/{mid}", timeout=6)
                _mkt_cache[mid] = r.json() if r.status_code == 200 else None
            mdata = _mkt_cache[mid]
            if not mdata or not mdata.get("closed"):
                continue
            tokens = mdata.get("tokens", [])
            winner = next((t for t in tokens if t.get("winner")), None)
            is_won = winner and str(winner.get("token_id", "")) == str(tok)
            exit_p = 1.0 if is_won else 0.0
            outcome = 'won' if is_won else 'lost'
            pnl = round(float(filled or 0) * (exit_p - float(ep or 0)), 4)
            from datetime import datetime as _dt_cw
            conn.execute("""
                UPDATE bot_trades
                SET status='closed', outcome=?, pnl_usdc=?,
                    closed_at=COALESCE(closed_at, ?)
                WHERE id=? AND paper_trade=0 AND status='open'
            """, (outcome, pnl, _dt_cw.utcnow().isoformat(), bt_id))
            real_resolved += 1
            print(f"[COPY_WATCHER_REAL] bt#{bt_id} {outcome} market_resolved pnl={pnl:+.4f}")
        except Exception as e:
            print(f"[COPY_WATCHER_REAL] bt#{bt_id} error: {e}")
    if real_resolved:
        conn.commit()
        print(f"[COPY_WATCHER_REAL] {real_resolved} bot_trades cerrados por resolución")

    # Recargar la lista de opens excluyendo los recién cerrados
    opens = conn.execute("""
        SELECT id, token_id, market_id, entry_price, entry_shares,
               fees_estimated, entry_size_usdc, wallet_copied
        FROM paper_trades
        WHERE agent_id IN ('DEF_COPY','DEF_COPY_05','DEF_WIGGLEW','DEF_KINDER','B') AND status='open'
    """).fetchall()
    conn.close()

    if not opens:
        return out
    out["checked"] = len(opens)

    # L81 FIX: ventana ampliada de 4h → 24h.
    # Si el bot cae >4h y Swisstony vende mientras está caído, con 4h
    # el SELL queda fuera de la ventana → paper abierto indefinidamente
    # y bot_trade real sin cerrar en CLOB. 24h cubre reinicios normales.
    cutoff = int((datetime.now(timezone.utc) - timedelta(hours=24)).timestamp())

    needed_wallets = {(row[7] or '').lower() for row in opens if row[7]}
    exits_by_wallet = {}
    for addr in needed_wallets:
        if not addr:
            continue
        exits_by_wallet[addr] = _fetch_exits(addr, cutoff)

    for pt_id, token_id, market_id, entry_price, shares, fees, sz, wc in opens:
        wc_l = (wc or '').lower()
        if wc_l not in exits_by_wallet:
            continue
        data = exits_by_wallet[wc_l]

        # Solo salimos cuando Swis hace SELL en el token concreto.
        # MERGE ignorado: Swis mergea para recuperar capital (YES+NO→$1) pero
        # mantiene exposición. Replicar su merge no tiene sentido a nuestra escala
        # y la API no devuelve usdcSize en merges. Dejamos resolver al mercado.
        swis_event_usdc = 0.0
        sells = [s for s in data['sells'] if s.get('asset') == token_id]
        if not sells:
            continue
        s = sorted(sells, key=lambda x: x.get('timestamp', 0))[-1]
        exit_price = float(s.get('price', entry_price) or entry_price)
        exit_reason = 'trader_sold'
        swis_event_usdc = float(s.get('usdcSize', 0) or 0)

        # Salida proporcional a escala (igual que la entrada):
        # swis vende X USDC → nosotros salimos de X × SCALE USDC de nuestra posición.
        # Si swis_event_usdc=0 (campo ausente), cerramos todo por seguridad.
        SCALE = float(_os.getenv('DEFINITIVE_SWIS_SCALE', '0.00784'))
        entry_shares_f = float(shares or 0)
        entry_price_f  = float(entry_price or 0)
        if swis_event_usdc > 0 and exit_price > 0:
            our_exit_usdc  = swis_event_usdc * SCALE
            close_shares   = min(entry_shares_f, our_exit_usdc / exit_price)
            partial        = close_shares < entry_shares_f * 0.95
        else:
            close_shares   = entry_shares_f
            partial        = False

        fees_f = float(fees or 0)
        close_fees = round(fees_f * (close_shares / entry_shares_f) if entry_shares_f > 0 else fees_f, 4)
        gross = round(close_shares * (exit_price - entry_price_f), 4)
        net   = round(gross - close_fees, 4)
        paper_status = "tp_hit" if net >= 0 else "sl_hit"

        try:
            conn = database.get_connection()
            if partial:
                # Salida parcial: reducir shares, acumular PnL realizado parcial
                remaining_shares = round(entry_shares_f - close_shares, 6)
                remaining_usdc   = round(remaining_shares * entry_price_f, 4)
                remaining_fees   = round(fees_f - close_fees, 4)
                prev_gross = conn.execute(
                    "SELECT COALESCE(realized_pnl_gross,0) FROM paper_trades WHERE id=?", (pt_id,)
                ).fetchone()[0] or 0
                prev_net = conn.execute(
                    "SELECT COALESCE(realized_pnl_net,0) FROM paper_trades WHERE id=?", (pt_id,)
                ).fetchone()[0] or 0
                conn.execute("""UPDATE paper_trades
                    SET entry_shares=?, entry_size_usdc=?, fees_estimated=?,
                        realized_pnl_gross=?, realized_pnl_net=?,
                        last_check_at=?, last_check_price=?
                    WHERE id=? AND status='open'""",
                    (remaining_shares, remaining_usdc, remaining_fees,
                     round(float(prev_gross) + gross, 4),
                     round(float(prev_net) + net, 4),
                     _now_iso(), exit_price, pt_id))
                conn.commit()
                conn.close()
                trader = DEF_WALLETS.get(wc_l, wc_l[:10])
                print(f"[COPY_EXIT] #{pt_id} parcial · {trader} {exit_reason} @ {exit_price:.4f} "
                      f"· {close_shares:.4f}sh / {remaining_shares:.4f}sh restantes · pnl parcial ${net:+.4f}")
            else:
                # Salida total
                conn.execute("""UPDATE paper_trades
                    SET status=?, exit_at=?, exit_price=?, exit_reason=?,
                        realized_pnl_gross=?, realized_pnl_net=?,
                        last_check_at=?, last_check_price=?
                    WHERE id=? AND status='open'""",
                    (paper_status, _now_iso(), exit_price, exit_reason, gross, net,
                     _now_iso(), exit_price, pt_id))
                conn.commit()
                conn.close()

                if exit_reason == 'trader_sold':
                    out["closed_sell"] += 1
                else:
                    out["closed_merge"] += 1
                trader = DEF_WALLETS.get(wc_l, wc_l[:10])
                print(f"[COPY_EXIT] #{pt_id} cerrado · {trader} {exit_reason} @ {exit_price:.4f} · pnl ${net:+.4f}")

            # Real: cerrar bot_trade via CLOB siempre que Swis salga (parcial o total).
            # REAL_COPY_ENABLED=false bloquea nuevas entradas pero no los cierres.
            SWIS = '0x204f72f35326db932158cba6adff0b9a1da95e14'
            if wc_l == SWIS:
                _close_real_bot_trades(market_id, exit_price, exit_reason,
                                       swis_event_usdc=swis_event_usdc)
        except Exception as e:
            out["errors"] += 1
            print(f"[DEF_COPY] err cerrando #{pt_id}: {e}")

    return out


if __name__ == "__main__":
    import time
    consecutive_errors = 0
    MAX_CONSECUTIVE_ERRORS = 20  # ~3 min de fallos antes de alertar
    while True:
        try:
            res = check_definitive_copy_exits()
            consecutive_errors = 0  # reset en éxito
            tot = res["closed_sell"] + res["closed_merge"]
            if tot > 0:
                print(f"[DEF_COPY watcher] closed sell={res['closed_sell']} merge={res['closed_merge']}")
        except Exception as e:
            consecutive_errors += 1
            print(f"[DEF_COPY watcher] err #{consecutive_errors}: {e}")
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                print(f"[DEF_COPY watcher] ⚠️  {consecutive_errors} errores consecutivos — "
                      f"las posiciones reales siguen abiertas hasta que se resuelvan por el mercado")
                consecutive_errors = 0  # reset para no spamear
        time.sleep(10)
