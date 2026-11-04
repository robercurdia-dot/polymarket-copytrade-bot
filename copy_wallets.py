"""
Monederos que copia el agente DEF_COPY y su escala.

Se configuran en el .env con COPY_WALLETS=dirección:Nombre:escala,dirección:Nombre:escala,...
(tools/seleccionar_wallets.py --escribir-env la rellena con los criterios del README).
La escala es la fracción del tamaño del trader que replicamos: README «tamaño = compra × ESCALA».

Sin COPY_WALLETS se mantiene el comportamiento histórico: DEF_COPY copia a Swisstony y los agentes
sombra (DEF_COPY_05, DEF_WIGGLEW, DEF_KINDER) a sus monederos, con las escalas DEFINITIVE_*_SCALE.
"""
from __future__ import annotations

import os

import config  # noqa: F401 — carga el .env del repo

SWISSTONY = "0x204f72f35326db932158cba6adff0b9a1da95e14"

# Monederos históricos (antes estaban repetidos en monitor, paper_executor y el watcher).
LEGADO = {
    SWISSTONY: ("Swisstony", "DEFINITIVE_SWIS_SCALE", "0.00784"),
    "0x84dbb7103982e3617704a2ed7d5b39691952aeeb": ("Soarin22", "DEFINITIVE_SOARIN_SCALE", "0.005"),
    "0xc96e5287ab294ac0388c2ddb00180fc464cff1f9": ("wigglew", "DEFINITIVE_WIGGLEW_SCALE", "0.02"),
    "0xa6d9b55a6a3a54a9d50ca94c64f00af69c50fd2f": ("kinderSman", "DEFINITIVE_KINDER_SCALE", "0.01"),
}


def _legado() -> dict[str, dict]:
    return {addr: {"name": nombre, "scale": float(os.getenv(var, defecto))}
            for addr, (nombre, var, defecto) in LEGADO.items()}


def configurados() -> dict[str, dict]:
    """Monederos de COPY_WALLETS: {dirección: {"name", "scale"}}. Vacío si no está configurado."""
    out = {}
    for item in (os.getenv("COPY_WALLETS", "") or "").split(","):
        partes = [p.strip() for p in item.split(":")]
        if len(partes) != 3 or not partes[0].startswith("0x"):
            continue
        try:
            out[partes[0].lower()] = {"name": partes[1] or partes[0][:10], "scale": float(partes[2])}
        except ValueError:
            continue
    return out


def para_def_copy() -> dict[str, dict]:
    """Monederos que replica DEF_COPY (y sus líneas base DEF_TP / DEF_HOLD)."""
    return configurados() or {SWISSTONY: _legado()[SWISSTONY]}


def todos() -> dict[str, dict]:
    """Todos los monederos de copia, históricos y configurados: los que se saltan filtros pensados para
    señales de terceros (tamaño mínimo, cobertura, nicho, edad de la señal)."""
    return {**_legado(), **configurados()}


def escala(addr: str) -> float:
    datos = todos().get((addr or "").lower())
    return datos["scale"] if datos else float(os.getenv("DEFINITIVE_SWIS_SCALE", "0.00784"))


def nombre(addr: str) -> str:
    datos = todos().get((addr or "").lower())
    return datos["name"] if datos else (addr or "")[:10]
