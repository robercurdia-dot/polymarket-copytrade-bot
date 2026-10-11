"""
approve_deposit_wallet.py — Aprobar pUSD a los 3 Exchange contracts
desde la deposit wallet vía relayer WALLET batch.

USO:
  python tools/approve_deposit_wallet.py            # dry-run
  python tools/approve_deposit_wallet.py --execute  # firma y envía

Requiere en .env:
  POLY_PRIVATE_KEY           (EOA owner de la deposit wallet)
  DEPOSIT_WALLET_ADDRESS     (la deposit wallet ya desplegada)
  POLY_BUILDER_API_KEY/SECRET/PASSPHRASE (builder creds para el relayer)
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import requests
from eth_account import Account
from eth_account.messages import encode_typed_data
from py_builder_signing_sdk.signing.hmac import build_hmac_signature

# Contratos Polygon (sacados de py_clob_client_v2.config)
PUSD            = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
EXCHANGE_V2     = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_ADAPTR = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
NEG_RISK_EX_V2  = "0xe2222d279d744050d28e00520010520000310F59"
FACTORY         = "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07"
CHAIN_ID        = 137
MAX_UINT256     = 2**256 - 1
RELAYER         = "https://relayer-v2.polymarket.com"

SPENDERS = [
    ("CTF Exchange V2",  EXCHANGE_V2),
    ("NegRisk Adapter",  NEG_RISK_ADAPTR),
    ("NegRisk Exch V2",  NEG_RISK_EX_V2),
]


def approve_calldata(spender: str, amount: int) -> str:
    """Construye calldata para ERC20.approve(spender, amount)."""
    selector = "0x095ea7b3"
    spender_padded = spender.lower().replace("0x", "").rjust(64, "0")
    amount_padded  = hex(amount)[2:].rjust(64, "0")
    return selector + spender_padded + amount_padded


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
    r = requests.get(f"{RELAYER}/nonce?address={eoa}&type=WALLET",
                     timeout=10)
    r.raise_for_status()
    return int(r.json()["nonce"])


def build_batch_signature(deposit_wallet: str, nonce: int, deadline: int,
                          calls: list, private_key: str) -> str:
    """Firma EIP-712 Batch del DepositWallet domain."""
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
            "name": "DepositWallet",
            "version": "1",
            "chainId": CHAIN_ID,
            "verifyingContract": deposit_wallet,
        },
        "message": {
            "wallet":   deposit_wallet,
            "nonce":    nonce,
            "deadline": deadline,
            "calls":    [
                {"target": c["target"],
                 "value":  int(c["value"]),
                 "data":   bytes.fromhex(c["data"][2:])}
                for c in calls
            ],
        },
    }
    encoded = encode_typed_data(full_message=typed)
    signed = Account.sign_message(encoded, private_key=private_key)
    return "0x" + signed.signature.hex()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true",
                    help="Firma y envía (default: dry-run)")
    args = ap.parse_args()

    pk  = os.environ["POLY_PRIVATE_KEY"]
    eoa = Account.from_key(pk).address
    dw  = os.environ["DEPOSIT_WALLET_ADDRESS"]

    print(f"EOA owner       : {eoa}")
    print(f"Deposit wallet  : {dw}")
    print(f"Factory         : {FACTORY}")
    print(f"pUSD            : {PUSD}")
    print()

    # Construir los 3 calls de approve
    calls = []
    for label, spender in SPENDERS:
        cd = approve_calldata(spender, MAX_UINT256)
        calls.append({"target": PUSD, "value": "0", "data": cd})
        print(f"  approve {label} ({spender}) → MAX")

    nonce    = get_wallet_nonce(eoa)
    deadline = int(time.time()) + 600
    print(f"\nWALLET nonce    : {nonce}")
    print(f"Deadline        : {deadline} (+10min)")

    if not args.execute:
        print("\n[DRY-RUN] No se firma ni envía. Vuelve a lanzar con --execute.")
        return

    # Firmar Batch
    signature = build_batch_signature(dw, nonce, deadline, calls, pk)
    print(f"\nBatch signature : {signature[:18]}... (len={len(signature)})")

    body = {
        "type": "WALLET",
        "from": eoa,
        "to":   FACTORY,
        "nonce": str(nonce),
        "signature": signature,
        "depositWalletParams": {
            "depositWallet": dw,
            "deadline": str(deadline),
            "calls": [{"target": c["target"], "value": c["value"], "data": c["data"]} for c in calls],
        },
    }
    body_str = json.dumps(body, separators=(",", ":"))
    headers = _hmac_headers("POST", "/submit", body_str)

    print("\nEnviando POST /submit (type=WALLET, 3 approves)...")
    r = requests.post(f"{RELAYER}/submit", data=body_str, headers=headers, timeout=20)
    print(f"status: {r.status_code}")
    print(f"body:   {r.text[:400]}")
    r.raise_for_status()
    tx_id = r.json()["transactionID"]
    print(f"\nTransactionID: {tx_id} · polling...")

    # Polling
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


if __name__ == "__main__":
    main()
