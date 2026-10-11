"""
auto_tuner.py — Sistema autocorrectivo de parámetros (Fase 2.4)
=================================================================
Lee histórico de paper_trades y recomendaciones del behavior analyzer
y ajusta parámetros automáticamente para optimizar WR/PnL.

Filosofía:
  - Conservador por defecto. Pasos del 5-15%.
  - Cooldown estricto por parámetro → evita oscilación.
  - Audit trail completo en `tuner_actions`.
  - Reversión automática si el cambio degrada >10% pnl_per_trade_net.
  - Detección de oscilación → freeze 30 días si la dirección alterna >2 veces.
  - Bootstrap retroactivo desde my_wallet histórico al primer arranque.
  - DRY-RUN inicial 7 días: solo loguea propuestas, no aplica.
  - El risk manager (Fase 2.5) tiene poder de veto sobre el tuner.

Parámetros gestionados:
  PAPER_TP_MULTIPLIER, PAPER_SL_MULTIPLIER, PAPER_TIMEOUT_DAYS,
  MIN_CONFIDENCE, EXCLUDED_NICHES, EXCLUDED_ENTRY_ABOVE
"""

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import config
import database
import risk_manager


# ── Rangos válidos por parámetro (reglas duras) ─────────────────────
PARAM_BOUNDS = {
    "PAPER_TP_MULTIPLIER":  {"min": 1.20, "max": 1.80, "type": "float"},
    "PAPER_SL_MULTIPLIER":  {"min": 0.45, "max": 0.80, "type": "float"},
    "PAPER_TIMEOUT_DAYS":   {"min": 3,    "max": 14,   "type": "int"},
    "MIN_CONFIDENCE":       {"min": 0.55, "max": 0.80, "type": "float"},
    "EXCLUDED_NICHES":      {"type": "list"},
    "EXCLUDED_ENTRY_ABOVE": {"min": 0.55, "max": 0.95, "type": "float_or_none"},
}

# Cooldown por parámetro (días)
COOLDOWN_DAYS = {
    "PAPER_TP_MULTIPLIER":  7,
    "PAPER_SL_MULTIPLIER":  10,
    "PAPER_TIMEOUT_DAYS":   14,
    "MIN_CONFIDENCE":       14,
    "EXCLUDED_NICHES":      14,
    "EXCLUDED_ENTRY_ABOVE": 14,
}


# ─────────────────────────────────────────────────────────────────────
# 1. Estado y wrappers
# ─────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def _serialize(value):
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return ""
    return str(value)


def _deserialize(raw: str, ptype: str):
    if raw is None or raw == "":
        return None if ptype.startswith("float_or_none") else ([] if ptype == "list" else None)
    if ptype == "float" or ptype == "float_or_none":
        return float(raw)
    if ptype == "int":
        return int(raw)
    if ptype == "list":
        try: return json.loads(raw)
        except Exception: return []
    return raw


def _default_for(parameter: str):
    """Lee el default desde config.py (env-based)."""
    return getattr(config, parameter, None)


def init_tuner_state():
    """Asegura que tuner_state tiene fila para cada parámetro gestionado."""
    conn = database.get_connection()
    c = conn.cursor()
    for p in PARAM_BOUNDS.keys():
        default = _default_for(p)
        c.execute("SELECT parameter FROM tuner_state WHERE parameter = ?", (p,))
        if not c.fetchone():
            c.execute("""INSERT INTO tuner_state
                         (parameter, current_value, default_value, last_updated)
                         VALUES (?, ?, ?, ?)""",
                      (p, _serialize(default), _serialize(default), _now_iso()))
    conn.commit()
    conn.close()


def get_managed_params() -> dict:
    """Lee tuner_state y devuelve dict {parameter: current_value}."""
    init_tuner_state()
    conn = database.get_connection()
    c = conn.cursor()
    c.execute("SELECT parameter, current_value FROM tuner_state")
    rows = c.fetchall()
    conn.close()
    out = {}
    for p, raw in rows:
        ptype = PARAM_BOUNDS[p]["type"]
        out[p] = _deserialize(raw, ptype)
    return out


def get_param_meta(parameter: str) -> dict:
    conn = database.get_connection()
    c = conn.cursor()
    c.execute("""SELECT current_value, default_value, last_updated, cooldown_until,
                        n_adjustments, frozen_until, frozen_reason
                 FROM tuner_state WHERE parameter = ?""", (parameter,))
    row = c.fetchone()
    conn.close()
    if not row:
        return {}
    ptype = PARAM_BOUNDS[parameter]["type"]
    return {
        "current":        _deserialize(row[0], ptype),
        "default":        _deserialize(row[1], ptype),
        "last_updated":   row[2],
        "cooldown_until": row[3],
        "n_adjustments":  row[4] or 0,
        "frozen_until":   row[5],
        "frozen_reason":  row[6],
    }


def apply_runtime_overrides():
    """Mutable los attrs de config con valores actuales del tuner_state.
    Llamar al arrancar el bot y tras cada aplicación de tuner."""
    params = get_managed_params()
    for p, v in params.items():
        if v is None and PARAM_BOUNDS[p]["type"] == "float_or_none":
            setattr(config, p, None)
        else:
            setattr(config, p, v)


# ─────────────────────────────────────────────────────────────────────
# 2. Condiciones para correr
# ─────────────────────────────────────────────────────────────────────

