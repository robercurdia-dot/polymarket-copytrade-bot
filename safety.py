"""
safety.py — Defensa en profundidad para Real Trading (Fase 4.6, 2026-05-23)
============================================================================

Objetivo: que nunca volvamos a perder dinero por:

  1. Approvals faltantes  → no podemos vender shares (CTF) → orden rechazada.
  2. Fills "delayed"      → CLOB matchea async; código antiguo dejaba el
                             trade open mientras las shares ya estaban vendidas.
  3. Posiciones fantasma  → status=open + 0 shares on-chain.

Componentes:
  - `verify_ctf_approvals()`   : True/False/dict — estado de approvals CTF→exchanges.
  - `ensure_ctf_approvals()`   : repara approvals via relayer Polymarket si faltan.
  - `health_check_open_trades()`: sweep defensivo — fuerza `_close_via_activity_log`
                                   sobre todo trade open, cierra fantasmas.
  - `phantom_open_positions()` : devuelve lista (id, market_id, token_id) sin shares.
  - `tg_alert(text)`           : envío síncrono a Telegram (HTTP raw, sin asyncio).

Pensado para llamarse:
  - Una vez al arrancar (main.py boot).
  - Periódicamente (cada 5 min) vía safety_health_check_loop.
"""
import os, time, json, traceback
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

import requests
from web3 import Web3

import config
import database


# ── Constantes Polymarket Polygon ─────────────────────────────────────
CTF             = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
EXCHANGE_V2     = Web3.to_checksum_address("0xE111180000d2663C0091e4f400237545B87B996B")
NEG_RISK_ADAPTR = Web3.to_checksum_address("0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296")
NEG_RISK_EX_V2  = Web3.to_checksum_address("0xC5d563A36AE78145C45a50134d48A1215220f80a")
# L40c (2026-06-02) — exchange 4 que Polymarket usa para SELL orders en
# algunos tokens (probablemente CTF Exchange V3 / safety net). Detectado
# cuando OCO post fallaba con "allowance: 0 -> spender: 0xe2222..."
EXCHANGE_V3     = Web3.to_checksum_address("0xe2222d279d744050d28e00520010520000310F59")
FACTORY         = Web3.to_checksum_address("0x00000000000Fb5C9ADea0298D729A0CB3823Cc07")
CHAIN_ID        = 137
RELAYER         = "https://relayer-v2.polymarket.com"

REQUIRED_SPENDERS = [
    ("CTF Exchange V2", EXCHANGE_V2),
    ("NegRisk Adapter", NEG_RISK_ADAPTR),
    ("NegRisk Exch V2", NEG_RISK_EX_V2),
    ("CTF Exchange V3", EXCHANGE_V3),
]

RPCS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon-rpc.com",
    "https://rpc.ankr.com/polygon",
]


def _w3():
    for url in RPCS:
        try:
            w = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 6}))
            w.eth.block_number
            return w
        except Exception:
            continue
    return None


# ── Telegram alert (sync, sin asyncio) ────────────────────────────────
def tg_alert(text: str) -> bool:
    """Envía mensaje a Telegram (sync). Silencioso si no configurado."""
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            data={"chat_id": chat, "text": text[:3900],
                  "parse_mode": "HTML", "disable_web_page_preview": "true"},
            timeout=8,
        )
        return r.status_code == 200
    except Exception:
        return False


# ── Approvals CTF (ERC-1155) ──────────────────────────────────────────
def verify_ctf_approvals() -> dict:
    """Devuelve {label: bool} por cada exchange + flag `all_ok`."""
    out = {"all_ok": False, "missing": [], "checked_at": time.time()}
    w3 = _w3()
    if w3 is None:
        out["error"] = "no_rpc"
        return out
    dw = Web3.to_checksum_address(config.DEPOSIT_WALLET_ADDRESS or "0x0")
    abi = [{"name":"isApprovedForAll","type":"function","inputs":[
            {"name":"a","type":"address"},{"name":"o","type":"address"}],
           "outputs":[{"name":"","type":"bool"}],"stateMutability":"view"}]
    ctf = w3.eth.contract(address=CTF, abi=abi)
    for label, spender in REQUIRED_SPENDERS:
        try:
            ok = ctf.functions.isApprovedForAll(dw, spender).call()
        except Exception as e:
            ok = False
            out.setdefault("errors", []).append(f"{label}: {e}")
        out[label] = bool(ok)
        if not ok:
            out["missing"].append(label)
    out["all_ok"] = not out["missing"]
    return out


