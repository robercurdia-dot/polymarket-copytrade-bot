"""
approve_allowances.py — Aprobación única (one-shot) de USDC.e
hacia los contratos Exchange de Polymarket, firmada por la EOA.

Tras este script, el CLOB API verá el balance USDC.e como colateral
operable y el bot podrá ejecutar trades reales.

USO:
  python tools/approve_allowances.py            # dry-run: muestra plan
  python tools/approve_allowances.py --execute  # firma y envía
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config
from web3 import Web3

USDC_E   = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
SPENDERS = [
    ("CTF Exchange",     "0xE111180000d2663C0091e4f400237545B87B996B"),
    ("NegRisk Exchange", "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"),
    ("NegRisk Adapter",  "0xe2222d279d744050d28e00520010520000310F59"),
]
MAX_UINT256 = 2**256 - 1
RPC         = "https://polygon-bor-rpc.publicnode.com"
CHAIN_ID    = 137

ERC20_ABI = [{
    "constant": False, "inputs": [
        {"name":"_spender","type":"address"},
        {"name":"_value","type":"uint256"}],
    "name":"approve",
    "outputs":[{"name":"","type":"bool"}],
    "type":"function",
}, {
    "constant": True, "inputs":[
        {"name":"_owner","type":"address"},
        {"name":"_spender","type":"address"}],
    "name":"allowance",
    "outputs":[{"name":"","type":"uint256"}],
    "type":"function",
}]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--execute", action="store_true",
                   help="Firma y envía (default: dry-run)")
    args = p.parse_args()

    w3 = Web3(Web3.HTTPProvider(RPC))
    if not w3.is_connected():
        print("ERROR: no se pudo conectar al RPC")
        sys.exit(1)

    pk = config.POLY_PRIVATE_KEY
    if not pk:
        print("ERROR: POLY_PRIVATE_KEY no configurado")
        sys.exit(1)
    acct = w3.eth.account.from_key(pk)
    print(f"EOA signer        : {acct.address}")
    print(f"Polygon chain_id  : {CHAIN_ID}")
    print(f"USDC.e token      : {USDC_E}")
    print(f"POL balance       : {w3.from_wei(w3.eth.get_balance(acct.address),'ether'):.4f}")

    usdc = w3.eth.contract(address=USDC_E, abi=ERC20_ABI)
    pending = []
    for label, spender in SPENDERS:
        spender = Web3.to_checksum_address(spender)
        cur = usdc.functions.allowance(acct.address, spender).call()
        status = "YA APROBADO" if cur >= MAX_UINT256 // 2 else "PENDIENTE"
        print(f"  {label:18} ({spender}) → current allowance = {cur} → {status}")
        if status == "PENDIENTE":
            pending.append((label, spender))

    if not pending:
        print("\nTodas las allowances ya están aprobadas. Nada que hacer.")
        return

    if not args.execute:
        print(f"\n[DRY-RUN] {len(pending)} approve(s) pendientes.")
        print("  Para ejecutar, vuelve a lanzar con --execute")
        return

    # Gas dinámico — Polygon puede subir a 200-400 gwei; usamos 2× el actual
    cur_gp = w3.eth.gas_price
    max_fee = max(cur_gp * 2, w3.to_wei(150, "gwei"))
    priority = max(cur_gp // 2, w3.to_wei(50, "gwei"))
    print(f"\nGas actual: {cur_gp/1e9:.0f} gwei → maxFee {max_fee/1e9:.0f} / priority {priority/1e9:.0f}")

    print(f"Ejecutando {len(pending)} approve tx(s)...")
    nonce = w3.eth.get_transaction_count(acct.address, "latest")
    for label, spender in pending:
        tx = usdc.functions.approve(spender, MAX_UINT256).build_transaction({
            "from":     acct.address,
            "nonce":    nonce,
            "chainId":  CHAIN_ID,
            "gas":      80_000,
            "maxFeePerGas":         max_fee,
            "maxPriorityFeePerGas": priority,
        })
        signed = acct.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        print(f"  → {label}: tx {tx_hash.hex()}  (esperando confirmación...)")
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        ok = receipt.status == 1
        print(f"     {'OK' if ok else 'FAIL'}  · block {receipt.blockNumber}  · gas {receipt.gasUsed}")
        nonce += 1
        time.sleep(1)

    print("\nDone. Verificando allowances finales:")
    for label, spender in SPENDERS:
        cur = usdc.functions.allowance(acct.address, Web3.to_checksum_address(spender)).call()
        print(f"  {label:18} = {cur if cur < MAX_UINT256//2 else 'MAX'}")


if __name__ == "__main__":
    main()