def _is_frozen(parameter: str) -> tuple[bool, str | None]:
    meta = get_param_meta(parameter)
    fu = meta.get("frozen_until")
    if not fu:
        return False, None
    try:
        if datetime.fromisoformat(fu) > datetime.utcnow():
            return True, meta.get("frozen_reason")
    except Exception:
        pass
    return False, None


def _is_in_cooldown(parameter: str) -> bool:
    meta = get_param_meta(parameter)
    cu = meta.get("cooldown_until")
    if not cu:
        return False
    try:
        return datetime.fromisoformat(cu) > datetime.utcnow()
    except Exception:
        return False


def should_run_now() -> tuple[bool, str | None]:
    if not config.AUTO_TUNER_ENABLED:
        return False, "AUTO_TUNER_ENABLED=False"
    veto, reason, _ = risk_manager.has_veto()
    if veto:
        return False, f"risk_manager VETO ({reason})"
    return True, None


# ─────────────────────────────────────────────────────────────────────
# 3. Recolectar métricas de paper_trades
# ─────────────────────────────────────────────────────────────────────

def _paper_metrics_for_evaluation(agent_id: str | None = None) -> dict:
    """Devuelve métricas agregadas necesarias para los evaluadores.
    Si agent_id se especifica → filtra a ese agente. Si None → todos
    los activos no-legacy (modo agregado, deprecado en Fase 2.6 pero
    se mantiene para compat)."""
    conn = database.get_connection()
    c = conn.cursor()
    if agent_id:
        c.execute("""SELECT id, status, entry_at, exit_at, entry_price, exit_price,
                            realized_pnl_net, market_question, niche
                     FROM paper_trades
                     WHERE status IN ('tp_hit','sl_hit','resolved_win','resolved_loss','disputed','timeout')
                       AND agent_id = ?""", (agent_id,))
    else:
        c.execute("""SELECT id, status, entry_at, exit_at, entry_price, exit_price,
                            realized_pnl_net, market_question, niche
                     FROM paper_trades
                     WHERE status IN ('tp_hit','sl_hit','resolved_win','resolved_loss','disputed','timeout')
                       AND agent_id NOT IN ('A_legacy')""")
    rows = c.fetchall()
    conn.close()

    closed = []
    for r in rows:
        h = None
        if r[2] and r[3]:
            try:
                h = (datetime.fromisoformat(r[3]) - datetime.fromisoformat(r[2])).total_seconds() / 3600
            except Exception:
                pass
        closed.append({
            "id": r[0], "status": r[1],
            "hours": h, "pnl_net": r[6] or 0,
            "niche": r[8] or "other",
        })
    return {
        "closed":  closed,
        "n":       len(closed),
        "tp_hit":  [c for c in closed if c["status"] == "tp_hit"],
        "sl_hit":  [c for c in closed if c["status"] == "sl_hit"],
        "resolved":[c for c in closed if c["status"] in ("resolved_win","resolved_loss","disputed")],
        "timeout": [c for c in closed if c["status"] == "timeout"],
    }


def _pct(part, total):
    return round(100.0 * part / total, 1) if total else 0


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 2) if xs else None


# ─────────────────────────────────────────────────────────────────────
# 4. Evaluadores por parámetro
# ─────────────────────────────────────────────────────────────────────

def evaluate_tp_multiplier(agent_id: str | None = None) -> dict | None:
    init_tuner_state()
    if _is_in_cooldown("PAPER_TP_MULTIPLIER"): return None
    frozen, reason = _is_frozen("PAPER_TP_MULTIPLIER")
    if frozen: return None
    pm = _paper_metrics_for_evaluation(agent_id=agent_id)
    if pm["n"] < config.AUTO_TUNER_MIN_TP: return None

    tp_pct = _pct(len(pm["tp_hit"]), pm["n"])
    sl_pct = _pct(len(pm["sl_hit"]), pm["n"])
    median_tp_h = _avg([t["hours"] for t in pm["tp_hit"]])

    current = float(get_param_meta("PAPER_TP_MULTIPLIER")["current"])

    if tp_pct >= 60 and (median_tp_h or 99) < 4:
        new = round(min(current * 1.10, PARAM_BOUNDS["PAPER_TP_MULTIPLIER"]["max"]), 4)
        return {
            "parameter":  "PAPER_TP_MULTIPLIER",
            "before":     current,
            "after":      new,
            "direction":  "increase",
            "hypothesis": f"TP_hit en {tp_pct}% en <{median_tp_h}h: profit en la mesa, subir 10%.",
            "evidence":   {"sample": pm["n"], "tp_pct": tp_pct, "sl_pct": sl_pct,
                          "median_time_to_tp_h": median_tp_h},
            "confidence": "high" if pm["n"] >= 30 else "medium",
        }
    if tp_pct <= 20 and sl_pct >= 40:
        new = round(max(current * 0.95, PARAM_BOUNDS["PAPER_TP_MULTIPLIER"]["min"]), 4)
        return {
            "parameter":  "PAPER_TP_MULTIPLIER",
            "before":     current,
            "after":      new,
            "direction":  "decrease",
            "hypothesis": f"TP_hit solo {tp_pct}% pero SL_hit {sl_pct}%: TP demasiado lejano.",
            "evidence":   {"sample": pm["n"], "tp_pct": tp_pct, "sl_pct": sl_pct},
            "confidence": "high" if pm["n"] >= 30 else "medium",
        }
    return None