def ensure_ctf_approvals(notify: bool = True) -> dict:
    """Si faltan approvals, los repara via relayer (sig_type=3 Batch).
    Returns dict {ok, fixed: [labels], tx_hash, msg}.
    No lanza excepciones; falla silenciosa con ok=False."""
    state = verify_ctf_approvals()
    if state.get("all_ok"):
        return {"ok": True, "fixed": [], "msg": "ya estaban OK"}

    missing = state.get("missing", [])
    if not missing:
        return {"ok": False, "msg": "no_rpc_o_error_lectura"}

    try:
        from eth_account import Account
        from eth_account.messages import encode_typed_data
        from eth_abi import encode as abi_encode
        from py_builder_signing_sdk.signing.hmac import build_hmac_signature
    except Exception as e:
        msg = f"deps ausentes: {e}"
        if notify: tg_alert(f"⚠️ <b>SAFETY</b>\nApprovals CTF faltan ({', '.join(missing)}) "
                            f"pero no se pueden reparar auto: {msg}")
        return {"ok": False, "msg": msg}

    try:
        pk  = os.environ["POLY_PRIVATE_KEY"]
        eoa = Account.from_key(pk).address
        dw  = Web3.to_checksum_address(os.environ["DEPOSIT_WALLET_ADDRESS"])
    except KeyError as e:
        return {"ok": False, "msg": f"env_missing_{e}"}

    # Build calls
    sel = Web3.keccak(text="setApprovalForAll(address,bool)")[:4]
    calls = []
    missing_spenders = []
    for label, spender in REQUIRED_SPENDERS:
        if not state.get(label, False):
            data = sel + abi_encode(["address","bool"], [spender, True])
            calls.append({"target": Web3.to_checksum_address(CTF), "value": "0",
                          "data": "0x" + data.hex()})
            missing_spenders.append(label)

    nonce_url = f"{RELAYER}/nonce?address={eoa}&type=WALLET"
    try:
        nonce = int(requests.get(nonce_url, timeout=10).json()["nonce"])
    except Exception as e:
        return {"ok": False, "msg": f"nonce_err: {e}"}
    deadline = int(time.time()) + 600

    typed = {
        "types":{
            "EIP712Domain":[{"name":"name","type":"string"},
                             {"name":"version","type":"string"},
                             {"name":"chainId","type":"uint256"},
                             {"name":"verifyingContract","type":"address"}],
            "Call":[{"name":"target","type":"address"},
                    {"name":"value","type":"uint256"},
                    {"name":"data","type":"bytes"}],
            "Batch":[{"name":"wallet","type":"address"},
                      {"name":"nonce","type":"uint256"},
                      {"name":"deadline","type":"uint256"},
                      {"name":"calls","type":"Call[]"}],
        },
        "primaryType":"Batch",
        "domain":{"name":"DepositWallet","version":"1","chainId":CHAIN_ID,
                  "verifyingContract":dw},
        "message":{"wallet":dw,"nonce":nonce,"deadline":deadline,
                   "calls":[{"target":c["target"],"value":int(c["value"]),
                             "data":bytes.fromhex(c["data"][2:])} for c in calls]},
    }
    signed = Account.sign_message(encode_typed_data(full_message=typed),
                                   private_key=pk)
    sig = "0x" + signed.signature.hex()
    body = {"type":"WALLET","from":eoa,"to":FACTORY,"nonce":str(nonce),
            "signature":sig,
            "depositWalletParams":{"depositWallet":dw,"deadline":str(deadline),
                                   "calls":[{"target":c["target"],"value":c["value"],
                                             "data":c["data"]} for c in calls]}}
    body_str = json.dumps(body, separators=(",",":"))

    def _hmac(method, path, b=None):
        s = os.environ["POLY_BUILDER_SECRET"]
        ts = str(int(time.time()))
        return {"POLY_BUILDER_API_KEY":os.environ["POLY_BUILDER_API_KEY"],
                "POLY_BUILDER_TIMESTAMP":ts,
                "POLY_BUILDER_PASSPHRASE":os.environ["POLY_BUILDER_PASSPHRASE"],
                "POLY_BUILDER_SIGNATURE":build_hmac_signature(s, ts, method, path, b),
                "Content-Type":"application/json"}

    try:
        r = requests.post(f"{RELAYER}/submit", data=body_str,
                          headers=_hmac("POST","/submit",body_str), timeout=20)
        r.raise_for_status()
        tx_id = r.json()["transactionID"]
    except Exception as e:
        if notify:
            tg_alert(f"🛑 <b>SAFETY</b>\nFallo aprobando CTF→{','.join(missing_spenders)}: "
                     f"{str(e)[:300]}\n\n<b>REAL TRADING EN RIESGO</b>")
        return {"ok": False, "msg": f"submit_err: {e}"}

    # Poll
    tx_hash = None
    for _ in range(30):
        try:
            d = requests.get(f"{RELAYER}/transaction?id={tx_id}",
                             headers=_hmac("GET",f"/transaction?id={tx_id}"),
                             timeout=10).json()
            d = d[0] if isinstance(d, list) else d
            s_ = d.get("state")
            if s_ in ("STATE_MINED","STATE_CONFIRMED"):
                tx_hash = d.get("transactionHash")
                break
            if s_ == "STATE_FAILED":
                return {"ok": False, "msg": "tx_failed", "tx_id": tx_id}
        except Exception:
            pass
        time.sleep(3)

    # Verify
    state2 = verify_ctf_approvals()
    ok = state2.get("all_ok", False)
    if notify:
        if ok:
            tg_alert(f"✅ <b>SAFETY</b>\nApprovals CTF reparados: "
                     f"{', '.join(missing_spenders)}\n"
                     f"tx: <code>{tx_hash or '?'}</code>")
        else:
            tg_alert(f"⚠️ <b>SAFETY</b>\nApprovals reparados pero verificación falla.\n"
                     f"missing: {state2.get('missing', [])}")
    return {"ok": ok, "fixed": missing_spenders, "tx_hash": tx_hash,
            "msg": "applied" if ok else "verify_failed"}


