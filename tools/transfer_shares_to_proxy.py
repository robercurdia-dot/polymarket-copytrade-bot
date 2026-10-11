"""
transfer_shares_to_proxy.py — Mueve shares CTF ganadoras (closed/won)
desde la deposit wallet (no UI) hacia la proxy legacy (sí UI Polymarket)
para que el usuario pueda hacer "Claim" en la web.

USO:
  python tools/transfer_shares_to_proxy.py            # dry-run
  python tools/transfer_shares_to_proxy.py --execute  # firma y envía
  python tools/transfer_shares_to_proxy.py --execute --include-losers
"""
import argparse, json, os, sqlite3, sys, time
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

CTF      = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
FACTORY  = Web3.to_checksum_address("0x00000000000Fb5C9ADea0298D729A0CB3823Cc07")
CHAIN_ID = 137
RELAYER  = "https://relayer-v2.polymarket.com"
RPC      = "https://polygon-bor-rpc.publicnode.com"

ABI = [{"name":"balanceOf","type":"function","inputs":[
        {"name":"o","type":"address"},{"name":"i","type":"uint256"}],
        "outputs":[{"name":"","type":"uint256"}],"stateMutability":"view"}]


def _hmac(method, path, body=None):
    secret = os.environ["POLY_BUILDER_SECRET"]
    ts = str(int(time.time()))
    sig = build_hmac_signature(secret, ts, method, path, body)
    return {"POLY_BUILDER_API_KEY":os.environ["POLY_BUILDER_API_KEY"],
            "POLY_BUILDER_TIMESTAMP":ts,
            "POLY_BUILDER_PASSPHRASE":os.environ["POLY_BUILDER_PASSPHRASE"],
            "POLY_BUILDER_SIGNATURE":sig, "Content-Type":"application/json"}


def get_nonce(eoa):
    return int(requests.get(f"{RELAYER}/nonce?address={eoa}&type=WALLET",
                            timeout=10).json()["nonce"])


def sign_batch(dw, nonce, deadline, calls, pk):
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
    s = Account.sign_message(encode_typed_data(full_message=typed), private_key=pk)
    return "0x" + s.signature.hex()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--include-losers", action="store_true",
                    help="Incluir también shares perdedoras (limpieza, valen $0)")
    ap.add_argument("--include-open", action="store_true",
                    help="Incluir también shares de posiciones aún abiertas (peligro: bot pierde acceso)")
    args = ap.parse_args()

    w3 = Web3(Web3.HTTPProvider(RPC))
    pk  = os.environ["POLY_PRIVATE_KEY"]
    eoa = Account.from_key(pk).address
    dw  = Web3.to_checksum_address(config.DEPOSIT_WALLET_ADDRESS)
    proxy = Web3.to_checksum_address(config.POLY_WALLET_ADDRESS)

    print(f"EOA      : {eoa}")
    print(f"Origen   : {dw}   (deposit wallet)")
    print(f"Destino  : {proxy}  (proxy legacy, UI Polymarket)")
    print()

    ctf = w3.eth.contract(address=CTF, abi=ABI)
    conn = sqlite3.connect("polymarket_insider.db")

    # Filtros sobre status según opciones
    status_filter = ["closed"]
    if args.include_open:
        status_filter.append("open")
    placeholders = ",".join("?" * len(status_filter))
    rows = conn.execute(f"""
        SELECT DISTINCT bt.token_id, bt.outcome, bt.id
        FROM bot_trades bt
        WHERE bt.paper_trade=0 AND bt.status IN ({placeholders})
          AND bt.token_id IS NOT NULL
        ORDER BY bt.id
    """, status_filter).fetchall()

    # Dedup por token_id: dos trades pueden compartir el mismo positionId
    # (varias compras sobre el mismo outcome). El balance on-chain es UNO solo.
    seen_tokens = set()
    ids, amounts, info = [], [], []
    for tok, outcome, tid in rows:
        if not args.include_losers and outcome != "won":
            continue
        tok_i = int(tok)
        if tok_i in seen_tokens:
            continue
        seen_tokens.add(tok_i)
        bal = ctf.functions.balanceOf(dw, tok_i).call()
        if bal == 0:
            continue
        ids.append(tok_i)
        amounts.append(bal)
        info.append((tid, outcome, bal/1e6))
        if len(ids) > 50:
            break

    if not ids:
        print("Nada que transferir.")
        return

    print(f"Tokens a transferir: {len(ids)}")
    won_total = 0.0
    for (tid, oc, sh), tk in zip(info, ids):
        marker = "★ WIN" if oc == "won" else ("lost" if oc == "lost" else oc)
        if oc == "won":
            won_total += sh
        print(f"  trade #{tid} ({marker:6s})  {sh:.4f} shares  token={str(tk)[:14]}…")
    print(f"\nValor estimado al claimar en UI (solo wins): ${won_total:.2f}")

    # Construir calldata safeBatchTransferFrom(from, to, ids, amounts, data)
    sel = Web3.keccak(text="safeBatchTransferFrom(address,address,uint256[],uint256[],bytes)")[:4]
    data = sel + abi_encode(
        ["address","address","uint256[]","uint256[]","bytes"],
        [dw, proxy, ids, amounts, b""]
    )
    calls = [{"target": CTF, "value": "0", "data": "0x" + data.hex()}]

    # eth_call simulación para validar
    try:
        w3.eth.call({"from": dw, "to": CTF, "data": "0x" + data.hex()})
        print("\n[simulation] safeBatchTransferFrom → OK ✓")
    except Exception as e:
        print(f"\n[simulation] REVERT: {e}")
        if "ERC1155Receiver" in str(e):
            print("  ⚠ La proxy NO acepta ERC-1155. Hay que usar otra ruta.")
        if not args.execute:
            return

    if not args.execute:
        print("\n[DRY-RUN] Vuelve a ejecutar con --execute para enviar.")
        return

    nonce = get_nonce(eoa)
    deadline = int(time.time()) + 600
    sig = sign_batch(dw, nonce, deadline, calls, pk)

    body = {"type":"WALLET","from":eoa,"to":FACTORY,
            "nonce":str(nonce),"signature":sig,
            "depositWalletParams":{"depositWallet":dw,"deadline":str(deadline),
                                    "calls":[{"target":c["target"],"value":c["value"],
                                              "data":c["data"]} for c in calls]}}
    body_str = json.dumps(body, separators=(",", ":"))
    r = requests.post(f"{RELAYER}/submit", data=body_str,
                       headers=_hmac("POST","/submit",body_str), timeout=20)
    print(f"\nrelayer status: {r.status_code}  body: {r.text[:300]}")
    r.raise_for_status()
    tx_id = r.json()["transactionID"]
    print(f"TransactionID: {tx_id} · polling...")

    for i in range(40):
        h = _hmac("GET", f"/transaction?id={tx_id}")
        rr = requests.get(f"{RELAYER}/transaction?id={tx_id}", headers=h, timeout=10).json()
        d = rr[0] if isinstance(rr, list) and rr else rr
        s = d.get("state")
        print(f"  [{i*3}s] state={s}")
        if s in ("STATE_MINED","STATE_CONFIRMED"):
            print(f"  txHash: {d.get('transactionHash')}")
            break
        if s == "STATE_FAILED":
            sys.exit(2)
        time.sleep(3)


if __name__ == "__main__":
    main()
