"""
trade_evaluator.py — IA que evalúa trades ANTES de alertar
===========================================================
Flujo por trade:
  1. Recibe datos de mercado + stats del trader + historial
  2. Llama a Claude Haiku con un prompt estructurado
  3. Parsea respuesta JSON: score 1-10, reasoning, size, risks, verdict
  4. Guarda evaluación en BD
  5. Devuelve dict con todo para que trusted_scanner decida alertar o no

Umbral por defecto: confidence ≥ 5 → alerta  |  < 5 → skip silencioso
"""

import json
import re
import asyncio
from datetime import datetime

import anthropic
import config
import database

_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

MODEL          = "claude-haiku-4-5-20251001"   # rápido: ~2-3 seg
MIN_CONFIDENCE = 5                              # umbral para alertar

# ── Prompt ────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert Polymarket trade evaluator. Your job is to assess whether a copy-trade opportunity is worth taking based on trader stats, market conditions, and historical performance.

You MUST respond with valid JSON only — no markdown, no explanation outside the JSON.

JSON schema:
{
  "confidence": <int 1-10>,
  "verdict": "<COPY|SKIP>",
  "reasoning": ["<bullet 1>", "<bullet 2>", "<bullet 3>"],
  "recommended_size_pct": <int 25-100>,
  "risks": ["<risk 1>", "<risk 2>"],
  "summary": "<one sentence>"
}

Scoring guide:
8-10: Strong signal — high-WR trader, good entry price, reasonable timeframe, solid niche performance
5-7: Moderate signal — some concerns but opportunity is real
3-4: Weak signal — significant concerns (low WR in this niche, expensive entry, very long timeframe)
1-2: Skip — trader losing in this niche, entry too expensive (>0.80), or <7 days with no edge

recommended_size_pct: % of MAX_TRADE_SIZE to allocate (25/50/75/100)."""

def _build_user_prompt(market_data: dict, trade_data: dict, trader_stats: dict,
                       similar_history: list) -> str:
    hist_lines = ""
    if similar_history:
        wins   = sum(1 for t in similar_history if t.get("pnl_usdc", 0) > 0)
        total  = len(similar_history)
        hist_lines = (f"\nSIMILAR TRADES HISTORY (same niche, this trader):\n"
                      f"  {wins}W / {total - wins}L out of last {total} "
                      f"({round(wins/total*100) if total else 0}% WR)\n")

    niche_wr = trader_stats.get("niche_win_rate")
    niche_line = (f"  Niche WR ({trade_data.get('niche','?')}): "
                  f"{round(niche_wr*100)}%" if niche_wr else "")

    return f"""TRADE TO EVALUATE:

MARKET: {market_data.get('question', '?')}
SIDE: {trade_data.get('side', '?')} (betting this outcome wins)
ENTRY PRICE: {trade_data.get('price', '?')} (implied prob {round(float(trade_data.get('price', 0.5))*100)}%)
POTENTIAL RETURN: +{trade_data.get('retorno', '?')}x if wins
DAYS TO RESOLUTION: {trade_data.get('days_left', '?')}
NICHE: {trade_data.get('niche', '?').upper()}
INSIDER BET SIZE: ${round(float(trade_data.get('size', 0)))} USDC

