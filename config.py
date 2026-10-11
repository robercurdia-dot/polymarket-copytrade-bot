import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env", override=True)

# Anthropic
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# Polymarket
POLY_API_KEY = os.getenv("POLY_API_KEY")
POLY_API_SECRET = os.getenv("POLY_API_SECRET")
POLY_API_PASSPHRASE = os.getenv("POLY_API_PASSPHRASE")
POLY_WALLET_ADDRESS = os.getenv("POLY_WALLET_ADDRESS")  # Proxy wallet — usar con Data API y posiciones
POLY_EOA_ADDRESS = os.getenv("POLY_EOA_ADDRESS")        # EOA (signer) — la que firma con POLY_PRIVATE_KEY
POLY_PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY")

# Alchemy
ALCHEMY_API_KEY = os.getenv("ALCHEMY_API_KEY")
POLYGON_RPC = f"https://polygon-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}"

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Configuración del bot
MAX_TRADE_SIZE_USDC = float(os.getenv("MAX_TRADE_SIZE_USDC", 5))
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", 10))
MIN_INSIDER_SCORE = float(os.getenv("MIN_INSIDER_SCORE", 0.75))
MIN_CONFIDENCE = float(os.getenv("MIN_CONFIDENCE", 0.65))
PAPER_TRADING = os.getenv("PAPER_TRADING", "true").lower() == "true"

# Real Trading (Fase 3.0 — kill switch independiente)
# Ambas flags deben estar configuradas explícitamente para operar real:
#   PAPER_TRADING=false AND REAL_TRADING_ENABLED=true → ejecuta REAL
#   cualquier otra combinación que NO sea esa → paper o blocked
REAL_TRADING_ENABLED = os.getenv("REAL_TRADING_ENABLED", "false").lower() == "true"

# Fase 3.1 — Capa agente→real. Qué agente del experimento decide trading real.
# Valores permitidos: "A", "B", "C", "NONE". Default "NONE" (seguro).
REAL_TRADING_AGENT = os.getenv("REAL_TRADING_AGENT", "NONE").upper()
_ALLOWED_AGENTS = {"A", "B", "C", "D", "DEF_COPY", "NONE"}
if REAL_TRADING_AGENT not in _ALLOWED_AGENTS:
    raise ValueError(
        f"REAL_TRADING_AGENT='{REAL_TRADING_AGENT}' inválido. "
        f"Permitidos: {sorted(_ALLOWED_AGENTS)}"
    )

# Slippage máximo permitido en executor real (Fase 3.1 — bajado de 0.15 a 0.05)
MAX_SLIPPAGE = float(os.getenv("MAX_SLIPPAGE", "0.05"))

# Firma de órdenes CLOB.
# Con SDK v2 (Fase 3.3): signature_type=3 = POLY_1271 (deposit wallet).
# Valores legacy (SDK v1): 0=POLY_PROXY, 1=POLY_GNOSIS_SAFE, 2=EOA.
POLY_SIGNATURE_TYPE     = int(os.getenv("POLY_SIGNATURE_TYPE", "3"))
POLY_FUNDER_ADDRESS     = os.getenv("POLY_FUNDER_ADDRESS", "")
DEPOSIT_WALLET_ADDRESS  = os.getenv("DEPOSIT_WALLET_ADDRESS", "")

# Modo single-shot: nº máximo de trades reales por sesión (0 = sin límite).
REAL_TRADES_SESSION_LIMIT = int(os.getenv("REAL_TRADES_SESSION_LIMIT", "0"))

# Fase 3.1b — nichos excluidos del trading real (el router los descarta).
REAL_EXCLUDED_NICHES = [n.strip() for n in os.getenv("REAL_EXCLUDED_NICHES", "").split(",") if n.strip()]

# Whale tracker — detección de cambios en posiciones de traders copiados
WHALE_SELL_THRESHOLD = float(os.getenv("WHALE_SELL_THRESHOLD", 0.5))
# si last_size < initial_size * WHALE_SELL_THRESHOLD → ballena vendiendo
WHALE_REINFORCE_THRESHOLD = float(os.getenv("WHALE_REINFORCE_THRESHOLD", 1.2))
# si last_size > initial_size * WHALE_REINFORCE_THRESHOLD → ballena reforzando
WHALE_CHECK_INTERVAL_SEC = int(os.getenv("WHALE_CHECK_INTERVAL_SEC", 300))

# Paper trading automático (Fase 2.3 — validación pre-real, sin riesgo monetario)
# Cada señal que pasa los filtros del monitor abre 1 paper_trade simulado.
# NO afecta a executor real, manual_trades, ni my_wallet.
PAPER_TRADING_ENABLED    = bool(int(os.getenv("PAPER_TRADING_ENABLED", "1")))
PAPER_SIZE_USDC          = float(os.getenv("PAPER_SIZE_USDC", "100"))
PAPER_TP_MULTIPLIER      = float(os.getenv("PAPER_TP_MULTIPLIER", "1.40"))
PAPER_SL_MULTIPLIER      = float(os.getenv("PAPER_SL_MULTIPLIER", "0.65"))
PAPER_FEES_ESTIMATED     = float(os.getenv("PAPER_FEES_ESTIMATED", "0.30"))
PAPER_CHECK_INTERVAL_SEC = int(os.getenv("PAPER_CHECK_INTERVAL_SEC", "60"))
PAPER_TIMEOUT_DAYS       = int(os.getenv("PAPER_TIMEOUT_DAYS", "7"))

