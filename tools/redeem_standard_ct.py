"""
redeem_standard_ct.py — Redime mercados standard CTF (no NegRisk).
Llama ConditionalTokens.redeemPositions(collateralToken, parentCollectionId,
conditionId, indexSets) via batch WALLET.
"""
import json, os, sys, time, requests
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from web3 import Web3
from eth_abi import encode as abi_encode
from eth_account import Account
import config
from tools.redeem_closed_positions import (
    build_batch_signature, get_wallet_nonce, _hmac_headers,
    FACTORY, RELAYER, PUSD, ERC20_ABI, CTF, NEGRISK_AD
)

RPC = "https://polygon-bor-rpc.publicnode.com"
USDC_E = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
DW = Web3.to_checksum_address(config.DEPOSIT_WALLET_ADDRESS)
w3 = Web3(Web3.HTTPProvider(RPC))

# 1. Sacar mercados standard con valor on-chain
pos = requests.get('https://data-api.polymarket.com/positions',
    params={'user': str(DW).lower(), 'limit': 500, 'sizeThreshold': 0},
    timeout=10).json() or []

standard_winners = []
seen = set()
for p in pos:
    if not p.get('redeemable'): continue
    if float(p.get('currentValue', 0) or 0) <= 0.5: continue
    if p.get('negativeRisk'): continue
    cid = (p.get('conditionId') or '').lower()
    if cid in seen: continue
    seen.add(cid)
    standard_winners.append({
        'cid': cid,
        'title': p.get('title', '')[:50],
        'outcome': p.get('outcome'),
        'value': float(p.get('currentValue', 0)),
    })

print(f"Mercados standard ganadores: {len(standard_winners)}")
total = sum(w['value'] for w in standard_winners)
print(f"Valor total a redimir: ${total:.2f}\n")
for w in standard_winners:
    print(f"  {w['cid'][:18]}… ${w['value']:.2f}  {w['outcome']} ({w['title']})")

if not standard_winners:
    sys.exit(0)

# 2. Construir calls
sel_red = Web3.keccak(text="redeemPositions(address,bytes32,bytes32,uint256[])")[:4]
ZERO32 = b"\x00" * 32

calls = []
for w in standard_winners:
    cid = bytes.fromhex(w['cid'][2:])
    # indexSets=[1,2] cubre ambos outcomes (binary). El no-ganador devuelve 0.
    data = sel_red + abi_encode(
        ["address", "bytes32", "bytes32", "uint256[]"],
        [USDC_E, ZERO32, cid, [1, 2]],
    )
    calls.append({
        "target": CTF, "value": "0", "data": "0x" + data.hex(),
        "_label": f"CT.redeem {w['cid'][:14]}… (~${w['value']:.2f} {w['outcome']})",
    })

print(f"\n{len(calls)} call(s):")
for c in calls:
    print(f"  - {c['_label']}")
    c.pop("_label", None)

if "--execute" not in sys.argv:
    print("\n[DRY-RUN] --execute para firmar.")
    sys.exit(0)

# 3. Firmar y enviar
pk = os.environ["POLY_PRIVATE_KEY"]
eoa = Account.from_key(pk).address
nonce = get_wallet_nonce(eoa)
deadline = int(time.time()) + 600
sig = build_batch_signature(config.DEPOSIT_WALLET_ADDRESS, nonce, deadline, calls, pk)
print(f"\nnonce={nonce} sig={sig[:18]}…")

body = {
    "type": "WALLET", "from": eoa, "to": FACTORY,
    "nonce": str(nonce), "signature": sig,
    "depositWalletParams": {
        "depositWallet": config.DEPOSIT_WALLET_ADDRESS,
        "deadline": str(deadline),
        "calls": [{"target": c["target"], "value": c["value"], "data": c["data"]} for c in calls],
    },
}
body_str = json.dumps(body, separators=(",", ":"))
hdr = _hmac_headers("POST", "/submit", body_str)
r = requests.post(f"{RELAYER}/submit", data=body_str, headers=hdr, timeout=20)
print(f"status={r.status_code}  body={r.text[:300]}")
if r.status_code != 200:
    sys.exit(2)
tx_id = r.json()["transactionID"]
print(f"\ntx={tx_id}")

bal_before = w3.eth.contract(address=USDC_E, abi=ERC20_ABI).functions.balanceOf(DW).call() / 1e6
print(f"USDC.e antes: ${bal_before:.4f}")

path = f"/transaction?id={tx_id}"
for i in range(48):
    h = _hmac_headers("GET", path)
    rr = requests.get(f"{RELAYER}{path}", headers=h, timeout=10).json()
    d = rr[0] if isinstance(rr, list) and rr else rr
    s = d.get("state")
    print(f"  [{i*5:>3}s] state={s}")
    if s in ("STATE_MINED", "STATE_CONFIRMED"):
        print(f"  txHash: {d.get('transactionHash')}")
        break
    if s == "STATE_FAILED":
        print(f"  FAILED: {d}")
        sys.exit(2)
    time.sleep(5)

bal_after = w3.eth.contract(address=USDC_E, abi=ERC20_ABI).functions.balanceOf(DW).call() / 1e6
pusd_after = w3.eth.contract(address=PUSD, abi=ERC20_ABI).functions.balanceOf(DW).call() / 1e6
print(f"\nUSDC.e después: ${bal_after:.4f}  Δ=${bal_after - bal_before:+.4f}")
print(f"pUSD: ${pusd_after:.4f}")
