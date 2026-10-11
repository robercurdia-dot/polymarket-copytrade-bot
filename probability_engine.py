"""
probability_engine.py
---------------------
Funciones matemáticas para evaluar y gestionar el riesgo de las wallets.

  cumulative_probability(win_rate, n_trades)
      Probabilidad de ganar AL MENOS UNA de n apuestas independientes.

  kelly_fraction(win_rate, avg_win, avg_loss)
      Fracción de Kelly: tamaño óptimo de apuesta como fracción del bankroll.

  daily_performance(wallet)
      Comprueba si la wallet ha perdido 3 trades seguidos hoy.
      Si es así, devuelve (True, motivo) — el monitor la pausará 24h.
      En caso contrario devuelve (False, "").
"""

import math
import database
from datetime import datetime, timedelta

# Pausa automática: N pérdidas consecutivas en el mismo día
MAX_CONSECUTIVE_LOSSES = 3


def cumulative_probability(win_rate: float, n_trades: int) -> float:
    """
    Probabilidad de que al menos UNA de las n apuestas sea ganadora,
    asumiendo eventos independientes con probabilidad win_rate.

    P(al menos 1 win) = 1 - P(todas pérdidas) = 1 - (1-p)^n

    >>> round(cumulative_probability(0.75, 1), 4)
    0.75
    >>> round(cumulative_probability(0.75, 3), 4)
    0.9844
    >>> round(cumulative_probability(0.5, 10), 4)
    0.999
    """
    if not 0 <= win_rate <= 1:
        raise ValueError("win_rate debe estar entre 0 y 1")
    if n_trades < 1:
        raise ValueError("n_trades debe ser >= 1")
    return 1.0 - (1.0 - win_rate) ** n_trades


def kelly_fraction(win_rate: float, avg_win: float, avg_loss: float) -> float:
    """
    Criterio de Kelly completo: fracción óptima del bankroll a apostar.

        f* = (p * avg_win - q * avg_loss) / avg_win

    donde p = win_rate, q = 1 - win_rate.

    Retorna 0.0 si la fórmula da negativo (edge negativo) o si
    los inputs son inválidos.

    Normalmente se usa la mitad de Kelly (half-Kelly) para mayor
    conservadurismo — el caller puede dividir entre 2.

    >>> round(kelly_fraction(0.75, 2.0, 1.0), 4)
    0.625
    >>> kelly_fraction(0.4, 1.0, 2.0)  # edge negativo
    0.0
    """
    if avg_win <= 0 or avg_loss <= 0:
        return 0.0
    if not 0 < win_rate < 1:
        return 0.0
    q = 1.0 - win_rate
    f = (win_rate * avg_win - q * avg_loss) / avg_win
    return max(0.0, round(f, 4))


def daily_performance(wallet: str):
    """
    Analiza los trades reales de 'wallet' ejecutados hoy por el bot.
    Si los últimos MAX_CONSECUTIVE_LOSSES trades del día son todos
    perdedores (pnl_usdc < 0), devuelve (True, motivo).
    En caso contrario (False, "").

    Un trade sin pnl_usdc registrado (outcome='open') se ignora —
    solo se evalúan trades ya resueltos (pnl_usdc != 0).

    La pausa es informativa: monitor.py decide no procesar más trades
    de esta wallet hasta la próxima sesión.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    conn = database.get_connection()
    c = conn.cursor()
    # Traer los últimos trades del día ya resueltos, más recientes primero
    c.execute("""
        SELECT pnl_usdc FROM bot_trades
        WHERE detected_trade_id IN (
            SELECT id FROM detected_trades WHERE wallet_address = ?
        )
        AND paper_trade = 0
        AND executed_at LIKE ?
        AND pnl_usdc != 0
        ORDER BY executed_at DESC
        LIMIT ?
    """, (wallet, today + "%", MAX_CONSECUTIVE_LOSSES))
    rows = c.fetchall()
    conn.close()

    if len(rows) < MAX_CONSECUTIVE_LOSSES:
        return False, ""

    # Todos negativos = racha de pérdidas
    all_losses = all(row[0] < 0 for row in rows)
    if all_losses:
        reason = f"{MAX_CONSECUTIVE_LOSSES} pérdidas consecutivas hoy"
        print(f"[PAUSA] {wallet[:10]}... — {reason}")
        return True, reason

    return False, ""


if __name__ == "__main__":
    print("=== probability_engine tests ===")
    print(f"cumulative_probability(0.75, 3) = {cumulative_probability(0.75, 3):.4f}  (esperado ~0.9844)")
    print(f"cumulative_probability(0.5, 10)  = {cumulative_probability(0.5, 10):.4f}  (esperado ~0.999)")
    print(f"kelly_fraction(0.75, 2.0, 1.0)  = {kelly_fraction(0.75, 2.0, 1.0):.4f}  (esperado 0.625)")
    print(f"kelly_fraction(0.4, 1.0, 2.0)   = {kelly_fraction(0.4, 1.0, 2.0):.4f}   (esperado 0.0)")
    print()
    print("daily_performance requiere BD activa.")