def evaluate_sl_multiplier(agent_id: str | None = None) -> dict | None:
    init_tuner_state()
    if _is_in_cooldown("PAPER_SL_MULTIPLIER"): return None
    frozen, _ = _is_frozen("PAPER_SL_MULTIPLIER")
    if frozen: return None
    pm = _paper_metrics_for_evaluation(agent_id=agent_id)
    if pm["n"] < config.AUTO_TUNER_MIN_SL: return None

    sl_pct = _pct(len(pm["sl_hit"]), pm["n"])
    median_sl_h = _avg([t["hours"] for t in pm["sl_hit"]])
    current = float(get_param_meta("PAPER_SL_MULTIPLIER")["current"])

    if sl_pct >= 45 and (median_sl_h or 99) < 3:
        # Ampliar SL = bajar el multiplier (más laxo). Ojo: SL_MULT < 1.
        new = round(max(current * 0.92, PARAM_BOUNDS["PAPER_SL_MULTIPLIER"]["min"]), 4)
        return {
            "parameter": "PAPER_SL_MULTIPLIER",
            "before":    current,
            "after":     new,
            "direction": "decrease",
            "hypothesis": f"SL premature: {sl_pct}% stops en <{median_sl_h}h. Ampliar SL.",
            "evidence":  {"sample": pm["n"], "sl_pct": sl_pct, "median_time_to_sl_h": median_sl_h},
            "confidence": "high" if pm["n"] >= 30 else "medium",
        }
    if sl_pct <= 5 and pm["n"] >= 30:
        new = round(min(current * 1.05, PARAM_BOUNDS["PAPER_SL_MULTIPLIER"]["max"]), 4)
        return {
            "parameter": "PAPER_SL_MULTIPLIER",
            "before":    current,
            "after":     new,
            "direction": "increase",
            "hypothesis": f"SL inactivo: {sl_pct}% en {pm['n']} trades. Subir SL multiplier (cortar antes).",
            "evidence":  {"sample": pm["n"], "sl_pct": sl_pct},
            "confidence": "medium",
        }
    return None


def evaluate_timeout_days() -> dict | None:
    init_tuner_state()
    if _is_in_cooldown("PAPER_TIMEOUT_DAYS"): return None
    frozen, _ = _is_frozen("PAPER_TIMEOUT_DAYS")
    if frozen: return None
    pm = _paper_metrics_for_evaluation()
    if pm["n"] < config.AUTO_TUNER_MIN_TP: return None

    to_pct = _pct(len(pm["timeout"]), pm["n"])
    avg_to = _avg([t["hours"] for t in pm["timeout"]])
    current = int(get_param_meta("PAPER_TIMEOUT_DAYS")["current"])

    if to_pct >= 30 and avg_to:
        new = max(PARAM_BOUNDS["PAPER_TIMEOUT_DAYS"]["min"], current - 1)
        if new == current: return None
        return {
            "parameter": "PAPER_TIMEOUT_DAYS",
            "before":    current,
            "after":     new,
            "direction": "decrease",
            "hypothesis": f"timeout_pct {to_pct}% — bajar 1 día para cerrar zombies antes.",
            "evidence":  {"sample": pm["n"], "timeout_pct": to_pct, "avg_to_h": avg_to},
            "confidence": "medium",
        }
    return None


def evaluate_min_confidence() -> dict | None:
    init_tuner_state()
    if _is_in_cooldown("MIN_CONFIDENCE"): return None
    frozen, _ = _is_frozen("MIN_CONFIDENCE")
    if frozen: return None

    # Cruzar paper_trades con detected_trades.confidence_score
    conn = database.get_connection()
    c = conn.cursor()
    c.execute("""SELECT dt.confidence_score, pt.status
                 FROM paper_trades pt
                 JOIN detected_trades dt ON dt.id = pt.detected_trade_id
                 WHERE pt.status IN ('tp_hit','sl_hit','resolved_win','resolved_loss','timeout')""")
    rows = c.fetchall()
    conn.close()
    if len(rows) < config.AUTO_TUNER_MIN_CONF: return None

    bands = [(0.55, 0.65), (0.65, 0.75), (0.75, 0.85), (0.85, 1.01)]
    band_stats = []
    for lo, hi in bands:
        b = [r for r in rows if r[0] is not None and lo <= r[0] < hi]
        if not b: continue
        wins = sum(1 for r in b if r[1] in ("tp_hit","resolved_win"))
        band_stats.append({
            "lo": lo, "hi": hi, "n": len(b),
            "wr": round(wins / len(b), 4),
        })

    # Si banda más baja contigua tiene wr<35% sobre ≥10 trades, subir min
    current = float(get_param_meta("MIN_CONFIDENCE")["current"])
    for bs in band_stats:
        if bs["lo"] <= current and bs["wr"] < 0.35 and bs["n"] >= 10:
            new = round(bs["hi"], 4)
            new = min(new, PARAM_BOUNDS["MIN_CONFIDENCE"]["max"])
            if new <= current: continue
            return {
                "parameter": "MIN_CONFIDENCE",
                "before":    current,
                "after":     new,
                "direction": "increase",
                "hypothesis": f"Banda [{bs['lo']:.2f}-{bs['hi']:.2f}] tiene WR {bs['wr']*100:.0f}% sobre {bs['n']} trades. Subir min.",
                "evidence":  {"sample": len(rows), "band_stats": band_stats},
                "confidence": "high" if bs["n"] >= 20 else "medium",
            }
    return None


