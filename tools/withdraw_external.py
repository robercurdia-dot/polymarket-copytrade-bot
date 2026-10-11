"""
withdraw_external.py — Envía un asset (pUSD/USDC.e/USDC nativo) desde la
deposit wallet hacia una dirección externa vía batch relayer.

USO:
  python tools/withdraw_external.py --to 0xABC... --amount 5 --asset pUSD
  python tools/withdraw_external.py --to 0xABC... --amount 5 --asset USDC_E --execute
  python tools/withdraw_external.py --to 0xABC... --amount 5 --asset USDC_N --execute

  (sin --execute = dry-run)

Importable como módulo: withdraw_external(to, amount, asset, dry_run=False)
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
from web3 import Web3
from eth_abi import encode as abi_encode
from py_builder_signing_sdk.signing.hmac import build_hmac_signature
import config

# Asset registry
ASSETS = {
    "pUSD":   {"addr": "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB", "decimals": 6},
    "USDC_E": {"addr": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174", "decimals": 6},
    "USDC_N": {"addr": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", "decimals": 6},
}

FACTORY = Web3.to_checksum_address("0x00000000000Fb5C9ADea0298D729A0CB3823Cc07")
CHAIN_ID = 137
RELAYER  = "https://relayer-v2.polymarket.com"
RPC      = "https://polygon-bor-rpc.publicnode.com"

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
    sig = build_hmac_signature(secret, ts, method, path, body)
    return {"POLY_BUILDER_API_KEY":os.environ["POLY_BUILDER_API_KEY"],
            "POLY_BUILDER_TIMESTAMP":ts,
            "POLY_BUILDER_PASSPHRASE":os.environ["POLY_BUILDER_PASSPHRASE"],
            "POLY_BUILDER_SIGNATURE":sig, "Content-Type":"application/json"}


def withdraw_external(to: str, amount: float, asset: str = "pUSD",
                      dry_run: bool = True, min_reserve: float = 20.0):
    """Returns dict {ok, tx_hash, status, msg}."""
    if asset not in ASSETS:
        return {"ok": False, "msg": f"asset desconocido: {asset}"}
    if not Web3.is_address(to):
        return {"ok": False, "msg": f"address inválida: {to}"}

    w3 = Web3(Web3.HTTPProvider(RPC))
    asset_meta = ASSETS[asset]
    token = Web3.to_checksum_address(asset_meta["addr"])
    decimals = asset_meta["decimals"]
    dw = Web3.to_checksum_address(config.DEPOSIT_WALLET_ADDRESS)
    dest = Web3.to_checksum_address(to)
    amount_base = int(round(amount * (10 ** decimals)))

    # Validar balance y reserve
    erc20 = w3.eth.contract(address=token, abi=ERC20_ABI)
    bal = erc20.functions.balanceOf(dw).call()
    bal_human = bal / (10 ** decimals)
    if amount_base > bal:
        return {"ok": False,
                "msg": f"saldo insuficiente: {bal_human:.4f} {asset} disponibles, pediste {amount:.4f}"}

    # Solo aplicar reserve a pUSD (es el operativo del bot)
    if asset == "pUSD":
        remaining = bal_human - amount
        if remaining < min_reserve:
            return {"ok": False,
                    "msg": f"violaría reserva mínima: tras withdraw quedarían ${remaining:.2f} < ${min_reserve:.2f}"}

    # Build calldata: ERC20.transfer(dest, amount_base)
    sel = Web3.keccak(text="transfer(address,uint256)")[:4]
    data = sel + abi_encode(["address","uint256"], [dest, amount_base])
    calls = [{"target": token, "value": "0", "data": "0x" + data.hex()}]

    # Simulate
    try:
        w3.eth.call({"from": dw, "to": token, "data": "0x"+data.hex()})
    except Exception as e:
        return {"ok": False, "msg": f"simulation REVERT: {e}"}

    if dry_run:
        return {"ok": True, "dry_run": True,
                "msg": f"[DRY-RUN] enviaría {amount:.4f} {asset} a {dest}",
                "balance_before": bal_human,
                "balance_after_estimate": bal_human - amount}

    # Execute via relayer
    pk = os.environ["POLY_PRIVATE_KEY"]
    eoa = Account.from_key(pk).address

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
    signed = Account.sign_message(encode_typed_data(full_message=typed),
                                    private_key=pk)
    sig = "0x" + signed.signature.hex()
    body = {"type":"WALLET","from":eoa,"to":FACTORY,
            "nonce":str(nonce),"signature":sig,
            "depositWalletParams":{"depositWallet":dw,"deadline":str(deadline),
                                    "calls":[{"target":c["target"],"value":c["value"],
                                              "data":c["data"]} for c in calls]}}
    body_str = json.dumps(body, separators=(",", ":"))
    r = requests.post(f"{RELAYER}/submit", data=body_str,
                       headers=_hmac("POST","/submit",body_str), timeout=20)
    if r.status_code != 200:
        return {"ok": False, "msg": f"relayer error {r.status_code}: {r.text[:200]}"}
    tx_id = r.json()["transactionID"]
    tx_hash = r.json().get("transactionHash")

    # Poll a few seconds for state
    for i in range(20):
        h = _hmac("GET", f"/transaction?id={tx_id}")
        rr = requests.get(f"{RELAYER}/transaction?id={tx_id}", headers=h, timeout=10).json()
        d = rr[0] if isinstance(rr, list) and rr else rr
        s = d.get("state")
        if s in ("STATE_MINED","STATE_CONFIRMED"):
            tx_hash = d.get("transactionHash") or tx_hash
            return {"ok": True, "tx_hash": tx_hash, "status": s,
                    "msg": f"{amount} {asset} enviados a {dest}",
                    "balance_before": bal_human}
        if s == "STATE_FAILED":
            return {"ok": False, "msg": f"tx failed: {d}"}
        time.sleep(2)
    return {"ok": True, "tx_hash": tx_hash, "status": "pending",
            "msg": "submitted, polling timeout"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", required=True, help="address destino")
    ap.add_argument("--amount", required=True, type=float, help="cantidad (humanos, no base units)")
    ap.add_argument("--asset", default="pUSD", choices=list(ASSETS.keys()))
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    res = withdraw_external(args.to, args.amount, args.asset, dry_run=not args.execute)
    print(json.dumps(res, indent=2, default=str))
    sys.exit(0 if res.get("ok") else 1)


if __name__ == "__main__":
    main()
