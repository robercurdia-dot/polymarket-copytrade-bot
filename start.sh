#!/bin/bash
# ── Polymarket Insider Agent — launcher ──────────────────────────────────────
# Ejecutar desde VS Code terminal: ./start.sh
# Los logs aparecen en tiempo real en la terminal.

cd "$(dirname "$0")"

# Matar instancia previa si existe
if pgrep -f "python.*main.py" > /dev/null 2>&1; then
    echo "⏹  Deteniendo instancia anterior..."
    pkill -9 -f "python.*main.py"
    sleep 2
fi

echo "🚀 Arrancando Polymarket Insider Agent..."
echo ""

# Activar entorno virtual y lanzar en primer plano (logs visibles en terminal)
source venv/bin/activate
REAL_TRADING_ENABLED=false python3 -u main.py