def evaluate_exclude_niches() -> list[dict]:
    """
    Cruza paper_trades por nicho con recomendaciones del behavior_analyzer.
    Solo aplica si AMBOS coinciden con confidence>=medium.
    """
    init_tuner_state()
    if _is_in_cooldown("EXCLUDED_NICHES"): return []
    frozen, _ = _is_frozen("EXCLUDED_NICHES")
    if frozen: return []

    pm = _paper_metrics_for_evaluation()
    if pm["n"] < config.AUTO_TUNER_MIN_NICHE: return []

    # Stats por nicho en paper
    by_niche = {}
    for c in pm["closed"]:
        n = c["niche"] or "other"
        if n not in by_niche: by_niche[n] = {"n":0, "wins":0, "pnl":0}
        by_niche[n]["n"] += 1
        if c["status"] in ("tp_hit","resolved_win"): by_niche[n]["wins"] += 1
        by_niche[n]["pnl"] += c["pnl_net"]

    # Recomendaciones del behavior analyzer
    try:
        import my_behavior_analyzer
        snap = my_behavior_analyzer.get_latest_snapshot() or {}
        behavior_excludes = {
            r["params"].get("niche"): r
            for r in (snap.get("recommendations_for_phase3") or [])
            if r.get("rule_type") == "exclude_niche"
        }
    except Exception:
        behavior_excludes = {}

    current_excluded = get_param_meta("EXCLUDED_NICHES")["current"] or []
    proposals = []
    for niche, st in by_niche.items():
        if niche in current_excluded: continue
        wr = st["wins"] / st["n"] if st["n"] else 0
        if st["n"] >= config.AUTO_TUNER_MIN_NICHE and wr < 0.30 and st["pnl"] < 0:
            if niche in behavior_excludes:
                new_list = list(current_excluded) + [niche]
                proposals.append({
                    "parameter": "EXCLUDED_NICHES",
                    "before":    current_excluded,
                    "after":     new_list,
                    "direction": "add",
                    "hypothesis": f"Paper: '{niche}' WR {wr*100:.0f}% sobre {st['n']} trades, "
                                  f"PnL ${st['pnl']:.2f}. Behavior analyzer también lo excluye.",
                    "evidence":  {"sample": st["n"], "wr": wr, "pnl": round(st["pnl"], 2),
                                 "behavior_evidence": behavior_excludes[niche].get("evidence")},
                    "confidence": "high",
                })
    return proposals


def evaluate_exclude_entry_above() -> dict | None:
    init_tuner_state()
    if _is_in_cooldown("EXCLUDED_ENTRY_ABOVE"): return None
    frozen, _ = _is_frozen("EXCLUDED_ENTRY_ABOVE")
    if frozen: return None

    conn = database.get_connection()
    c = conn.cursor()
    c.execute("""SELECT entry_price, status FROM paper_trades
                 WHERE status IN ('tp_hit','sl_hit','resolved_win','resolved_loss','timeout')""")
    rows = c.fetchall()
    conn.close()
    if len(rows) < config.AUTO_TUNER_MIN_ENTRY: return None

    bands = [(0.50, 0.70), (0.70, 0.85), (0.85, 1.01)]
    for lo, hi in bands:
        b = [r for r in rows if lo <= r[0] < hi]
        if len(b) < config.AUTO_TUNER_MIN_ENTRY: continue
        wins = sum(1 for r in b if r[1] in ("tp_hit","resolved_win"))
        wr = wins / len(b)
        current = get_param_meta("EXCLUDED_ENTRY_ABOVE")["current"]
        if wr < 0.25 and (current is None or current > lo):
            return {
                "parameter": "EXCLUDED_ENTRY_ABOVE",
                "before":    current,
                "after":     lo,
                "direction": "set",
                "hypothesis": f"Banda [{lo:.2f}-{hi:.2f}] WR {wr*100:.0f}% en {len(b)} trades.",
                "evidence":  {"sample": len(b), "wr": round(wr, 4), "band": [lo, hi]},
                "confidence": "high" if len(b) >= 25 else "medium",
            }
    return None


# ─────────────────────────────────────────────────────────────────────
# 5. Detección de oscilación
# ─────────────────────────────────────────────────────────────────────

def detect_oscillation(parameter: str) -> bool:
    """
    Si en las últimas 4 acciones aplicadas del mismo parámetro la dirección
    alterna >2 veces, congelar parámetro 30 días.
    """
    conn = database.get_connection()
    c = conn.cursor()
    c.execute("""SELECT direction FROM tuner_actions
                 WHERE parameter = ? AND applied = 1 AND direction IN ('increase','decrease')
                 ORDER BY action_at DESC LIMIT 4""", (parameter,))
    dirs = [r[0] for r in c.fetchall()]
    conn.close()
    if len(dirs) < 4:
        return False
    flips = sum(1 for i in range(len(dirs) - 1) if dirs[i] != dirs[i+1])
    if flips > 2:
        until = (datetime.utcnow() + timedelta(days=config.AUTO_TUNER_OSCILLATION_FREEZE_DAYS)).isoformat()
        conn = database.get_connection()
        c = conn.cursor()
        c.execute("""UPDATE tuner_state SET frozen_until=?, frozen_reason=?
                     WHERE parameter=?""",
                  (until, "oscillation_detected", parameter))
        conn.commit()
        conn.close()
        print(f"[TUNER] ❄️  {parameter} CONGELADO 30d (oscillation_detected, flips={flips})")
        return True
    return False


