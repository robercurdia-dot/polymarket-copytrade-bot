#!/bin/bash
# ── Polymarket Insider Agent — launcher en background con logs persistentes ──
# Tras el incidente 2026-05-04 (logs truncados al reiniciar), los logs ahora
# se acumulan en logs/bot-YYYYMMDD.log (un fichero por día).
# Ejecutar: ./start_background.sh
# Ver logs: tail -f logs/bot-$(date +%Y%m%d).log

set -e
cd "$(dirname "$0")"
mkdir -p logs

if pgrep -f "python.*main.py" > /dev/null 2>&1; then
    echo "⏹  Deteniendo instancia anterior..."
    pkill -9 -f "python.*main.py"
    sleep 2
fi

LOGFILE="logs/bot-$(date +%Y%m%d).log"
echo "🚀 Arrancando bot · logs → $LOGFILE"
echo "──── arranque $(date -u +%FT%TZ) ────" >> "$LOGFILE"

# >> append, NUNCA > overwrite (regla post-incidente 2026-05-04)
# caffeinate -s: previene sleep de sistema y red (evita NameResolutionError nocturno)
nohup caffeinate -s bash -c 'source venv/bin/activate && python -u main.py' >> "$LOGFILE" 2>&1 &
disown
sleep 2
PID=$(pgrep -f "python.*main.py" | head -1)
echo "✅ PID $PID · tail -f $LOGFILE"
