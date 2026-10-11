"""
withdraw_from_proxy.py — Transfiere USDC_N desde el proxy wallet (UI Polymarket)
hacia el deposit wallet (DW) para luego bridgear a pUSD.

USO:
  python tools/withdraw_from_proxy.py            # dry-run (muestra qué haría)
  python tools/withdraw_from_proxy.py --execute  # ejecuta la transacción
"""
import argparse, json, os, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import requests
from eth_account import Account
from eth_account.messages import encode_typed_data
from web3 import Web3
from py_builder_signing_sdk.signing.hmac import build_hmac_signature
import config

USDC_N   = Web3.to_checksum_address("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359")
CHAIN_ID = 137
RELAYER  = "https://relayer-v2.polymarket.com"
RPC      = "https://polygon-bor-rpc.publicnode.com"
DECIMALS = 6

ERC20_ABI = [
    {"name":"balanceOf","type":"function","inputs":[{"name":"o","type":"address"}],
     "outputs":[{"name":"","type":"uint256"}],"stateMutability":"view"},
    {"name":"transfer","type":"function","inputs":[
        {"name":"to","type":"address"},{"name":"amount","type":"uint256"}],
     "outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable"},
]

def _hmac(method, path, body=None):
    secret = os.environ["POLY_BUILDER_SECRET"]
    ts = str(int(time.time()))
    from py_builder_signing_sdk.signing.hmac import build_hmac_signature
    sig = build_hmac_signature(secret, ts, method, path, body)
    return {"POLY_BUILDER_API_KEY": os.environ["POLY_BUILDER_API_KEY"],
            "POLY_BUILDER_TIMESTAMP": ts,
            "POLY_BUILDER_PASSPHRASE": os.environ["POLY_BUILDER_PASSPHRASE"],
            "POLY_BUILDER_SIGNATURE": sig, "Content-Type": "application/json"}


def get_nonce(eoa):
    r = requests.get(f"{RELAYER}/nonce?address={eoa}&type=WALLET", timeout=10)
    return int(r.json()["nonce"])


def sign_batch(wallet_addr, nonce, deadline, calls, pk):
    """Firma un Batch EIP712 donde verifyingContract = wallet_addr."""
    typed = {
        "types": {
            "EIP712Domain": [
                {"name": "name",    "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
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
            "chainId": CHAIN_ID, "verifyingContract": wallet_addr,
        },
        "message": {
            "wallet": wallet_addr, "nonce": nonce, "deadline": deadline,
            "calls": [
                {"target": c["target"], "value": int(c["value"]),
                 "data": bytes.fromhex(c["data"][2:])}
                for c in calls
            ],
        },
    }
    s = Account.sign_message(encode_typed_data(full_message=typed), private_key=pk)
    return "0x" + s.signature.hex()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="Ejecutar (sin flag = dry-run)")
    args = ap.parse_args()

    w3    = Web3(Web3.HTTPProvider(RPC))
    pk    = os.environ["POLY_PRIVATE_KEY"]
    eoa   = Account.from_key(pk).address
    proxy = Web3.to_checksum_address(config.POLY_WALLET_ADDRESS)   # 0xd8dfc628...
    dw    = Web3.to_checksum_address(config.DEPOSIT_WALLET_ADDRESS) # 0x03c20f59...

    print(f"EOA   : {eoa}")
    print(f"Origen: {proxy}  (proxy wallet)")
    print(f"Destino: {dw}  (deposit wallet)")
    print()

    usdc = w3.eth.contract(address=USDC_N, abi=ERC20_ABI)
    balance = usdc.functions.balanceOf(proxy).call()
    balance_usdc = balance / (10 ** DECIMALS)
    print(f"USDC_N en proxy: {balance_usdc:.6f}")

    if balance == 0:
        print("Nada que transferir.")
        return

    # Reservar 0 — mover TODO al DW, el DW gestionará reservas
    amount = balance
    print(f"Transferir     : {amount / 1e6:.6f} USDC_N → DW")
    print()

    # Codificar ERC20.transfer(dw, amount)
    call_data = usdc.encode_abi("transfer", args=[dw, amount])

    calls = [{"target": USDC_N, "value": "0", "data": call_data}]

    nonce    = get_nonce(eoa)
    deadline = int(time.time()) + 3600  # 1 hora
    print(f"Nonce relayer: {nonce}  deadline: {deadline}")

    if not args.execute:
        print("\n[DRY-RUN] Simulación — llama con --execute para enviar.")
        return

    FACTORY = Web3.to_checksum_address("0x00000000000Fb5C9ADea0298D729A0CB3823Cc07")

    # Firmar usando el PROXY como verifyingContract (no la DW)
    sig = sign_batch(proxy, nonce, deadline, calls, pk)

    body = {
        "type": "WALLET",
        "from": eoa,
        "to": FACTORY,
        "nonce": str(nonce),
        "signature": sig,
        "depositWalletParams": {
            "depositWallet": proxy,
            "deadline": str(deadline),
            "calls": [{"target": c["target"], "value": c["value"], "data": c["data"]} for c in calls],
        },
    }
    body_str = json.dumps(body, separators=(",", ":"))

    print("Enviando al relayer...")
    r = requests.post(f"{RELAYER}/submit", data=body_str,
                      headers=_hmac("POST", "/submit", body_str), timeout=30)
    print(f"relayer status: {r.status_code}  body: {r.text[:500]}")

    if r.status_code not in (200, 201):
        print("ERROR en relayer.")
        return

    res = r.json()
    tx_id = res.get("transactionID")
    print(f"\nTransactionID: {tx_id} · polling...")
    for i in range(40):
        h = _hmac("GET", f"/transaction?id={tx_id}")
        rr = requests.get(f"{RELAYER}/transaction?id={tx_id}", headers=h, timeout=10).json()
        d = rr[0] if isinstance(rr, list) and rr else rr
        state = d.get("state", "?")
        tx_hash = d.get("transactionHash", "")
        print(f"  [{i*3}s] state={state}")
        if state in ("STATE_MINED", "STATE_CONFIRMED"):
            print(f"  txHash: {tx_hash}")
            break
        if state == "STATE_FAILED":
            print("  FAILED")
            break
        time.sleep(3)

    # Balance post
    new_bal = usdc.functions.balanceOf(proxy).call()
    new_dw  = usdc.functions.balanceOf(dw).call()
    print(f"\nProxy USDC_N: {new_bal/1e6:.6f}")
    print(f"DW    USDC_N: {new_dw/1e6:.6f}")


if __name__ == "__main__":
    main()