# ─────────────────────────────────────────────────────────────────────
# 6. Aplicación / persistencia
# ─────────────────────────────────────────────────────────────────────

def _persist_action(prop: dict, applied: bool, dry_run: bool, metric_before: float | None) -> int:
    cooldown = (datetime.utcnow() + timedelta(days=COOLDOWN_DAYS.get(prop["parameter"], 7))).isoformat()
    conn = database.get_connection()
    c = conn.cursor()
    c.execute("""INSERT INTO tuner_actions
        (action_at, parameter, before_value, after_value, direction,
         hypothesis, evidence_json, confidence, cooldown_until,
         applied, dry_run, metric_before)
        VALUES (?,?,?,?, ?,?,?,?, ?, ?,?,?)""",
        (_now_iso(), prop["parameter"],
         _serialize(prop["before"]), _serialize(prop["after"]),
         prop["direction"], prop["hypothesis"],
         json.dumps(prop["evidence"], ensure_ascii=False, default=str),
         prop["confidence"], cooldown,
         1 if applied else 0, 1 if dry_run else 0,
         metric_before))
    aid = c.lastrowid
    if applied:
        c.execute("""UPDATE tuner_state
                     SET current_value=?, last_updated=?, cooldown_until=?,
                         n_adjustments = COALESCE(n_adjustments, 0) + 1
                     WHERE parameter=?""",
                  (_serialize(prop["after"]), _now_iso(), cooldown, prop["parameter"]))
    conn.commit()
    conn.close()
    return aid


def _current_pnl_per_trade_net() -> float | None:
    conn = database.get_connection()
    c = conn.cursor()
    c.execute("""SELECT realized_pnl_net FROM paper_trades
                 WHERE status IN ('tp_hit','sl_hit','resolved_win','resolved_loss','timeout')""")
    rows = [r[0] or 0 for r in c.fetchall()]
    conn.close()
    return round(sum(rows) / len(rows), 4) if rows else None


def apply_proposal(prop: dict, dry_run: bool) -> int:
    metric_before = _current_pnl_per_trade_net()
    if dry_run:
        aid = _persist_action(prop, applied=False, dry_run=True, metric_before=metric_before)
        print(f"[TUNER:DRY] #{aid} {prop['parameter']}: {prop['before']} → {prop['after']} "
              f"({prop['direction']}, conf={prop['confidence']}) | {prop['hypothesis']}")
        return aid
    # Aplicar real
    aid = _persist_action(prop, applied=True, dry_run=False, metric_before=metric_before)
    apply_runtime_overrides()  # mutar config.* en runtime
    print(f"[TUNER] ✅ #{aid} {prop['parameter']}: {prop['before']} → {prop['after']} "
          f"({prop['direction']}, conf={prop['confidence']})")
    detect_oscillation(prop["parameter"])
    return aid


# ─────────────────────────────────────────────────────────────────────
# 7. Validación / reversión
# ─────────────────────────────────────────────────────────────────────

def validate_recent_actions():
    """
    Para cada acción aplicada hace ≥7 días (sin validar):
      - Si pnl_per_trade_net empeoró >AUTO_TUNER_REVERT_DEGRADATION
        Y hay ≥AUTO_TUNER_MIN_POST_SAMPLE trades cerrados después → REVERTIR.
      - Si mejoró → marcar validated_at.
    """
    cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()
    conn = database.get_connection()
    c = conn.cursor()
    c.execute("""SELECT id, parameter, before_value, after_value, action_at, metric_before
                 FROM tuner_actions
                 WHERE applied=1 AND validated_at IS NULL AND reverted_at IS NULL
                   AND action_at <= ?""", (cutoff,))
    actions = c.fetchall()

    for aid, param, before_raw, after_raw, action_at, metric_before in actions:
        # Trades cerrados después de la acción
        c.execute("""SELECT realized_pnl_net FROM paper_trades
                     WHERE status IN ('tp_hit','sl_hit','resolved_win','resolved_loss','timeout')
                       AND COALESCE(exit_at, last_check_at) >= ?""",
                  (action_at,))
        post_rows = [r[0] or 0 for r in c.fetchall()]
        if len(post_rows) < config.AUTO_TUNER_MIN_POST_SAMPLE:
            continue
        metric_after = round(sum(post_rows) / len(post_rows), 4)

        # ¿degradación?
        if metric_before is not None and metric_after < metric_before:
            degradation = (metric_before - metric_after) / max(abs(metric_before), 0.01)
            if degradation > config.AUTO_TUNER_REVERT_DEGRADATION:
                # Revertir
                ptype = PARAM_BOUNDS[param]["type"]
                before_val = _deserialize(before_raw, ptype)
                c.execute("""UPDATE tuner_state SET current_value=?, last_updated=?
                             WHERE parameter=?""",
                          (_serialize(before_val), _now_iso(), param))
                c.execute("""UPDATE tuner_actions
                             SET reverted_at=?, revert_reason=?, metric_after=?
                             WHERE id=?""",
                          (_now_iso(),
                           f"degradation {degradation*100:.1f}% (>{config.AUTO_TUNER_REVERT_DEGRADATION*100:.0f}%) "
                           f"on {len(post_rows)} post-trades",
                           metric_after, aid))
                print(f"[TUNER] ⏪ REVERTED #{aid} {param}: {after_raw}→{before_raw} "
                      f"(metric_before={metric_before:.3f}, metric_after={metric_after:.3f})")
                continue

        # Validado (no degradó >X)
        c.execute("""UPDATE tuner_actions SET validated_at=?, metric_after=? WHERE id=?""",
                  (_now_iso(), metric_after, aid))
        print(f"[TUNER] ✓ validated #{aid} {param} (metric: {metric_before}→{metric_after})")

    conn.commit()
    conn.close()