# Filtros gestionados por auto-tuner (vacíos por defecto; el tuner los puebla)
EXCLUDED_NICHES          = []           # list[str] — nichos a saltar en passes_user_preferences
EXCLUDED_ENTRY_ABOVE     = None         # float | None — saltar señales con price > X

# Auto-tuner (Fase 2.4) — sistema autocorrectivo
AUTO_TUNER_ENABLED       = bool(int(os.getenv("AUTO_TUNER_ENABLED", "1")))
AUTO_TUNER_DRY_RUN       = bool(int(os.getenv("AUTO_TUNER_DRY_RUN", "1")))   # 1 → solo logea propuestas
AUTO_TUNER_RUN_HOUR_UTC  = int(os.getenv("AUTO_TUNER_RUN_HOUR_UTC", "4"))    # ejecución diaria a las 04:00 UTC
AUTO_TUNER_DRY_RUN_DAYS  = int(os.getenv("AUTO_TUNER_DRY_RUN_DAYS", "7"))    # tras N días, considerar quitar dry-run
# Min-samples por parámetro
AUTO_TUNER_MIN_TP        = int(os.getenv("AUTO_TUNER_MIN_TP", "20"))
AUTO_TUNER_MIN_SL        = int(os.getenv("AUTO_TUNER_MIN_SL", "20"))
AUTO_TUNER_MIN_NICHE     = int(os.getenv("AUTO_TUNER_MIN_NICHE", "10"))
AUTO_TUNER_MIN_ENTRY     = int(os.getenv("AUTO_TUNER_MIN_ENTRY", "15"))
AUTO_TUNER_MIN_CONF      = int(os.getenv("AUTO_TUNER_MIN_CONF", "30"))
# Reversión
AUTO_TUNER_REVERT_DEGRADATION = float(os.getenv("AUTO_TUNER_REVERT_DEGRADATION", "0.10"))  # 10%
AUTO_TUNER_MIN_POST_SAMPLE    = int(os.getenv("AUTO_TUNER_MIN_POST_SAMPLE", "15"))
# Detección de oscilación
AUTO_TUNER_OSCILLATION_FREEZE_DAYS = int(os.getenv("AUTO_TUNER_OSCILLATION_FREEZE_DAYS", "30"))

# Risk Manager (Fase 2.5) — circuit breakers que protegen capital
RISK_MANAGER_ENABLED   = bool(int(os.getenv("RISK_MANAGER_ENABLED", "1")))
# Drawdown principal: si PnL acumulado <= -(TRADE_SIZE × DRAWDOWN_MULTIPLIER) → veto permanente.
# Aplica al tamaño de trade activo (paper o real). Para paper con $100 → para a -$1000.
DRAWDOWN_MULTIPLIER    = float(os.getenv("DRAWDOWN_MULTIPLIER", "10"))
MAX_API_ERRORS_HOUR    = int(os.getenv("MAX_API_ERRORS_HOUR", "10"))     # >N → veto 1h
MIN_WALLET_BALANCE_USDC = float(os.getenv("MIN_WALLET_BALANCE_USDC", "5"))  # < → veto permanente
# Flag explícito: el check LOW_BALANCE solo aplica cuando hay automatización real activa.
# La flag legacy PAPER_TRADING controla el executor manual de Telegram, NO el risk check.
# Activar a True manualmente al pasar a Fase 3a (executor real automático con $5/trade).
RISK_CHECK_LOW_BALANCE = bool(int(os.getenv("RISK_CHECK_LOW_BALANCE", "0")))

# ── Fase de escalado (Fase A — $5 → $7.5 → $10/trade) ─────────────────
# SCALE_PHASE:                 etiqueta informativa (A1, A2, A3, OFF)
# SCALE_PHASE_STARTED_AT:      ISO timestamp de inicio de la fase
# SCALE_24H_LOSS_THRESHOLD:    si PnL real (paper_trade=0) últimas 24h
#                              cae <= este valor → MANUAL_PAUSE auto.
# SCALE_TARGET_TRADES:         trades cerrados objetivo para promover.
SCALE_PHASE              = os.getenv("SCALE_PHASE", "OFF")
SCALE_PHASE_STARTED_AT   = os.getenv("SCALE_PHASE_STARTED_AT", "")
SCALE_24H_LOSS_THRESHOLD = float(os.getenv("SCALE_24H_LOSS_THRESHOLD", "-45"))
SCALE_TARGET_TRADES      = int(os.getenv("SCALE_TARGET_TRADES", "35"))

# Experimento multi-agente (Fase 2.6) — oculta secciones no relacionadas en dashboard.
EXPERIMENT_MODE                    = bool(int(os.getenv("EXPERIMENT_MODE", "1")))
EXPERIMENT_MIN_TRADES_TARGET       = int(os.getenv("EXPERIMENT_MIN_TRADES_TARGET", "150"))
EXPERIMENT_MIN_TRADES_HOLD_TARGET  = int(os.getenv("EXPERIMENT_MIN_TRADES_HOLD_TARGET", "50"))
EXPERIMENT_DURATION_DAYS_TARGET    = int(os.getenv("EXPERIMENT_DURATION_DAYS_TARGET", "3"))
EXPERIMENT_STARTED_AT              = os.getenv("EXPERIMENT_STARTED_AT", "")  # ISO; lo set'eará el bot al primer arranque post-deploy

# Insider detector
MIN_TRADES_TO_QUALIFY = 8
MIN_WIN_RATE = 0.70   # bajado 0.75 → 0.70 (2026-05-25, usuario)
MIN_TRADE_SIZE_TO_TRACK = 500

# The Graph - Polymarket subgraph
SUBGRAPH_URL = "https://data-api.polymarket.com"