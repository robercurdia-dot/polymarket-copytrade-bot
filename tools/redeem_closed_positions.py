"""
redeem_closed_positions.py — Redime shares CTF de mercados resueltos
desde la deposit wallet vía relayer WALLET batch.

Convierte tokens-shares en pUSD spendable. Misma firma EIP-712 Batch
que approve_deposit_wallet.py.

USO:
  python tools/redeem_closed_positions.py            # dry-run
  python tools/redeem_closed_positions.py --execute  # firma y envía
  python tools/redeem_closed_positions.py --execute --include-losers
                                                    # también quema shares perdedoras

Identifica trades con status='closed' y shares CTF > 0 en deposit wallet.
Agrupa por (conditionId) y construye una sola tx batch con un call
redeemPositions por mercado. Tras éxito, los shares ganadores devuelven
pUSD proporcional; los perdedores se queman (recuperación 0 pero limpia).
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import requests
from eth_account import Account
from eth_account.messages import encode_typed_data
from web3 import Web3
from py_builder_signing_sdk.signing.hmac import build_hmac_signature

import config

# ── Contratos Polygon ────────────────────────────────────────────────────────
CTF        = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
NEGRISK_AD = Web3.to_checksum_address("0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296")
PUSD       = Web3.to_checksum_address("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")
FACTORY    = Web3.to_checksum_address("0x00000000000Fb5C9ADea0298D729A0CB3823Cc07")
CHAIN_ID   = 137
RELAYER    = "https://relayer-v2.polymarket.com"
RPC        = "https://polygon-bor-rpc.publicnode.com"

# ABIs mínimas
CTF_ABI = [
    {"name": "redeemPositions", "type": "function", "inputs": [
        {"name": "collateralToken",  "type": "address"},
        {"name": "parentCollectionId","type": "bytes32"},
        {"name": "conditionId",       "type": "bytes32"},
        {"name": "indexSets",         "type": "uint256[]"},
    ], "outputs": [], "stateMutability": "nonpayable"},
    {"name": "balanceOf", "type": "function", "inputs": [
        {"name": "owner", "type": "address"},
        {"name": "id",    "type": "uint256"},
    ], "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view"},
]
NEGRISK_ABI = [
    {"name": "redeemPositions", "type": "function", "inputs": [
        {"name": "conditionId", "type": "bytes32"},
        {"name": "amounts",     "type": "uint256[]"},
    ], "outputs": [], "stateMutability": "nonpayable"},
]
ERC20_ABI = [
    {"name": "balanceOf", "type": "function", "inputs": [
        {"name":"owner","type":"address"}],
     "outputs": [{"name":"","type":"uint256"}], "stateMutability":"view"},
]


def _hmac_headers(method, path, body_str=None):
    secret = os.environ["POLY_BUILDER_SECRET"]
    ts = str(int(time.time()))
    sig = build_hmac_signature(secret, ts, method, path, body_str)
    return {
        "POLY_BUILDER_API_KEY":   os.environ["POLY_BUILDER_API_KEY"],
        "POLY_BUILDER_TIMESTAMP": ts,
        "POLY_BUILDER_PASSPHRASE": os.environ["POLY_BUILDER_PASSPHRASE"],
        "POLY_BUILDER_SIGNATURE": sig,
        "Content-Type":           "application/json",
    }


def get_wallet_nonce(eoa: str) -> int:
    r = requests.get(f"{RELAYER}/nonce?address={eoa}&type=WALLET", timeout=10)
    r.raise_for_status()
    return int(r.json()["nonce"])


def build_batch_signature(deposit_wallet: str, nonce: int, deadline: int,
                          calls: list, private_key: str) -> str:
    typed = {
        "types": {
            "EIP712Domain": [
                {"name": "name",              "type": "string"},
                {"name": "version",           "type": "string"},
                {"name": "chainId",           "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Call": [
                {"name": "target", "type": "address"},
                {"name": "value",  "type": "uint256"},
                {"name": "data",   "type": "bytes"},
            ],
            "Batch": [
                {"name": "wallet",   "type": "address"},
                {"name": "nonce",    "type": "uint256"},
                {"name": "deadline", "type": "uint256"},
                {"name": "calls",    "type": "Call[]"},
            ],
        },
        "primaryType": "Batch",
        "domain": {
            "name": "DepositWallet", "version": "1",
            "chainId": CHAIN_ID, "verifyingContract": deposit_wallet,
        },
        "message": {
            "wallet": deposit_wallet, "nonce": nonce, "deadline": deadline,
            "calls": [
                {"target": c["target"], "value": int(c["value"]),
                 "data": bytes.fromhex(c["data"][2:])}
                for c in calls
            ],
        },
    }
    signed = Account.sign_message(encode_typed_data(full_message=typed),
                                  private_key=private_key)
    return "0x" + signed.signature.hex()


def collect_redeem_candidates(include_losers=False):
    """Lee bot_trades.status='closed' con shares on-chain > 0.
    Agrupa por market_id; consulta TODOS los tokens del mercado vía CLOB API
    para construir amounts[] correctamente (en orden de outcome)."""
    w3 = Web3(Web3.HTTPProvider(RPC))
    ctf = w3.eth.contract(address=CTF, abi=CTF_ABI)
    dw = Web3.to_checksum_address(config.DEPOSIT_WALLET_ADDRESS)

    conn = sqlite3.connect("polymarket_insider.db")
    rows = conn.execute("""
        SELECT id, market_id, token_id, outcome
        FROM bot_trades
        WHERE paper_trade=0 AND status='closed' AND token_id IS NOT NULL
        ORDER BY id
    """).fetchall()

    # Agrupar por mercado
    markets = defaultdict(list)
    for tid, mid, tok, oc in rows:
        if not include_losers and oc != "won":
            continue
        markets[mid].append((tid, tok, oc))

    groups = {}
    for mid, trades in markets.items():
        # Consultar tokens del market en orden de outcome
        try:
            r = requests.get(f"https://clob.polymarket.com/markets/{mid}", timeout=10)
            mdata = r.json()
            outcome_tokens = [int(t["token_id"]) for t in mdata.get("tokens", [])]
        except Exception as e:
            print(f"  ⚠ market {mid[:14]}…: no se pudo obtener tokens ({e}) — skip")
            continue
        if not outcome_tokens:
            continue
        # Balance per outcome (en unidades base, 6 decimales)
        amounts = []
        any_balance = False
        for tok in outcome_tokens:
            b = ctf.functions.balanceOf(dw, tok).call()
            amounts.append(b)
            if b > 0:
                any_balance = True
        if not any_balance:
            continue
        # Si hay balance en AMBOS lados, solo redimir el lado ganador (token del trade won)
        nonzero = sum(1 for a in amounts if a > 0)
        if nonzero > 1:
            # Identificar el token ganador usando el token_id del trade won en DB
            winner_tokens = {int(tok) for (_, tok, oc) in trades if oc == "won" and tok}
            clean_amounts = []
            for i, (tok, amt) in enumerate(zip(outcome_tokens, amounts)):
                if tok in winner_tokens:
                    clean_amounts.append(amt)
                else:
                    clean_amounts.append(0)
            if all(a == 0 for a in clean_amounts):
                print(f"  ⚠ market {mid[:14]}…: no se pudo identificar token ganador — skip")
                continue
            print(f"  ℹ market {mid[:14]}…: ambos lados con balance, redimiendo solo ganador: {[f'{a/1e6:.2f}' for a in clean_amounts]}")
            amounts = clean_amounts
        groups[mid] = {
            "market_id": mid,
            "trades": trades,
            "outcome_tokens": outcome_tokens,
            "amounts": amounts,
        }
    conn.close()
    return groups, dw, w3


def build_redeem_calls(groups, include_approval=True):
    """Devuelve list[{target,value,data}].
    Si include_approval=True, antecede setApprovalForAll(NegRiskAdapter, true)
    sobre el CTF para que el adapter pueda burn las shares."""
    from eth_abi import encode as abi_encode
    calls = []

    if include_approval:
        # CTF.setApprovalForAll(NegRiskAdapter, true)
        sel_app = Web3.keccak(text="setApprovalForAll(address,bool)")[:4]
        data_app = sel_app + abi_encode(["address","bool"], [NEGRISK_AD, True])
        calls.append({"target": CTF, "value": "0",
                      "data": "0x" + data_app.hex(),
                      "_label": "CTF.setApprovalForAll(NegRiskAdapter, true)"})

    # NegRiskAdapter.redeemPositions(bytes32 conditionId, uint256[] amounts)
    sel_red = Web3.keccak(text="redeemPositions(bytes32,uint256[])")[:4]
    for mid, g in groups.items():
        cid = bytes.fromhex(mid[2:])
        data = sel_red + abi_encode(["bytes32","uint256[]"], [cid, g["amounts"]])
        amts_str = ",".join(f"{a/1e6:.2f}" for a in g["amounts"])
        calls.append({"target": NEGRISK_AD, "value": "0",
                      "data": "0x" + data.hex(),
                      "_label": f"NegRisk.redeem {mid[:14]}… amounts=[{amts_str}]"})
    return calls


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--include-losers", action="store_true",
                    help="También redime shares perdedoras (limpieza, recuperación=0)")
    args = ap.parse_args()

    pk  = os.environ["POLY_PRIVATE_KEY"]
    eoa = Account.from_key(pk).address
    dw  = config.DEPOSIT_WALLET_ADDRESS

    print(f"EOA owner       : {eoa}")
    print(f"Deposit wallet  : {dw}")
    print()

    groups, dw_addr, w3 = collect_redeem_candidates(
        include_losers=args.include_losers
    )
    if not groups:
        print("No hay candidatos para redimir.")
        return

    pusd = w3.eth.contract(address=PUSD, abi=ERC20_ABI)
    bal_before = pusd.functions.balanceOf(dw_addr).call() / 1e6
    print(f"pUSD antes      : ${bal_before:.4f}")
    print()

    print("Candidatos (vía NegRiskAdapter):")
    for mid, g in groups.items():
        trades_str = ", ".join(f"#{t[0]}({t[2]})" for t in g["trades"])
        amts_str = "/".join(f"{a/1e6:.2f}" for a in g["amounts"])
        print(f"  market {mid[:14]}… amounts=[{amts_str}]  trades=[{trades_str}]")

    calls = build_redeem_calls(groups)
    if not calls:
        print("\nSin calls que ejecutar (neg-risk no soportado o vacío).")
        return

    print(f"\n{len(calls)} call(s) a incluir en batch:")
    for c in calls:
        print(f"  - {c['_label']}")
        c.pop("_label", None)

    if not args.execute:
        print("\n[DRY-RUN] Sin --execute no se firma. Repite con --execute para enviar.")
        return

    nonce    = get_wallet_nonce(eoa)
    deadline = int(time.time()) + 600
    print(f"\nWALLET nonce: {nonce} · deadline +10min")

    signature = build_batch_signature(dw, nonce, deadline, calls, pk)
    print(f"Batch signature: {signature[:18]}… (len={len(signature)})")

    body = {
        "type": "WALLET", "from": eoa, "to": FACTORY,
        "nonce": str(nonce), "signature": signature,
        "depositWalletParams": {
            "depositWallet": dw, "deadline": str(deadline),
            "calls": [{"target": c["target"], "value": c["value"], "data": c["data"]}
                      for c in calls],
        },
    }
    body_str = json.dumps(body, separators=(",", ":"))
    headers = _hmac_headers("POST", "/submit", body_str)

    print("\nEnviando POST /submit...")
    r = requests.post(f"{RELAYER}/submit", data=body_str, headers=headers, timeout=20)
    print(f"status: {r.status_code}  body: {r.text[:400]}")
    r.raise_for_status()
    tx_id = r.json()["transactionID"]
    print(f"\nTransactionID: {tx_id} · polling...")

    path = f"/transaction?id={tx_id}"
    for i in range(40):
        h = _hmac_headers("GET", path)
        rr = requests.get(f"{RELAYER}{path}", headers=h, timeout=10).json()
        data = rr[0] if isinstance(rr, list) and rr else rr
        state = data.get("state")
        print(f"  [{i*5:>3}s] state={state}")
        if state in ("STATE_MINED", "STATE_CONFIRMED"):
            print(f"  txHash: {data.get('transactionHash')}")
            break
        if state == "STATE_FAILED":
            print(f"  FAILED: {data}")
            sys.exit(2)
        time.sleep(5)

    bal_after = pusd.functions.balanceOf(dw_addr).call() / 1e6
    print(f"\npUSD después    : ${bal_after:.4f}")
    print(f"Δ pUSD          : ${bal_after - bal_before:+.4f}")


if __name__ == "__main__":
    main()