# ─────────────────────────────────────────────────────────────────────
# 8. Bootstrap retroactivo (one-shot)
# ─────────────────────────────────────────────────────────────────────

def bootstrap_from_history(force: bool = False) -> list[int]:
    """
    Una sola vez al primer arranque. Analiza my_wallet.closed_positions
    (trades reales del usuario) y propone TP/SL óptimos retroactivos si
    difieren del default >5%.

    Marca de control: si ya existe alguna acción con direction='bootstrap',
    no vuelve a ejecutar (idempotente).
    """
    conn = database.get_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM tuner_actions WHERE direction='bootstrap'")
    if c.fetchone()[0] > 0 and not force:
        conn.close()
        return []
    conn.close()

    try:
        import my_wallet_tracker
        # Cargar TODAS las cerradas (sin truncar)
        open_raw = my_wallet_tracker._fetch_positions(status='OPEN')
        all_open = [my_wallet_tracker._parse_position(p, is_open=True) for p in open_raw]
        real_open, pending = [], []
        for p, raw in zip(all_open, open_raw):
            cp = float(raw.get("curPrice") or 0)
            pn = float(raw.get("percentPnl") or 0)
            if cp < 0.01 and pn < -90:
                pending.append(p)
            else:
                real_open.append(p)
        open_keys = {(p["condition_id"], (p["outcome"] or "").upper().strip()) for p in real_open}
        events = my_wallet_tracker._fetch_activity(limit_total=2000)
        closed = my_wallet_tracker._aggregate_closed(events, open_keys)
    except Exception as e:
        print(f"[TUNER] bootstrap error cargando histórico: {e}")
        return []

    # ROI medio en sell_profit = candidato natural para TP
    sp = [t for t in closed if t.get("close_type") == "sell_profit" and t.get("buy_usdc", 0) > 0]
    if not sp:
        return []
    avg_roi_sell_profit = sum(t["cash_pnl"] / t["buy_usdc"] for t in sp) / len(sp)

    # SL: pérdida media en sell_loss como % del coste — proxy de stop-loss razonable
    sl = [t for t in closed if t.get("close_type") == "sell_loss" and t.get("buy_usdc", 0) > 0]

    proposals_persisted = []
    default_tp = float(_default_for("PAPER_TP_MULTIPLIER"))
    default_sl = float(_default_for("PAPER_SL_MULTIPLIER"))

    # Propuesta TP retroactiva
    proposed_tp = round(min(max(1 + avg_roi_sell_profit, PARAM_BOUNDS["PAPER_TP_MULTIPLIER"]["min"]),
                            PARAM_BOUNDS["PAPER_TP_MULTIPLIER"]["max"]), 3)
    if abs(proposed_tp - default_tp) / default_tp > 0.05:
        prop = {
            "parameter": "PAPER_TP_MULTIPLIER",
            "before":    default_tp,
            "after":     proposed_tp,
            "direction": "bootstrap",
            "hypothesis": f"Retro: ROI medio sell_profit histórico = {avg_roi_sell_profit*100:.1f}% "
                          f"sobre {len(sp)} trades reales del usuario.",
            "evidence":  {"sample": len(sp), "avg_roi": round(avg_roi_sell_profit, 4)},
            "confidence": "medium",
        }
        aid = _persist_action(prop, applied=False, dry_run=True, metric_before=None)
        proposals_persisted.append(aid)
        print(f"[TUNER:BOOTSTRAP] #{aid} TP {default_tp} → {proposed_tp} (ROI sell_profit medio {avg_roi_sell_profit*100:.1f}%)")

    # Propuesta SL retroactiva
    if sl:
        avg_loss_pct = sum(abs(t["cash_pnl"]) / t["buy_usdc"] for t in sl) / len(sl)
        # SL multiplier = 1 - avg_loss_pct (interpretación: si el usuario corta a -X% de media,
        # ése es el SL natural). Pero NO pasar por debajo del bound.
        proposed_sl = round(max(1 - avg_loss_pct, PARAM_BOUNDS["PAPER_SL_MULTIPLIER"]["min"]), 3)
        proposed_sl = round(min(proposed_sl, PARAM_BOUNDS["PAPER_SL_MULTIPLIER"]["max"]), 3)
        if abs(proposed_sl - default_sl) / default_sl > 0.05:
            prop = {
                "parameter": "PAPER_SL_MULTIPLIER",
                "before":    default_sl,
                "after":     proposed_sl,
                "direction": "bootstrap",
                "hypothesis": f"Retro: pérdida media sell_loss = {avg_loss_pct*100:.1f}% "
                              f"sobre {len(sl)} trades reales.",
                "evidence":  {"sample": len(sl), "avg_loss_pct": round(avg_loss_pct, 4)},
                "confidence": "medium",
            }
            aid = _persist_action(prop, applied=False, dry_run=True, metric_before=None)
            proposals_persisted.append(aid)
            print(f"[TUNER:BOOTSTRAP] #{aid} SL {default_sl} → {proposed_sl} (loss medio {avg_loss_pct*100:.1f}%)")

    # Marcador de bootstrap completado (incluso si no hubo propuestas)
    if not proposals_persisted:
        conn = database.get_connection()
        c = conn.cursor()
        c.execute("""INSERT INTO tuner_actions
            (action_at, parameter, before_value, after_value, direction,
             hypothesis, evidence_json, confidence, applied, dry_run)
            VALUES (?,?,?,?, 'bootstrap', ?,?, 'low', 0, 1)""",
            (_now_iso(), "_BOOTSTRAP_MARKER", "—", "—",
             "Histórico examinado, no diferencias >5% del default.",
             json.dumps({"closed_count": len(closed)})))
        conn.commit()
        conn.close()

    return proposals_persisted