# ── Posiciones fantasma (status=open + 0 shares on-chain) ─────────────
def phantom_open_positions() -> list:
    """Devuelve list[dict] de trades open con balance CTF = 0 en DW+Proxy."""
    w3 = _w3()
    if w3 is None:
        return []
    dw    = Web3.to_checksum_address(config.DEPOSIT_WALLET_ADDRESS or "0x0")
    proxy = Web3.to_checksum_address(config.POLY_WALLET_ADDRESS or "0x0")
    abi = [{"name":"balanceOf","type":"function","inputs":[
            {"name":"a","type":"address"},{"name":"i","type":"uint256"}],
           "outputs":[{"name":"","type":"uint256"}],"stateMutability":"view"}]
    ctf = w3.eth.contract(address=CTF, abi=abi)

    conn = database.get_connection()
    c = conn.cursor()
    c.execute("""SELECT id, market_id, token_id, size_filled, entry_price,
                        executed_at, order_id
                 FROM bot_trades
                 WHERE paper_trade=0 AND status='open' AND token_id IS NOT NULL""")
    rows = c.fetchall()
    conn.close()

    # Helpers para clasificar trades sin shares on-chain
    def _order_state(order_id):
        """Devuelve dict {status, size_matched, original_size} o None."""
        if not order_id:
            return None
        try:
            import executor as _e
            o = _e.get_client().get_order(order_id)
            if not isinstance(o, dict):
                return None
            return {"status": (o.get('status') or '').upper(),
                    "matched": float(o.get('size_matched') or 0),
                    "original": float(o.get('original_size') or 0)}
        except Exception:
            return None

    def _has_sell_in_activity(market_id):
        """True si la wallet tiene alguna venta registrada en /activity
        para ese market. Indica phantom REAL (no settlement pending)."""
        try:
            import requests as _rq
            url = (f"https://data-api.polymarket.com/activity?"
                   f"user={config.DEPOSIT_WALLET_ADDRESS.lower()}"
                   f"&market={market_id}&limit=20")
            acts = _rq.get(url, timeout=6).json() or []
            for a in acts:
                if (a.get("type") == "TRADE" and
                    str(a.get("side", "")).upper() == "SELL"):
                    return True
        except Exception:
            pass
        return False

    from datetime import datetime as _dt
    GRACE_MIN = int(os.environ.get("PHANTOM_GRACE_MINUTES", "30"))

    # Fix L16: estados terminales del CLOB → marcar 'rejected' automáticamente.
    REJECT_STATES = {"INVALID", "CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}
    phantoms = []
    rejected_post_hoc = []
    for (bt_id, market, tok, filled, entry, exec_at, oid) in rows:
        try:
            bd = ctf.functions.balanceOf(dw, int(tok)).call() / 1e6
            bp = ctf.functions.balanceOf(proxy, int(tok)).call() / 1e6
        except Exception:
            continue
        # Fix L20: dust-aware. Compara con shares esperadas (cost/entry).
        # Si onchain < 5% de expected → ya vendido aunque queden migajas.
        expected = (float(filled) / float(entry)) if (entry and entry > 0) else 0
        onchain_total = bd + bp
        dust_threshold = max(0.5, 0.05 * expected) if expected > 0 else 0.5
        if onchain_total >= dust_threshold:
            continue  # shares presentes → no phantom

        # Estado de la orden
        state = _order_state(oid)
        clob_st = state["status"] if state else None

        # Caso A (Fix L16): orden rechazada post-hoc por el CLOB +
        # 0 shares → marcar rejected. NO es phantom (no había shares que
        # recuperar) sino una entrada nunca completada que infla equity ficticio.
        if clob_st in REJECT_STATES:
            try:
                database.update_trade_status(
                    bt_id, status='rejected',
                    error=f"safety_reconcile_clob_{clob_st.lower()}")
                rejected_post_hoc.append(bt_id)
                print(f"[SAFETY] bt#{bt_id} marcado rejected "
                      f"(CLOB={clob_st}, 0 shares) — fix L16")
            except Exception as e:
                print(f"[SAFETY] no se pudo marcar bt#{bt_id} rejected: {e}")
            continue

        # Caso B: BUY GTC live esperando match → no phantom
        if clob_st == "LIVE":
            continue

        # Caso C: Grace period — trade joven sin shares puede estar en settlement
        try:
            age_min = (_dt.utcnow() - _dt.fromisoformat(exec_at)).total_seconds() / 60
        except Exception:
            age_min = 9999
        if age_min < GRACE_MIN:
            if not _has_sell_in_activity(market):
                continue  # settlement_pending, no flag

        # Caso D: trade viejo sin shares, sin orden viva → es phantom real
        phantoms.append({"id": bt_id, "market_id": market, "token_id": tok,
                         "size_filled": filled, "entry_price": entry,
                         "executed_at": exec_at,
                         "dw_balance": bd, "proxy_balance": bp,
                         "age_min": round(age_min, 1),
                         "clob_status": clob_st})

    if rejected_post_hoc:
        try:
            tg_alert(f"SAFETY · post-hoc rejected (fix L16)\n"
                     f"IDs marcados como rejected (CLOB INVALID/CANCELED/EXPIRED + 0 shares): "
                     f"{rejected_post_hoc}\n"
                     f"Capital ficticio liberado del equity.")
        except Exception:
            pass

    return phantoms


def orphan_filled_positions() -> list:
    """L32 (2026-05-31) — caso INVERSO al phantom.

    Trade open en DB con size_filled menor que las shares realmente recibidas
    on-chain. Síntoma: el bot postea LIMIT BUY, fillea, pero el reconcile no
    actualiza size_filled (la orden ya no está LIVE cuando el bot intenta
    leerla → size_matched 0 fantasma).

    Devuelve list[dict] de candidatos a reconciliar con shares reales.
    """
    w3 = _w3()
    if w3 is None:
        return []
    dw    = Web3.to_checksum_address(config.DEPOSIT_WALLET_ADDRESS or "0x0")
    proxy = Web3.to_checksum_address(config.POLY_WALLET_ADDRESS or "0x0")
    abi = [{"name":"balanceOf","type":"function","inputs":[
            {"name":"a","type":"address"},{"name":"i","type":"uint256"}],
           "outputs":[{"name":"","type":"uint256"}],"stateMutability":"view"}]
    ctf = w3.eth.contract(address=CTF, abi=abi)

    conn = database.get_connection()
    rows = conn.execute("""SELECT id, market_id, token_id, size_usdc,
                                  size_filled, entry_price
                           FROM bot_trades
                           WHERE paper_trade=0 AND status='open'
                             AND token_id IS NOT NULL
                             AND entry_price > 0""").fetchall()
    conn.close()

    # Agrupar por token_id — varios trades pueden compartir on-chain shares
    by_token: dict = {}
    for r in rows:
        by_token.setdefault(r[2], []).append(r)

    orphans = []
    for tok, trades in by_token.items():
        try:
            bd = ctf.functions.balanceOf(dw, int(tok)).call() / 1e6
            bp = ctf.functions.balanceOf(proxy, int(tok)).call() / 1e6
        except Exception:
            continue
        onchain_shares_total = bd + bp
        # Expected total = suma de shares esperadas de todos los trades open con ese token
        expected_total = sum(
            (float(t[4] or 0) / float(t[5])) for t in trades if t[5]
        )
        diff_total = onchain_shares_total - expected_total
        # No es orphan si la diferencia es <5% Y <1 share
        if diff_total <= max(1.0, 0.05 * expected_total):
            continue
        # Hay shares extra on-chain → asignarlas al trade con mayor déficit
        # individual proporcional. Estrategia simple: solo reportamos UNO
        # (el más reciente que tenga size_filled << expected) para reconciliar.
        candidates = [
            t for t in trades
            if (float(t[4] or 0) / float(t[5])) <
               max(1.0, 0.05 * (float(t[3] or 0) / float(t[5])))
        ]
        # Si hay candidatos con size_filled cerca de 0, son el target
        target = candidates[0] if candidates else max(trades, key=lambda x: x[0])
        (bt_id, mkt, _tok, size_usdc, filled, entry) = target
        # Reasignamos solo la diferencia, no el balance entero
        implied_filled = round((float(filled or 0)) + diff_total * float(entry), 4)
        orphans.append({
            "id": bt_id, "market_id": mkt, "token_id": tok,
            "size_usdc": size_usdc, "size_filled_db": filled,
            "entry_price": entry,
            "expected_shares": round(expected_total, 4),
            "onchain_shares": round(onchain_shares_total, 4),
            "implied_filled": implied_filled,
            "missing_cost_usdc": round(implied_filled - (filled or 0), 4),
        })
    return orphans


def reconcile_orphan_fills(notify: bool = True) -> dict:
    """L32 — actualiza size_filled de orphans detectados a su valor real.
    Solo toca el campo `size_filled` (no transiciona status), preservando
    la inmutabilidad de L21."""
    out = {"checked": 0, "fixed": 0, "skipped": 0, "errors": 0}
    orphans = orphan_filled_positions()
    out["checked"] = len(orphans)
    if not orphans:
        return out

    from datetime import datetime as _dt
    conn = database.get_connection()
    c = conn.cursor()
    fixed_ids = []
    for o in orphans:
        try:
            c.execute("""UPDATE bot_trades
                         SET size_filled = ?,
                             error = COALESCE(error,'')
                                  || ' | orphan_fill_reconciled_'||?||
                                     ': onchain='||?||'sh implied=$'||?
                         WHERE id = ? AND status='open'""",
                      (o["implied_filled"],
                       _dt.utcnow().isoformat(),
                       o["onchain_shares"], o["implied_filled"], o["id"]))
            if c.rowcount:
                out["fixed"] += 1
                fixed_ids.append(o["id"])
            else:
                out["skipped"] += 1
        except Exception:
            out["errors"] += 1
            traceback.print_exc()
    conn.commit()
    conn.close()

    if fixed_ids and notify:
        try:
            recovered = round(sum(o["missing_cost_usdc"]
                                  for o in orphans if o["id"] in fixed_ids), 2)
            tg_alert(f"🩹 <b>SAFETY orphan-fill</b>\n"
                     f"IDs reconciliados: {fixed_ids}\n"
                     f"Valor recuperado en visibilidad: ~${recovered}")
        except Exception:
            pass
    return out


def health_check_open_trades(notify: bool = True) -> dict:
    """Sweep defensivo. Cubre 2 direcciones de divergencia DB↔on-chain:
      1. phantom: DB open + 0 shares on-chain → rescata vía activity log.
      2. orphan_fill (L32): DB size_filled < shares reales on-chain →
         actualiza size_filled para que aparezca en equity.
    Devuelve {checked, phantom, rescued, orphan_fills, orphan_fixed, errors}."""
    out = {"checked": 0, "phantom": 0, "rescued": 0,
           "orphan_fills": 0, "orphan_fixed": 0, "errors": 0}
    phantoms = phantom_open_positions()
    out["checked"] = len(phantoms)
    # ── Caso 1: phantom (shares=0 en chain) ──────────────────────────
    if phantoms:
        try:
            import executor
        except Exception as e:
            out["error"] = f"import_executor: {e}"
            return out
        for p in phantoms:
            out["phantom"] += 1
            try:
                ok = executor._close_via_activity_log(
                    p["id"], p["market_id"], p["token_id"],
                    p["size_filled"], p["entry_price"], 0)
                if ok:
                    out["rescued"] += 1
                    if notify:
                        tg_alert(f"🛟 <b>SAFETY rescue</b>\n"
                                 f"Trade #{p['id']} cerrado desde activity log "
                                 f"(estaba 'open' con 0 shares). "
                                 f"Mira logs para PnL.")
            except Exception:
                out["errors"] += 1
                traceback.print_exc()

    # ── Caso 2: orphan-fill (L32) — DB infra-reporta shares on-chain ─
    try:
        orphan_stats = reconcile_orphan_fills(notify=notify)
        out["orphan_fills"] = orphan_stats.get("checked", 0)
        out["orphan_fixed"] = orphan_stats.get("fixed", 0)
        out["errors"] += orphan_stats.get("errors", 0)
    except Exception:
        out["errors"] += 1
        traceback.print_exc()
    return out


def divergence_monitor(window: int = 20, alert_threshold_pp: float = 25.0,
                        notify: bool = True) -> dict:
    """Compara PnL% paper-real para el agente real sobre las últimas N
    ventas cerradas (pares paper/real con mismo detected_trade_id).

    Si el GAP MEDIO supera `alert_threshold_pp` puntos porcentuales,
    envía alerta Telegram. Devuelve dict con métricas.

    Defensa anti-divergencia: si paper sube mucho más que real, significa
    que paper está usando precios no ejecutables (midpoint sobre orderbook
    iliquido). Esta función lo detecta para que actuemos antes de perder.
    """
    real_agent = (getattr(config, "REAL_TRADING_AGENT", "NONE") or "NONE").upper()
    if real_agent == "NONE":
        return {"active": False, "reason": "no_real_agent"}

    conn = database.get_connection()
    rows = conn.execute("""
        SELECT pt.realized_pnl_net, bt.pnl_usdc, bt.size_filled,
               pt.market_question, pt.id, bt.id, pt.status, bt.status,
               pt.exit_at
        FROM paper_trades pt
        JOIN bot_trades  bt ON pt.detected_trade_id = bt.detected_trade_id
                            AND bt.paper_trade = 0
        WHERE pt.agent_id = ?
          AND pt.status != 'open' AND bt.status = 'closed'
          AND pt.realized_pnl_net IS NOT NULL
          AND bt.pnl_usdc IS NOT NULL
        ORDER BY pt.exit_at DESC LIMIT ?
    """, (real_agent, window)).fetchall()
    conn.close()

    if not rows:
        return {"active": True, "n": 0, "msg": "no_pairs"}

    paper_pcts, real_pcts = [], []
    worst = None
    for (p_pnl, r_pnl, r_size, q, p_id, r_id, p_st, r_st, ex_at) in rows:
        try:
            p_pct = float(p_pnl) / 100.0          # paper size nominal $100
            r_pct = float(r_pnl) / max(0.01, float(r_size or 1.0))
        except Exception:
            continue
        paper_pcts.append(p_pct)
        real_pcts.append(r_pct)
        gap = p_pct - r_pct
        if worst is None or abs(gap) > abs(worst[0]):
            worst = (gap, q, p_id, r_id, p_pnl, r_pnl, r_size, p_st, r_st)

    if not paper_pcts:
        return {"active": True, "n": 0, "msg": "no_valid_pairs"}

    avg_paper = sum(paper_pcts) / len(paper_pcts)
    avg_real  = sum(real_pcts) / len(real_pcts)
    gap_pp    = (avg_paper - avg_real) * 100   # en puntos porcentuales

    out = {
        "active": True,
        "n": len(paper_pcts),
        "avg_paper_pct": round(avg_paper * 100, 2),
        "avg_real_pct":  round(avg_real * 100, 2),
        "gap_pp":        round(gap_pp, 2),
        "threshold_pp":  alert_threshold_pp,
        "worst_gap_pp":  round(worst[0] * 100, 2) if worst else None,
        "worst_market":  (worst[1][:50] if worst else None),
        "alert":         abs(gap_pp) > alert_threshold_pp,
    }
    if out["alert"] and notify:
        direction = "PAPER >> REAL" if gap_pp > 0 else "REAL >> PAPER"
        tg_alert(
            f"⚠️ <b>SAFETY · divergencia</b>\n"
            f"Últimos {len(paper_pcts)} pares paper-real:\n"
            f"  paper avg = {avg_paper*100:+.2f}%\n"
            f"  real  avg = {avg_real*100:+.2f}%\n"
            f"  gap     = <b>{gap_pp:+.2f}pp</b> ({direction})\n"
            f"  threshold = {alert_threshold_pp}pp\n\n"
            f"Peor pair: gap={out['worst_gap_pp']}pp  "
            f"{out['worst_market']}"
        )
    return out


def startup_preflight() -> bool:
    """Llamar UNA VEZ al arrancar el bot.
    1) Verifica/repara approvals CTF.
    2) Sweep inicial de fantasmas.
    Devuelve True si real trading es seguro de operar.
    Si approvals faltan y no se pueden reparar → desactiva real trading
    para evitar pérdidas (set REAL_TRADING_ENABLED=false en .env)."""
    print("[SAFETY] Pre-flight: verificando approvals CTF → exchanges…")
    state = verify_ctf_approvals()
    if state.get("all_ok"):
        print("[SAFETY]   ✅ Approvals OK")
    else:
        print(f"[SAFETY]   ⚠️  Faltan: {state.get('missing')}; intentando reparar…")
        fixed = ensure_ctf_approvals(notify=True)
        if not fixed.get("ok"):
            print(f"[SAFETY]   🛑 No se pudieron reparar: {fixed.get('msg')}")
            # Auto-desactivar para evitar abrir trades que no podemos cerrar
            try:
                from dotenv import set_key
                set_key(str(Path(__file__).resolve().parent / ".env"),
                        "REAL_TRADING_ENABLED", "false")
                config.REAL_TRADING_ENABLED = False
                tg_alert("🛑 <b>SAFETY</b>\nApprovals CTF rotos y no reparables.\n"
                         "<b>REAL_TRADING_ENABLED=false</b> (auto-desactivado). "
                         "Revisa logs.")
            except Exception:
                pass
            return False
        print(f"[SAFETY]   ✅ Approvals reparados: {fixed.get('fixed')}")

    print("[SAFETY] Sweep inicial de posiciones fantasma…")
    sweep = health_check_open_trades(notify=True)
    if sweep["phantom"] > 0:
        print(f"[SAFETY]   detectadas {sweep['phantom']} fantasmas, "
              f"rescatadas {sweep['rescued']}")
    else:
        print("[SAFETY]   ✅ Sin posiciones fantasma")
    return True


if __name__ == "__main__":
    # Manual: python safety.py
    import sys
    if "--repair" in sys.argv:
        r = ensure_ctf_approvals(notify=False)
        print(json.dumps(r, indent=2))
    elif "--sweep" in sys.argv:
        r = health_check_open_trades(notify=False)
        print(json.dumps(r, indent=2))
    else:
        print(json.dumps(verify_ctf_approvals(), indent=2))
        print()
        print("Phantoms:", phantom_open_positions())
