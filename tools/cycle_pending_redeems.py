"""
cycle_pending_redeems.py — Orchestrator del ciclo completo:
  shares ganadoras (dw) → proxy → Polymarket auto-redeem →
  [tú Withdraw UI] → USDC nativo (dw) → bridge → pUSD (dw)

USO:
  python tools/cycle_pending_redeems.py            # dry-run (solo info)
  python tools/cycle_pending_redeems.py --execute  # ejecuta pasos automáticos
"""
import argparse, os, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from web3 import Web3
import config

w3 = Web3(Web3.HTTPProvider("https://polygon-bor-rpc.publicnode.com"))
PUSD = Web3.to_checksum_address("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")
USDC_N = Web3.to_checksum_address("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359")
proxy = Web3.to_checksum_address(config.POLY_WALLET_ADDRESS)
dw = Web3.to_checksum_address(config.DEPOSIT_WALLET_ADDRESS)
ABI = [{"name":"balanceOf","type":"function","inputs":[{"name":"o","type":"address"}],
        "outputs":[{"name":"","type":"uint256"}],"stateMutability":"view"}]


def bal(token, addr):
    return w3.eth.contract(address=token, abi=ABI).functions.balanceOf(addr).call() / 1e6


def status():
    return {
        "dw_pUSD":    bal(PUSD, dw),
        "dw_USDCn":   bal(USDC_N, dw),
        "proxy_pUSD": bal(PUSD, proxy),
    }


def show(label):
    s = status()
    print(f"  [{label}]  dw_pUSD=${s['dw_pUSD']:.2f}  dw_USDCn=${s['dw_USDCn']:.2f}  proxy_pUSD=${s['proxy_pUSD']:.2f}")
    return s


def run(cmd):
    print(f"\n$ {' '.join(cmd)}")
    r = subprocess.run(cmd, cwd=str(Path(__file__).resolve().parent.parent),
                        capture_output=True, text=True, timeout=300)
    print(r.stdout[-1500:])
    if r.stderr: print(f"STDERR: {r.stderr[:500]}")
    return r.returncode == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    print("=" * 70)
    print("CICLO COMPLETO PENDING REDEEMS")
    print("=" * 70)
    s0 = show("inicio")

    if not args.execute:
        print("\n[DRY-RUN] Pasos que ejecutaría:")
        print("  1. transfer_shares_to_proxy.py --execute   (shares dw→proxy)")
        print("  2. ESPERAR auto-redeem Polymarket (~10s-min)")
        print("  3. PEDIRTE Withdraw en UI a deposit wallet")
        print("  4. ESPERAR USDC nativo entrante a dw")
        print("  5. bridge_usdc_to_pusd.py --execute        (USDCn→pUSD)")
        print("\nRepite con --execute para hacerlo real.")
        return

    # PASO 1
    print("\n[1/5] Transferir shares ganadoras dw → proxy")
    if not run([sys.executable, "tools/transfer_shares_to_proxy.py", "--execute"]):
        print("⚠ falló transfer, abortando")
        return
    s1 = show("post-transfer")

    # PASO 2: esperar auto-redeem Polymarket
    print("\n[2/5] Esperando auto-redeem Polymarket en proxy (max 3 min)…")
    for i in range(36):
        sx = status()
        if sx["proxy_pUSD"] >= 5:
            print(f"  ✓ Polymarket redimió ${sx['proxy_pUSD']:.2f} pUSD en proxy")
            break
        time.sleep(5)
    else:
        print("⚠ Timeout esperando auto-redeem. Verifica polymarket.com manualmente.")
        return

    # PASO 3
    print("\n[3/5] ACCIÓN MANUAL TUYA:")
    print("  → ve a https://polymarket.com")
    print("  → pulsa Withdraw")
    print(f"  → destino: {dw}")
    print(f"  → cantidad: ${sx['proxy_pUSD']:.2f} (máx)")
    print("  → confirma en MetaMask")
    print("\n  Esperando USDC nativo entrante a deposit wallet (max 5 min)…")

    s_pre_wd = status()
    for i in range(60):
        sx = status()
        if sx["dw_USDCn"] > s_pre_wd["dw_USDCn"] + 1:
            print(f"  ✓ Detectado +${sx['dw_USDCn'] - s_pre_wd['dw_USDCn']:.2f} USDCn en dw")
            break
        time.sleep(5)
    else:
        print("⚠ Timeout esperando Withdraw. Cuando termines, ejecuta paso 5 manualmente:")
        print(f"  python tools/bridge_usdc_to_pusd.py --execute")
        return

    # PASO 5: bridge
    print("\n[5/5] Bridge USDC nativo → pUSD")
    if not run([sys.executable, "tools/bridge_usdc_to_pusd.py", "--execute"]):
        print("⚠ falló bridge")
        return

    s_final = show("FINAL")
    delta_pusd = s_final["dw_pUSD"] - s0["dw_pUSD"]
    print(f"\n✅ Ciclo completo · Δ pUSD operativo: ${delta_pusd:+.2f}")


if __name__ == "__main__":
    main()