def _persist_proposal_per_agent(prop: dict, agent_id: str, dry_run: bool):
    """Variante de _persist_action que etiqueta con agent_id (Fase 2.6)."""
    cooldown = (datetime.utcnow() + timedelta(days=COOLDOWN_DAYS.get(prop["parameter"], 7))).isoformat()
    conn = database.get_connection()
    c = conn.cursor()
    c.execute("""INSERT INTO tuner_actions
        (action_at, parameter, before_value, after_value, direction,
         hypothesis, evidence_json, confidence, cooldown_until,
         applied, dry_run, metric_before, agent_id)
        VALUES (?,?,?,?, ?,?,?,?, ?, ?,?,?,?)""",
        (_now_iso(), prop["parameter"],
         _serialize(prop["before"]), _serialize(prop["after"]),
         prop["direction"], f"[agent={agent_id}] " + prop["hypothesis"],
         json.dumps(prop["evidence"], ensure_ascii=False, default=str),
         prop["confidence"], cooldown,
         0, 1 if dry_run else 0,
         _current_pnl_per_trade_net(), agent_id))
    aid = c.lastrowid
    conn.commit()
    conn.close()
    print(f"[TUNER:DRY][{agent_id}] #{aid} {prop['parameter']}: "
          f"{prop['before']} → {prop['after']} ({prop['direction']}, conf={prop['confidence']})")
    return aid


def run_one_cycle() -> dict:
    """Ejecuta una pasada completa. Devuelve resumen.
    En modo experimento (todos los agentes config_frozen=1) las propuestas
    se persisten POR AGENTE pero NUNCA se aplican. dry_run=1 forzado."""
    init_tuner_state()
    ok, reason = should_run_now()
    if not ok:
        print(f"[TUNER] skip cycle: {reason}")
        return {"skipped": reason}

    # Validar acciones antiguas (puede revertir)
    try:
        validate_recent_actions()
    except Exception as e:
        print(f"[TUNER] validate error: {e}")

    # ── Modo experimento Fase 2.6: estudio per-agente, dry-run forzado ──
    conn = database.get_connection()
    c = conn.cursor()
    c.execute("SELECT id, config_frozen FROM agents WHERE active=1 AND id != 'A_legacy'")
    agent_rows = c.fetchall()
    conn.close()
    all_frozen = agent_rows and all(r[1] == 1 for r in agent_rows)

    proposals = []
    if all_frozen:
        # Estudio per-agente, NO aplicar nada
        per_agent_proposals = 0
        for ag_id, _ in agent_rows:
            for fn in (evaluate_tp_multiplier, evaluate_sl_multiplier):
                try:
                    p = fn(agent_id=ag_id)
                    if p:
                        _persist_proposal_per_agent(p, ag_id, dry_run=True)
                        per_agent_proposals += 1
                except TypeError:
                    # evaluator antiguo sin firma agent_id → skip
                    pass
                except Exception as e:
                    print(f"[TUNER] eval error agent={ag_id} {fn.__name__}: {e}")
        return {"experiment_mode": True, "agents_studied": len(agent_rows),
                "proposals_persisted": per_agent_proposals,
                "applied": 0, "dry_run": True}

    # ── Modo normal (sin experimento) — comportamiento Fase 2.4 ──
    for fn in (evaluate_tp_multiplier, evaluate_sl_multiplier,
               evaluate_timeout_days, evaluate_min_confidence,
               evaluate_exclude_entry_above):
        try:
            p = fn()
            if p: proposals.append(p)
        except Exception as e:
            print(f"[TUNER] eval error en {fn.__name__}: {e}")
    try:
        proposals.extend(evaluate_exclude_niches())
    except Exception as e:
        print(f"[TUNER] eval error niches: {e}")

    dry_run = config.AUTO_TUNER_DRY_RUN
    applied_ids = []
    for p in proposals:
        try:
            aid = apply_proposal(p, dry_run=dry_run)
            applied_ids.append(aid)
        except Exception as e:
            print(f"[TUNER] apply error {p['parameter']}: {e}")

    summary = {
        "evaluated": 6,
        "proposed":  len(proposals),
        "applied":   0 if dry_run else len(applied_ids),
        "dry_run":   bool(dry_run),
        "actions":   applied_ids,
    }
    print(f"[TUNER] Cycle done · proposed={summary['proposed']} "
          f"applied={summary['applied']} dry_run={summary['dry_run']}")
    return summary