TRADER STATS ({trader_stats.get('name', '?')}):
  Historical WR: {round(float(trader_stats.get('win_rate', 0))*100)}%
  Total closed: {trader_stats.get('total_closed', '?')}
  Profit Factor: {trader_stats.get('profit_factor', '?')}x
  Avg win: ${trader_stats.get('avg_win', '?')} / Avg loss: ${trader_stats.get('avg_loss', '?')}
{niche_line}
  Real PnL (tracked by bot): ${trader_stats.get('real_pnl_usdc', 0):+.0f} USDC
  Bot copies: {trader_stats.get('total_copied', 0)}W:{trader_stats.get('total_won', 0)} L:{trader_stats.get('total_lost', 0)}
{hist_lines}
Evaluate this trade and return JSON."""


# ── Core evaluator ────────────────────────────────────────────────────────────

async def evaluate_trade(market_data: dict, trade_data: dict,
                         trader_stats: dict, similar_history: list | None = None,
                         trade_id: int | None = None) -> dict:
    """
    Evalúa un trade con Claude Haiku.

    Parámetros:
      market_data     — {question}
      trade_data      — {side, price, retorno, days_left, niche, size}
      trader_stats    — {name, win_rate, total_closed, profit_factor, avg_win, avg_loss,
                         niche_win_rate, real_pnl_usdc, total_copied, total_won, total_lost}
      similar_history — lista de trades previos en mismo nicho [{pnl_usdc, ...}]
      trade_id        — detected_trade id para guardar en BD

    Devuelve:
      {confidence, verdict, reasoning, recommended_size_pct, risks, summary, error?}
    """
    similar_history = similar_history or []
    prompt = _build_user_prompt(market_data, trade_data, trader_stats, similar_history)

    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: _client.messages.create(
                model=MODEL,
                max_tokens=512,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
        )
        raw = response.content[0].text.strip()

        # Extraer JSON aunque haya texto adicional
        json_match = re.search(r'\{[\s\S]*\}', raw)
        if not json_match:
            raise ValueError(f"No JSON in response: {raw[:200]}")
        result = json.loads(json_match.group())

        # Normalizar campos
        result["confidence"]          = int(result.get("confidence", 5))
        result["verdict"]             = result.get("verdict", "SKIP").upper()
        result["reasoning"]           = result.get("reasoning", [])
        result["recommended_size_pct"]= int(result.get("recommended_size_pct", 50))
        result["risks"]               = result.get("risks", [])
        result["summary"]             = result.get("summary", "")

        # Guardar en BD
        if trade_id is not None:
            try:
                database.store_trade_evaluation(
                    detected_trade_id     = trade_id,
                    trader_wallet         = trader_stats.get("wallet", ""),
                    market_question       = market_data.get("question", ""),
                    confidence_score      = result["confidence"],
                    verdict               = result["verdict"],
                    reasoning_json        = json.dumps(result["reasoning"]),
                    recommended_size_pct  = result["recommended_size_pct"],
                    risks_json            = json.dumps(result["risks"]),
                    summary               = result["summary"],
                    model_used            = MODEL,
                )
            except Exception as db_err:
                print(f"[EVAL] DB error: {db_err}")

        return result

    except Exception as e:
        print(f"[EVAL] Error evaluating trade: {e}")
        return {
            "confidence": 5,
            "verdict": "COPY",
            "reasoning": ["Evaluación no disponible — fallback a alerta estándar"],
            "recommended_size_pct": 50,
            "risks": [],
            "summary": "Sin evaluación",
            "error": str(e),
        }


# ── Formatear para Telegram ───────────────────────────────────────────────────

def format_evaluation_for_telegram(evaluation: dict, max_trade_usdc: float) -> str:
    """
    Genera el bloque de texto de evaluación para incluir en el mensaje de Telegram.
    """
    score    = evaluation.get("confidence", 0)
    verdict  = evaluation.get("verdict", "SKIP")
    reasoning= evaluation.get("reasoning", [])
    risks    = evaluation.get("risks", [])
    size_pct = evaluation.get("recommended_size_pct", 50)
    summary  = evaluation.get("summary", "")

    # Colores por score
    if score >= 8:
        score_emoji = "🟢"
    elif score >= 6:
        score_emoji = "🟡"
    elif score >= 5:
        score_emoji = "🟠"
    else:
        score_emoji = "🔴"

    verdict_line = "✅ COPIAR" if verdict == "COPY" else "⛔ SALTAR"
    rec_size     = round(max_trade_usdc * size_pct / 100, 2)

    lines = [
        "",
        f"{'─'*36}",
        f"{score_emoji} EVALUACIÓN IA  {score}/10  —  {verdict_line}",
    ]
    if summary:
        lines.append(f"   {summary}")
    lines.append("")

    for bullet in reasoning[:3]:
        lines.append(f"   • {bullet[:70]}")

    if rec_size != max_trade_usdc:
        lines.append(f"\n   💰 Tamaño recomendado: {rec_size} USDC ({size_pct}%)")

    if risks:
        lines.append("")
        for risk in risks[:2]:
            lines.append(f"   ⚠️  {risk[:65]}")

    return "\n".join(lines)


# ── Test standalone ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import asyncio

    async def _test():
        print("=" * 55)
        print("TRADE EVALUATOR — test standalone")
        print("=" * 55)

        market = {"question": "Will Bitcoin reach $100k by June 2026?"}
        trade  = {
            "side": "YES", "price": 0.43, "retorno": 1.33,
            "days_left": 45, "niche": "crypto", "size": 200,
        }
        stats  = {
            "name": "sovereign2013", "wallet": "0xee613b...",
            "win_rate": 0.792, "total_closed": 24,
            "profit_factor": 3.1, "avg_win": 56, "avg_loss": 19,
            "niche_win_rate": 0.75,
            "real_pnl_usdc": 1045, "total_copied": 5,
            "total_won": 4, "total_lost": 1,
        }
        history = [{"pnl_usdc": 45}, {"pnl_usdc": -18}, {"pnl_usdc": 62}]

        print("\nEvaluando trade de ejemplo...")
        result = await evaluate_trade(market, trade, stats, history)

        print(f"\nConfidence : {result['confidence']}/10")
        print(f"Verdict    : {result['verdict']}")
        print(f"Summary    : {result['summary']}")
        print(f"Reasoning  :")
        for b in result["reasoning"]:
            print(f"  • {b}")
        print(f"Risks      :")
        for r in result["risks"]:
            print(f"  ⚠ {r}")
        print(f"Rec. size  : {result['recommended_size_pct']}%")
        print()
        print("── Formato Telegram ──────────────────────────────────")
        print(format_evaluation_for_telegram(result, 10.0))

    asyncio.run(_test())
