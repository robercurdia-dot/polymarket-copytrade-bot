"""
preferences_analyzer.py
------------------------
Lee user_feedback, calcula umbrales por nicho y exporta preferences.json.

Uso:
    python3 preferences_analyzer.py            # calcula + guarda + muestra sugerencias .env
    python3 preferences_analyzer.py --report   # solo muestra estadísticas detalladas

preferences.json tiene esta estructura:
{
  "nba":        {"min_confidence": 72, "min_retorno": 0.35, "max_days_left": 3,  "min_insider_score": 0.80},
  "crypto":     {...},
  ...
  "global":     {"min_confidence": 65, "min_retorno": 0.25, "max_days_left": 7,  "min_insider_score": 0.75},
  "meta":       {"total_feedback": 42, "generated_at": "2026-04-13T10:00:00"}
}
"""

import json
import sys
from datetime import datetime
from statistics import median, mean, quantiles
import database

# Mínimo de muestras copiadas para que el umbral sea fiable.
# Con menos muestras se usa el valor por defecto de config.
MIN_SAMPLES = 5

NICHES = ["esports", "nba", "nfl", "crypto", "politics", "geopolitics", "other"]

DEFAULTS = {
    "min_confidence":   65.0,
    "min_retorno":      0.20,
    "max_days_left":    30.0,
    "min_insider_score": 0.75,
}

PREFERENCES_FILE = "preferences.json"


def _percentile(data, pct):
    """Percentil simple sin dependencias externas."""
    if not data:
        return None
    s = sorted(data)
    idx = (pct / 100) * (len(s) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)


def compute_niche_thresholds(copied, ignored):
    """
    Dado el historial de trades copiados e ignorados de un nicho,
    devuelve umbrales sugeridos.

    Lógica:
      - min_confidence   → percentil 25 de confidence en copiados
      - min_retorno      → percentil 25 de retorno en copiados
      - max_days_left    → percentil 75 de days_left en copiados
      - min_insider_score→ percentil 25 de insider_score en copiados

    Si hay menos de MIN_SAMPLES copiados devuelve None para ese umbral
    (monitor.py usará el default de config).
    """
    if len(copied) < MIN_SAMPLES:
        return None

    confidences   = [r["confidence"]    for r in copied if r["confidence"]    is not None]
    retornos      = [r["retorno"]       for r in copied if r["retorno"]       is not None]
    days_lefts    = [r["days_left"]     for r in copied if r["days_left"]     is not None and r["days_left"] < 900]
    scores        = [r["insider_score"] for r in copied if r["insider_score"] is not None]

    result = {}
    result["min_confidence"]    = round(_percentile(confidences,  25) or DEFAULTS["min_confidence"],   1)
    result["min_retorno"]       = round(_percentile(retornos,      25) or DEFAULTS["min_retorno"],      3)
    result["max_days_left"]     = round(_percentile(days_lefts,    75) or DEFAULTS["max_days_left"],    1) if days_lefts else DEFAULTS["max_days_left"]
    result["min_insider_score"] = round(_percentile(scores,        25) or DEFAULTS["min_insider_score"],3)
    result["samples_copied"]    = len(copied)
    result["samples_ignored"]   = len(ignored)
    result["copy_rate"]         = round(len(copied) / (len(copied) + len(ignored)), 2) if (copied or ignored) else 0
    return result


def analyze():
    rows = database.get_all_feedback()
    if not rows:
        print("Sin datos en user_feedback todavía.")
        return None

    # Agrupar por nicho
    by_niche = {n: {"copied": [], "ignored": []} for n in NICHES}
    for r in rows:
        niche = r["niche"] if r["niche"] in NICHES else "other"
        bucket = "copied" if r["action"] == "copied" else "ignored"
        by_niche[niche][bucket].append(r)

    prefs = {}
    for niche in NICHES:
        copied  = by_niche[niche]["copied"]
        ignored = by_niche[niche]["ignored"]
        t = compute_niche_thresholds(copied, ignored)
        if t:
            prefs[niche] = t

    # Global: todos los copiados juntos
    all_copied  = [r for r in rows if r["action"] == "copied"]
    all_ignored = [r for r in rows if r["action"] == "ignored"]
    global_t = compute_niche_thresholds(all_copied, all_ignored)
    prefs["global"] = global_t or DEFAULTS.copy()
    prefs["meta"] = {
        "total_feedback": len(rows),
        "total_copied":   len(all_copied),
        "total_ignored":  len(all_ignored),
        "generated_at":   datetime.now().isoformat()
    }
    return prefs


def save_preferences(prefs):
    with open(PREFERENCES_FILE, "w") as f:
        json.dump(prefs, f, indent=2)
    print(f"Preferencias guardadas en {PREFERENCES_FILE}")


def print_report(prefs):
    print()
    print("=" * 65)
    print("PREFERENCIAS DEL USUARIO — " + prefs.get("meta", {}).get("generated_at", "")[:10])
    meta = prefs.get("meta", {})
    print(f"Total feedback: {meta.get('total_feedback', 0)}  |  "
          f"Copiados: {meta.get('total_copied', 0)}  |  "
          f"Ignorados: {meta.get('total_ignored', 0)}")
    print("=" * 65)

    for niche in NICHES + ["global"]:
        t = prefs.get(niche)
        if not t or niche == "global":
            continue
        print(f"\n[{niche.upper()}]  copy_rate={t.get('copy_rate', '?')}  "
              f"({t.get('samples_copied', 0)}C / {t.get('samples_ignored', 0)}I)")
        print(f"  min_confidence   = {t['min_confidence']}%")
        print(f"  min_retorno      = {t['min_retorno']}x")
        print(f"  max_days_left    = {t['max_days_left']} días")
        print(f"  min_insider_score= {t['min_insider_score']}")

    g = prefs.get("global", DEFAULTS)
    print(f"\n[GLOBAL]")
    print(f"  min_confidence   = {g.get('min_confidence', DEFAULTS['min_confidence'])}%")
    print(f"  min_retorno      = {g.get('min_retorno', DEFAULTS['min_retorno'])}x")
    print(f"  max_days_left    = {g.get('max_days_left', DEFAULTS['max_days_left'])} días")
    print(f"  min_insider_score= {g.get('min_insider_score', DEFAULTS['min_insider_score'])}")

    print()
    print("─" * 65)
    print("AJUSTES SUGERIDOS PARA .env  (basados en preferencias globales)")
    print("─" * 65)
    g = prefs.get("global", DEFAULTS)
    print(f"MIN_CONFIDENCE={round(g.get('min_confidence', DEFAULTS['min_confidence']) / 100, 2)}")
    print(f"# MIN_RETORNO={g.get('min_retorno', DEFAULTS['min_retorno'])}  "
          f"(aplicar en monitor.py manualmente)")
    print(f"MIN_INSIDER_SCORE={g.get('min_insider_score', DEFAULTS['min_insider_score'])}")
    print()


if __name__ == "__main__":
    report_only = "--report" in sys.argv
    prefs = analyze()
    if prefs:
        if not report_only:
            save_preferences(prefs)
        print_report(prefs)
    else:
        print("Ejecuta el bot un tiempo para acumular feedback antes de analizar.")