# ─────────────────────────────────────────────────────────────────────
# 10. Loop async — ejecución a hora fija (04:00 UTC)
# ─────────────────────────────────────────────────────────────────────

async def auto_tuner_loop():
    """Ejecuta diario a AUTO_TUNER_RUN_HOUR_UTC (04:00 por defecto)."""
    if not config.AUTO_TUNER_ENABLED:
        print("[TUNER] disabled (AUTO_TUNER_ENABLED=0)")
        return

    # Bootstrap retroactivo (idempotente: solo si no hay marcador previo)
    try:
        bootstrap_from_history()
    except Exception as e:
        print(f"[TUNER] bootstrap error: {e}")

    apply_runtime_overrides()
    print(f"[TUNER] Loop iniciado · run_hour_utc={config.AUTO_TUNER_RUN_HOUR_UTC} · "
          f"dry_run={config.AUTO_TUNER_DRY_RUN}")

    while True:
        # Calcular segundos hasta el próximo slot 04:00 UTC
        now = datetime.utcnow()
        next_run = now.replace(hour=config.AUTO_TUNER_RUN_HOUR_UTC,
                               minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        sleep_s = (next_run - now).total_seconds()
        await asyncio.sleep(sleep_s)
        try:
            run_one_cycle()
        except Exception as e:
            print(f"[TUNER] cycle error: {e}")


# ─────────────────────────────────────────────────────────────────────
# 11. Stats para dashboard
# ─────────────────────────────────────────────────────────────────────

def get_dashboard_state() -> dict:
    """Estado completo para data.tuner."""
    init_tuner_state()
    conn = database.get_connection()
    c = conn.cursor()
    c.execute("""SELECT parameter, current_value, default_value, last_updated,
                        cooldown_until, n_adjustments, frozen_until, frozen_reason
                 FROM tuner_state ORDER BY parameter""")
    state_rows = c.fetchall()
    c.execute("""SELECT id, action_at, parameter, before_value, after_value,
                        direction, hypothesis, evidence_json, confidence,
                        applied, dry_run, reverted_at, revert_reason,
                        metric_before, metric_after, validated_at
                 FROM tuner_actions ORDER BY action_at DESC LIMIT 20""")
    action_rows = c.fetchall()
    conn.close()

    params = []
    for r in state_rows:
        ptype = PARAM_BOUNDS[r[0]]["type"]
        params.append({
            "parameter":      r[0],
            "current":        _deserialize(r[1], ptype),
            "default":        _deserialize(r[2], ptype),
            "last_updated":   r[3],
            "cooldown_until": r[4],
            "n_adjustments":  r[5] or 0,
            "frozen_until":   r[6],
            "frozen_reason":  r[7],
            "is_modified":    r[1] != r[2],
        })

    actions = []
    for a in action_rows:
        actions.append({
            "id":            a[0],
            "action_at":     a[1],
            "parameter":     a[2],
            "before":        a[3],
            "after":         a[4],
            "direction":     a[5],
            "hypothesis":    a[6],
            "evidence":      json.loads(a[7] or "{}"),
            "confidence":    a[8],
            "applied":       bool(a[9]),
            "dry_run":       bool(a[10]),
            "reverted_at":   a[11],
            "revert_reason": a[12],
            "metric_before": a[13],
            "metric_after":  a[14],
            "validated_at":  a[15],
        })

    # Conteo de propuestas pendientes por agente (Fase 2.6)
    conn = database.get_connection()
    c = conn.cursor()
    c.execute("""SELECT COALESCE(agent_id, 'A'), COUNT(*) FROM tuner_actions
                 WHERE applied=0 AND reverted_at IS NULL AND validated_at IS NULL
                 GROUP BY agent_id""")
    pending_by_agent = dict(c.fetchall())
    # Última propuesta por agente
    c.execute("""SELECT t.id, t.action_at, t.parameter, t.before_value, t.after_value,
                        t.direction, t.hypothesis, t.confidence, COALESCE(t.agent_id, 'A')
                 FROM tuner_actions t
                 WHERE applied=0 AND reverted_at IS NULL
                 ORDER BY t.id DESC""")
    latest_by_agent = {}
    for r in c.fetchall():
        ag = r[8]
        if ag not in latest_by_agent:
            latest_by_agent[ag] = {
                "id": r[0], "action_at": r[1], "parameter": r[2],
                "before": r[3], "after": r[4], "direction": r[5],
                "hypothesis": r[6], "confidence": r[7],
            }
    conn.close()

    return {
        "enabled":  config.AUTO_TUNER_ENABLED,
        "dry_run":  config.AUTO_TUNER_DRY_RUN,
        "run_hour_utc": config.AUTO_TUNER_RUN_HOUR_UTC,
        "params":   params,
        "actions":  actions,
        "pending_by_agent": pending_by_agent,
        "latest_by_agent":  latest_by_agent,
        "risk_manager_veto": risk_manager.has_veto()[0],
    }


if __name__ == "__main__":
    print(json.dumps(get_dashboard_state(), indent=2, default=str))
