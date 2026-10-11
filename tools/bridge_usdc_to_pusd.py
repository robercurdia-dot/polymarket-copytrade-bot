"""
bridge_usdc_to_pusd.py — Envía USDC.e o USDC nativo de la deposit wallet
al bridge de Polymarket → vuelve como pUSD operativo para el bot.

USO:
  python tools/bridge_usdc_to_pusd.py            # dry-run
  python tools/bridge_usdc_to_pusd.py --execute  # firma y envía
  python tools/bridge_usdc_to_pusd.py --execute --asset USDC_E
"""
import argparse, os, sys, json, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import requests
from eth_account import Account
from eth_account.messages import encode_typed_data
from web3 import Web3
from eth_abi import encode as abi_encode
from py_builder_signing_sdk.signing.hmac import build_hmac_signature
import config

ASSETS = {
    "USDC_N": Web3.to_checksum_address("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"),
    "USDC_E": Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"),
}
PUSD    = Web3.to_checksum_address("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")
BRIDGE  = Web3.to_checksum_address("0xB7E6f27d4E5fC98FCF03Dbd40ADf902004AF766e")
FACTORY = Web3.to_checksum_address("0x00000000000Fb5C9ADea0298D729A0CB3823Cc07")
CHAIN_ID = 137
RELAYER = "https://relayer-v2.polymarket.com"
RPC = "https://polygon-bor-rpc.publicnode.com"

ABI = [{"name":"balanceOf","type":"function","inputs":[{"name":"o","type":"address"}],
        "outputs":[{"name":"","type":"uint256"}],"stateMutability":"view"}]


def _hmac(method, path, body=None):
    secret = os.environ["POLY_BUILDER_SECRET"]
    ts = str(int(time.time()))
    sig = build_hmac_signature(secret, ts, method, path, body)
    return {"POLY_BUILDER_API_KEY":os.environ["POLY_BUILDER_API_KEY"],
            "POLY_BUILDER_TIMESTAMP":ts,
            "POLY_BUILDER_PASSPHRASE":os.environ["POLY_BUILDER_PASSPHRASE"],
            "POLY_BUILDER_SIGNATURE":sig, "Content-Type":"application/json"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--asset", default="USDC_N", choices=list(ASSETS.keys()))
    args = ap.parse_args()

    w3 = Web3(Web3.HTTPProvider(RPC))
    pk = os.environ["POLY_PRIVATE_KEY"]
    eoa = Account.from_key(pk).address
    dw = Web3.to_checksum_address(config.DEPOSIT_WALLET_ADDRESS)
    token = ASSETS[args.asset]

    bal = w3.eth.contract(address=token, abi=ABI).functions.balanceOf(dw).call()
    pusd_before = w3.eth.contract(address=PUSD, abi=ABI).functions.balanceOf(dw).call()
    print(f"Deposit wallet : {dw}")
    print(f"Asset          : {args.asset} ({token})")
    print(f"Saldo {args.asset:6s}  : ${bal/1e6:.4f}")
    print(f"pUSD ANTES     : ${pusd_before/1e6:.4f}")
    print(f"Bridge         : {BRIDGE}")

    if bal == 0:
        print("\nNada que enviar.")
        return

    sel = Web3.keccak(text="transfer(address,uint256)")[:4]
    data = sel + abi_encode(["address","uint256"], [BRIDGE, bal])
    calls = [{"target": token, "value": "0", "data": "0x" + data.hex()}]

    # Simulate
    try:
        w3.eth.call({"from": dw, "to": token, "data": "0x" + data.hex()})
        print("[simulation] transfer OK")
    except Exception as e:
        print(f"[simulation] REVERT: {e}")
        sys.exit(2)

    if not args.execute:
        print("\n[DRY-RUN] Repite con --execute para enviar.")
        return

    # Submit batch
    nonce = int(requests.get(f"{RELAYER}/nonce?address={eoa}&type=WALLET",
                              timeout=10).json()["nonce"])
    deadline = int(time.time()) + 600
    typed = {
        "types":{"EIP712Domain":[{"name":"name","type":"string"},
                                  {"name":"version","type":"string"},
                                  {"name":"chainId","type":"uint256"},
                                  {"name":"verifyingContract","type":"address"}],
                  "Call":[{"name":"target","type":"address"},
                          {"name":"value","type":"uint256"},
                          {"name":"data","type":"bytes"}],
                  "Batch":[{"name":"wallet","type":"address"},
                            {"name":"nonce","type":"uint256"},
                            {"name":"deadline","type":"uint256"},
                            {"name":"calls","type":"Call[]"}]},
        "primaryType":"Batch",
        "domain":{"name":"DepositWallet","version":"1","chainId":CHAIN_ID,
                  "verifyingContract":dw},
        "message":{"wallet":dw,"nonce":nonce,"deadline":deadline,
                    "calls":[{"target":c["target"],"value":int(c["value"]),
                              "data":bytes.fromhex(c["data"][2:])} for c in calls]},
    }
    signed = Account.sign_message(encode_typed_data(full_message=typed), private_key=pk)
    sig = "0x" + signed.signature.hex()
    body = {"type":"WALLET","from":eoa,"to":FACTORY,"nonce":str(nonce),"signature":sig,
            "depositWalletParams":{"depositWallet":dw,"deadline":str(deadline),
                                    "calls":[{"target":c["target"],"value":c["value"],
                                              "data":c["data"]} for c in calls]}}
    body_str = json.dumps(body, separators=(",", ":"))
    r = requests.post(f"{RELAYER}/submit", data=body_str,
                       headers=_hmac("POST","/submit",body_str), timeout=20)
    print(f"\nrelayer: {r.status_code} {r.text[:200]}")
    r.raise_for_status()
    tx_id = r.json()["transactionID"]
    print(f"txID: {tx_id} polling...")
    for i in range(20):
        h = _hmac("GET", f"/transaction?id={tx_id}")
        d = requests.get(f"{RELAYER}/transaction?id={tx_id}", headers=h, timeout=10).json()
        d = d[0] if isinstance(d, list) else d
        s = d.get("state")
        print(f"  [{i*3}s] state={s}")
        if s in ("STATE_MINED","STATE_CONFIRMED"):
            print(f"  txHash: {d.get('transactionHash')}")
            break
        if s == "STATE_FAILED":
            sys.exit(2)
        time.sleep(3)

    time.sleep(6)
    pusd_after = w3.eth.contract(address=PUSD, abi=ABI).functions.balanceOf(dw).call()
    print(f"\npUSD DESPUÉS: ${pusd_after/1e6:.4f}")
    print(f"Δ pUSD      : ${(pusd_after - pusd_before)/1e6:+.4f}")


if __name__ == "__main__":
    main()
