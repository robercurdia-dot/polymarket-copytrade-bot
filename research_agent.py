import anthropic
import config
from datetime import datetime

client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

SPORTS_KEYWORDS = [
    "NBA", "NFL", "NHL", "MLB", "soccer", "football", "basketball", "tennis", "golf",
    "UFC", "MMA", "boxing", "Champions", "Premier", "Serie A", "Bundesliga", "LaLiga",
    "World Cup", "vs.", "O/U", "moneyline", "Spread",
    "Lakers", "Warriors", "Celtics", "Heat", "Knicks", "Bulls",
    "Real Madrid", "Barcelona", "Manchester", "LoL", "Dota", "esports",
    "Masters", "PGA",
    "Phillies", "Marlins", "Braves", "Orioles", "Diamondbacks", "ERA", "innings", "pitching",
    # Premier League clubs adicionales
    "Crystal Palace", "Everton", "Wolves", "Fulham", "Brentford",
    "Bournemouth", "Nottingham", "Leicester", "Southampton", "Ipswich",
    "West Ham", "Aston Villa", "Newcastle", "Brighton",
    # Patrón "Will X win on" → siempre deportes
    "win on",
]

POLITICAL_KEYWORDS = ["Iran", "Russia", "Ukraine", "Trump", "China", "Israel", "Gaza", "NATO", "nuclear", "ceasefire", "war", "sanctions", "coup", "election", "president", "congress", "Fed", "OPEC", "military", "treaty", "deal", "minister", "government", "Hormuz", "uranium"]

def is_sports(question):
    return any(kw.lower() in question.lower() for kw in SPORTS_KEYWORDS)

def is_political(question):
    return any(kw.lower() in question.lower() for kw in POLITICAL_KEYWORDS)

def get_time_left(end_date):
    if not end_date:
        return 999, None
    try:
        from datetime import timezone as _tz
        dt = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz.utc)
        else:
            dt = dt.astimezone(_tz.utc)
        diff = dt - datetime.now(_tz.utc)
        total_seconds = diff.total_seconds()
        if total_seconds <= 0:
            return 0, 0
        days    = int(total_seconds // 86400)
        hours   = int((total_seconds % 86400) // 3600)
        minutes = int((total_seconds % 3600) // 60)
        return days, hours, minutes
    except:
        return 999, None, None

def get_alert_level(adjusted_confidence, days_left, hours_left=None):
    if hours_left is not None and hours_left <= 1:
        return "URGENTE", "🚨"
    elif adjusted_confidence >= 80 and days_left <= 1:
        return "CRITICO", "🔴"
    elif adjusted_confidence >= 80 and days_left <= 7:
        return "ALTO", "🟠"
    elif adjusted_confidence >= 80:
        return "ALTO", "🟠"
    elif adjusted_confidence >= 65 and days_left <= 7:
        return "ALTO", "🟠"
    elif adjusted_confidence >= 65:
        return "MEDIO", "🟡"
    else:
        return "SEGUIMIENTO", "⚪"

def parse_research(text):
    result = {
        "confidence": 50,
        "stat": "",
        "noticia": "",
        "summary": "",
        "risk": "",
        "raw": text
    }
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("CONFIANZA:"):
            try:
                result["confidence"] = int(line.split(":")[1].strip())
            except:
                pass
        elif line.startswith("STAT:"):
            result["stat"] = line.split(":", 1)[1].strip()
        elif line.startswith("NOTICIA:"):
            result["noticia"] = line.split(":", 1)[1].strip()
        elif line.startswith("RESUMEN:"):
            result["summary"] = line.split(":", 1)[1].strip()
        elif line.startswith("RIESGO:"):
            result["risk"] = line.split(":", 1)[1].strip()
    return result