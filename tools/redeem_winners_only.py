"""
redeem_winners_only.py — Redime SOLO posiciones con currentValue > 0 on-chain.
Filtra antes de firmar el batch para evitar reverts por mercados perdedores
o que no son neg-risk.
"""
import json, os, sys, time, requests
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from web3 import Web3
import config
from tools.redeem_closed_positions import (
    collect_redeem_candidates, build_redeem_calls, build_batch_signature,
    get_wallet_nonce, _hmac_headers, FACTORY, RELAYER, PUSD, ERC20_ABI
)
from eth_account import Account

DW = config.DEPOSIT_WALLET_ADDRESS.lower()

# 1. Sacar de la API los conditionIds con valor > 0
pos = requests.get('https://data-api.polymarket.com/positions',
    params={'user': DW, 'limit': 500, 'sizeThreshold': 0}, timeout=10).json() or []
winners = {(p.get('conditionId') or '').lower()
           for p in pos
           if p.get('redeemable') and float(p.get('currentValue', 0) or 0) > 0.5}
print(f"Mercados con redeemable+value>0: {len(winners)}")
for cid in winners:
    parts = [p for p in pos if (p.get('conditionId') or '').lower() == cid]
    total = sum(float(p.get('currentValue', 0) or 0) for p in parts)
    print(f"  {cid[:18]}… ${total:.2f}")

# 2. Obtener grupos del script original
groups, dw_addr, w3 = collect_redeem_candidates(include_losers=False)

# 3. Filtrar groups → solo conditionIds en winners
filtered = {mid: g for mid, g in groups.items() if mid.lower() in winners}
skipped = {mid: g for mid, g in groups.items() if mid.lower() not in winners}
print(f"\nGrupos del script: {len(groups)}  →  filtrados (con valor): {len(filtered)}  ·  descartados (sin valor): {len(skipped)}")
for mid in skipped:
    print(f"  SKIP {mid[:18]}…  (script lo marcó won pero on-chain perdió)")

if not filtered:
    print("\nNada que redimir (con valor real).")
    sys.exit(0)

# 4. Construir + enviar batch
calls = build_redeem_calls(filtered)
print(f"\n{len(calls)} call(s) a firmar:")
for c in calls:
    print(f"  - {c['_label']}")
    c.pop("_label", None)

if "--execute" not in sys.argv:
    print("\n[DRY-RUN] Pasa --execute para enviar.")
    sys.exit(0)

pk = os.environ["POLY_PRIVATE_KEY"]
eoa = Account.from_key(pk).address
nonce = get_wallet_nonce(eoa)
deadline = int(time.time()) + 600
sig = build_batch_signature(config.DEPOSIT_WALLET_ADDRESS, nonce, deadline, calls, pk)
print(f"\nWALLET nonce={nonce} · sig={sig[:18]}…")

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
print(f"\ntx={tx_id} · polling…")
path = f"/transaction?id={tx_id}"
for i in range(40):
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

pusd = w3.eth.contract(address=PUSD, abi=ERC20_ABI)
bal = pusd.functions.balanceOf(dw_addr).call() / 1e6
print(f"\npUSD final: ${bal:.4f}")
