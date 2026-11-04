"""Rehace la whitelist de monederos a partir del leaderboard (Data API v2) y la escribe en .env.

    python tools/construir_whitelist.py [--top 200] [--min-score 0] [--solo-mostrar]

Ejecuta leaderboard_analyzer.run_analysis (los filtros del nicho deciden quién entra), toma los monederos
activos con insider_score >= --min-score y escribe REAL_D_WALLET_WHITELIST y REAL_WALLET_WHITELIST en el .env
del repo. No toca ninguna otra variable. El .env está en .gitignore.
"""
import argparse
import re
import sqlite3
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
import database  # noqa: E402
import leaderboard_analyzer  # noqa: E402


def _escribir_env(ruta: Path, valores: dict) -> None:
    texto = ruta.read_text(encoding="utf-8") if ruta.exists() else ""
    for clave, valor in valores.items():
        linea = f"{clave}={valor}"
        if re.search(rf"^{clave}=.*$", texto, re.M):
            texto = re.sub(rf"^{clave}=.*$", linea, texto, flags=re.M)
        else:
            texto += ("" if texto.endswith("\n") or not texto else "\n") + linea + "\n"
    ruta.write_text(texto, encoding="utf-8")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--top", type=int, default=200, help="traders del leaderboard mensual a analizar")
    p.add_argument("--min-score", type=float, default=0.0, help="insider_score mínimo para entrar")
    p.add_argument("--solo-mostrar", action="store_true", help="no escribe el .env")
    a = p.parse_args()

    database.init_db()
    leaderboard_analyzer.run_analysis(a.top)

    conn = sqlite3.connect(database.DB_PATH)
    filas = conn.execute(
        "SELECT address, username, insider_score, win_rate, profit_factor FROM wallets "
        "WHERE active = 1 AND insider_score >= ? ORDER BY insider_score DESC", (a.min_score,)).fetchall()
    conn.close()

    print(f"\nWhitelist: {len(filas)} monederos")
    for addr, nombre, score, wr, pf in filas:
        print(f"  {addr}  {str(nombre)[:20]:20}  score {score:.3f}  WR {wr:.0%}  PF {pf}")
    if a.solo_mostrar or not filas:
        return
    lista = ",".join(f[0] for f in filas)
    _escribir_env(RAIZ / ".env", {"REAL_D_WALLET_WHITELIST": lista, "REAL_WALLET_WHITELIST": lista})
    print(f"Escrita en {RAIZ / '.env'}")


if __name__ == "__main__":
    main()
