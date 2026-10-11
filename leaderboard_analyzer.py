import requests
from datetime import datetime, timedelta, timezone
import database
import config

DATA_URL = "https://data-api.polymarket.com"

POLITICAL_KEYWORDS = ["Trump", "Iran", "Russia", "Ukraine", "China", "election", "ceasefire", "NATO", "sanction", "war", "president", "congress", "senate", "nuclear", "tariff", "Putin", "Xi", "Netanyahu", "Gaza", "Israel", "OPEC", "coup", "resign", "impeach", "Fed", "military", "deal", "treaty", "vote", "minister", "government"]

SPORTS_KEYWORDS = [
    # Ligas y torneos
    "NBA", "NFL", "NHL", "MLB", "UFC", "MMA", "boxing",
    "Champions League", "Premier League", "Serie A", "Bundesliga", "LaLiga",
    "World Cup", "Olympics", "Grand Slam", "Wimbledon", "Roland Garros",
    "Super Bowl", "World Series", "Stanley Cup", "March Madness", "ATP", "WTA",
    # Términos genéricos
    "basketball", "soccer", "football", "tennis", "golf", "vs.", "O/U", "spread", "moneyline",
    # Equipos NBA
    "Lakers", "Warriors", "Celtics", "Heat", "Knicks", "Bucks", "Suns",
    "Nuggets", "Clippers", "76ers", "Raptors",
    # Equipos NFL
    "Chiefs", "Cowboys", "Patriots", "Eagles", "Rams", "Packers",
    "Bears", "Broncos", "49ers", "Ravens",
    # Equipos MLB
    "Mets", "Dodgers", "Yankees", "Red Sox", "Cubs", "Cardinals",
    "Braves", "Phillies", "Marlins", "Orioles", "Diamondbacks",
    "SF Giants", "NY Giants", "Padres", "Astros", "Rangers", "Guardians",
    "Tigers", "Royals", "Athletics", "Mariners", "Twins",
    "White Sox", "Blue Jays", "Rays", "Nationals", "Pirates",
    "Reds", "Rockies", "Angels", "Brewers",
    # Clubs de fútbol europeo
    "Bayern", "Real Madrid", "Barcelona", "Atletico", "Inter Milan", "Juventus",
    "Chelsea", "Arsenal", "Liverpool", "PSG", "Tottenham",
    "Man City", "Man United", "Dortmund", "AC Milan", "Napoli",
    "Porto", "Benfica", "Ajax", "Sevilla", "Valencia",
    # Premier League clubs adicionales
    "Crystal Palace", "Everton", "Wolves", "Fulham", "Brentford",
    "Bournemouth", "Nottingham", "Leicester", "Southampton", "Ipswich",
    "West Ham", "Aston Villa", "Newcastle", "Brighton",
    # Patrón "Will X win on" → siempre deportes
    "win on",
]

# ── Orden ESTRICTO: esports → sports → crypto → geopolitics → politics ───────
# ECONOMÍA es siempre el último recurso (niche=None en el código).
NICHE_KEYWORDS = {
    # 0º ENTERTAINMENT (TV shows) — primero para evitar colisión con "the boys"
    # en patrones tipo "Cowboys" (sports). Usa substrings con contexto.
    "entertainment": [
        "the boys", "squid game", "stranger things", "severance",
        "house of the dragon", "succession",
        'die in "',   # patrón "Will X die in 'Show'?"
    ],
    # 1º ESPORTS
    "esports": [
        "LoL", "Dota", "esports", "esport", "CS:GO", "CSGO", "Valorant",
        "Fortnite", "League of Legends", "Overwatch", "Starcraft", "Rainbow Six",
        # Añadidos post-análisis classifier (2026-05-16):
        "Counter-Strike", "Map Handicap", "Rocket League",
    ],
    # 2º DEPORTES (NBA + NFL + MLB + fútbol + tenis + golf + más)
    "sports": [
        # Ligas y torneos
        "NBA", "NFL", "NHL", "MLB", "UFC", "MMA", "boxing",
        "Champions League", "Premier League", "Serie A", "Bundesliga", "LaLiga",
        "World Cup", "Olympics", "Grand Slam", "Wimbledon", "Roland Garros",
        "Super Bowl", "World Series", "Stanley Cup", "March Madness", "ATP", "WTA",
        # Términos genéricos de deporte
        "basketball", "soccer", "football", "tennis", "golf", "O/U", "moneyline", "spread",
        # Equipos NBA
        "Lakers", "Warriors", "Celtics", "Heat", "Knicks", "Bucks", "Suns",
        "Nuggets", "Clippers", "76ers", "Raptors",
        # Equipos NFL
        "Chiefs", "Cowboys", "Patriots", "Eagles", "Rams", "Packers",
        "Bears", "Broncos", "49ers", "Ravens",
        # Equipos MLB
        "Mets", "Dodgers", "Yankees", "Red Sox", "Cubs", "Cardinals",
        "Braves", "Phillies", "Marlins", "Orioles", "Diamondbacks",
        "SF Giants", "NY Giants", "Padres", "Astros", "Rangers", "Guardians",
        "Tigers", "Royals", "Athletics", "Mariners", "Twins",
        "White Sox", "Blue Jays", "Rays", "Nationals", "Pirates",
        "Reds", "Rockies", "Angels", "Brewers",
        # Clubs de fútbol europeo
        "Bayern", "Real Madrid", "Barcelona", "Atletico", "Inter", "Juventus",
        "Chelsea", "Arsenal", "Liverpool", "PSG", "Tottenham",
        "Man City", "Man United", "Dortmund", "AC Milan", "Napoli",
        "Porto", "Benfica", "Ajax", "Sevilla", "Valencia",
        # Premier League clubs adicionales
        "Crystal Palace", "Everton", "Wolves", "Fulham", "Brentford",
        "Bournemouth", "Nottingham", "Leicester", "Southampton", "Ipswich",
        "West Ham", "Aston Villa", "Newcastle", "Brighton",
        # Patrón "Will X win on" → siempre deportes
        "win on",
        # Torneos tenis ATP/WTA (añadidos 2026-05-16) — usar prefijos con ":"
        # para los nombres genéricos de ciudad, evita colisión geo.
        "Internazionali", "BNL d'Italia", "Madrid Open", "Marrakech",
        "Wuxi:", "Bengaluru", "Jiujiang:", "Francavilla:", "Istanbul:",
    ],
    # 3º CRYPTO
    "crypto": [
        "Bitcoin", "BTC", "ETH", "Ethereum", "crypto", "XRP", "SOL",
        "DOGE", "altcoin", "Up or Down",
    ],
    # 4º GEOPOLÍTICA
    "geopolitics": [
        # 2026-05-23: añadidos términos que el clasificador previo perdía
        # ("Iranian", "uranium", "Hamas") permitiendo paper B abriera
        # trades en geopolítica.
        "Iran", "Iranian", "Tehran", "uranium", "enriched",
        "Russia", "Russian", "Ukraine", "China", "Chinese",
        "Israel", "Israeli", "Gaza", "Hamas", "Hezbollah", "Pyongyang", "Kremlin",
        "NATO", "nuclear", "ceasefire", "war", "sanctions", "coup", "military",
        "treaty", "OPEC", "Putin", "Xi", "Netanyahu", "Hormuz", "Taiwan",
    ],
    # 5º POLÍTICA
    "politics": [
        "Trump", "Biden", "Congress", "Senate", "election", "president",
        "Republican", "Democrat", "vote", "impeach", "tariff",
        "governor", "mayor", "minister", "government",
        # "Fed" eliminado — los mercados de tipos de interés son ECONOMÍA, no política
    ],
    # 6º ECONOMÍA ← se coge por fallback en get_question_niche() (niche=None)
}

def get_leaderboard(limit=200):
    all_traders = []
    offset = 0
    while offset < limit:
        try:
            r = requests.get(DATA_URL + "/v1/leaderboard?timePeriod=month&orderBy=PNL&limit=50&offset=" + str(offset) + "&category=overall", timeout=15)
            data = r.json()
            if not data:
                break
            all_traders.extend(data)
            offset += 50
        except Exception as e:
            print(f"  [leaderboard] error offset {offset}: {e}")
            break
    return all_traders[:limit]

# ── Configuración de filtros por familia de nicho ────────────────────────────
NICHE_FILTERS = {
    "sports": {
        "niches":          {"esports", "sports"},
        "min_closed":      15,
        "min_win_rate":    0.70,   # bajado 0.80 → 0.70 (2026-05-25, usuario)
        "min_pf":          1.5,
        "lookback_days":   14,
    },
    "other": {
        "niches":          set(),
        "min_closed":      15,
        "min_win_rate":    0.70,
        "min_pf":          1.5,
        "lookback_days":   14,
    },
    # politics/geopolitics/crypto ELIMINADOS 2026-05-25 (decisión usuario).
    # crypto: 0 trades históricos en cualquier capa — irrelevante operativamente.
    # politics/geopolitics: liquidez catastrófica al cerrar (uranium-Iran zombies).
}

# Niches a IGNORAR completamente en el análisis del leaderboard
IGNORED_PRIMARY_NICHES = {"politics", "geopolitics", "entertainment", "crypto"}

# Mapeo rápido nicho → familia
_NICHE_FAMILY = {}
for _fam, _cfg in NICHE_FILTERS.items():
    for _n in _cfg["niches"]:
        _NICHE_FAMILY[_n] = _fam

def get_niche_family(niche):
    """Devuelve la familia de filtros para un nicho dado."""
    return _NICHE_FAMILY.get(niche, "other")

def get_filters_for_niche(niche):
    """Devuelve el dict de filtros aplicable a un nicho."""
    return NICHE_FILTERS[get_niche_family(niche)]

# Constantes de fallback (se usan en monitor.py cuando no hay nicho claro)
# Bajadas 0.80 → 0.70 (2026-05-25, usuario) — captura más wallets.
MIN_CLOSED_POSITIONS = 15
MIN_PROFIT_FACTOR    = 1.5
MIN_WIN_RATE_WEEK    = 0.70
MIN_WIN_RATE_HIST    = 0.70

def _parse_dt(raw):
    """Parsea una fecha ISO y la convierte siempre a datetime aware (UTC)."""
    if not raw:
        return None
    try:
        s = str(raw).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

def _is_closed(p, now):
    """Devuelve True si la posición está cerrada (redeemable o endDate pasado)."""
    if bool(p.get("redeemable", False)):
        return True
    end_dt = _parse_dt(p.get("endDate") or p.get("endDateIso") or "")
    return end_dt is not None and end_dt < now

def _fetch_positions_unbiased(wallet: str, max_positions: int = 1000) -> list:
    """
    Obtiene posiciones SIN sesgo de selección.
    Combina el extremo superior (mejor PnL) y el inferior (peor PnL) para
    cubrir el máximo rango posible con las 500 + 500 peticiones permitidas.
    Desduplicamos por (conditionId, outcome, asset).
    """
    seen, result = set(), []

    for direction in ("desc", "asc"):
        try:
            r = requests.get(
                DATA_URL + "/positions?user=" + wallet
                + "&limit=500&sortBy=CASHPNL&sortDirection=" + direction,
                timeout=20,
            )
            if r.status_code != 200:
                continue
            raw = r.json()
            if not isinstance(raw, list):
                continue
        except Exception as e:
            print(f"  [positions_unbiased] error {wallet[:10]} {direction}: {e}")
            continue

        for p in raw:
            key = (p.get("conditionId", ""), p.get("outcome", ""), p.get("asset", ""))
            if key not in seen:
                seen.add(key)
                result.append(p)
            if len(result) >= max_positions:
                return result

    return result

def categorize_positions(positions):
    political = []
    sports = []
    other = []
    for p in positions:
        title = p.get("title", "").lower()
        if any(kw.lower() in title for kw in POLITICAL_KEYWORDS):
            political.append(p)
        elif any(kw.lower() in title for kw in SPORTS_KEYWORDS):
            sports.append(p)
        else:
            other.append(p)
    return political, sports, other

def categorize_by_niches(positions):
    """Clasifica posiciones en los 6 nichos. Cada posición se asigna al primer nicho que coincida."""
    niches = {n: [] for n in NICHE_KEYWORDS}
    for p in positions:
        title = p.get("title", "")
        for niche, keywords in NICHE_KEYWORDS.items():
            if any(kw.lower() in title.lower() for kw in keywords):
                niches[niche].append(p)
                break
    return niches

def calc_stats(positions):
    if not positions:
        return None
    total = len(positions)
    wins = sum(1 for p in positions if float(p.get("cashPnl", 0)) > 0)
    losses = sum(1 for p in positions if float(p.get("cashPnl", 0)) < 0)
    win_rate = wins / total if total > 0 else 0
    avg_win = sum(float(p.get("cashPnl", 0)) for p in positions if float(p.get("cashPnl", 0)) > 0) / wins if wins > 0 else 0
    avg_loss = abs(sum(float(p.get("cashPnl", 0)) for p in positions if float(p.get("cashPnl", 0)) < 0) / losses) if losses > 0 else 0
    profit_factor = avg_win / avg_loss if avg_loss > 0 else 0
    avg_size = sum(float(p.get("initialValue", p.get("size", 0))) for p in positions) / total
    return {
        "total": total, "wins": wins, "losses": losses,
        "win_rate": round(win_rate, 3),
        "avg_win": round(avg_win, 2), "avg_loss": round(avg_loss, 2),
        "profit_factor": round(profit_factor, 2),
        "avg_size": round(avg_size, 2)
    }

def calc_niche_score(stats):
    if not stats or stats["total"] < 2:
        return 0.0
    pf_factor = min(stats["profit_factor"] / 2, 1.0) if stats["profit_factor"] > 0 else 0.5
    return round(stats["win_rate"] * min(stats["total"] / 10, 1.0) * pf_factor, 3)

def analyze_trader(trader):
    wallet = trader.get("proxyWallet", "")

    # ── Paso 1: Obtener posiciones brutas desde la API ────────────────────
    try:
        raw = _fetch_positions_unbiased(wallet)
        if not raw:
            return None
    except Exception as e:
        print(f"  [positions] error {wallet[:10]}: {e}")
        return None

    now = datetime.now(timezone.utc)

    # ── Paso 2: Filtrar solo posiciones cerradas ───────────────────────────
    closed_all = []
    for p in raw:
        size = float(p.get("initialValue", p.get("size", 0)) or 0)
        if size <= 0:
            continue
        if _is_closed(p, now):
            closed_all.append(p)

    if len(closed_all) < 10:
        return None  # muestra insuficiente para detectar nicho

    # ── Paso 3: Detectar nicho primario del trader ─────────────────────────
    niche_buckets_raw = categorize_by_niches(closed_all)
    primary_niche = max(niche_buckets_raw, key=lambda n: len(niche_buckets_raw[n]), default=None)
    if not primary_niche or not niche_buckets_raw.get(primary_niche):
        primary_niche = None  # sin nicho claro → fallback "other"

    # 2026-05-25 user decision: ignorar wallets cuyo nicho dominante sea
    # politics/geopolitics/entertainment — no son operacionalmente útiles.
    if primary_niche in IGNORED_PRIMARY_NICHES:
        return None

    # ── Paso 4: Filtros específicos del nicho ─────────────────────────────
    f = get_filters_for_niche(primary_niche)

    if len(closed_all) < f["min_closed"]:
        return None

    cutoff = now - timedelta(days=f["lookback_days"])
    closed_week = []
    for p in closed_all:
        end_dt = _parse_dt(p.get("endDate") or p.get("endDateIso") or "")
        if end_dt is not None and end_dt >= cutoff:
            closed_week.append(p)

    # ── Stats históricas ───────────────────────────────────────────────────
    political, sports, other_pos = categorize_positions(closed_all)
    pol_stats = calc_stats(political)
    spo_stats = calc_stats(sports)

    # ── Criterios de insider con umbrales del nicho ────────────────────────
    # 2026-05-25 — eliminado path is_political_insider; politics/geopol no se
    # analizan. Solo se califica por sports stats.
    half_min = max(f["min_closed"] // 2, 5)
    is_sports_insider = (
        spo_stats is not None
        and spo_stats["win_rate"]      >= f["min_win_rate"]
        and spo_stats["profit_factor"] >= f["min_pf"]
        and spo_stats["total"]         >= half_min
    )

    if not is_sports_insider:
        return None

    category   = "sports"
    hist_stats = spo_stats

    # ── Filtro win_rate_week: si hay datos recientes, deben cumplir umbral ─
    week_stats = calc_stats(closed_week) if closed_week and len(closed_week) >= 5 else None
    if week_stats and week_stats["win_rate"] < f["min_win_rate"]:
        return None  # rendimiento reciente insuficiente

    # ── insider_score con normalización por nicho ──────────────────────────
    if week_stats:
        base_wr = week_stats["win_rate"]
        base_pf = week_stats["profit_factor"]
        base_n  = week_stats["total"]
    else:
        base_wr = hist_stats["win_rate"]
        base_pf = hist_stats["profit_factor"]
        base_n  = hist_stats["total"]

    # Fórmula: win_rate * volumen(max en min_closed del nicho) * profit_factor(max en 3x)
    insider_score = (
        base_wr
        * min(base_n / f["min_closed"], 1.0)
        * min(base_pf / 3, 1.0)
    )

    # ── Niche scores (posiciones recientes si hay, globales si no) ─────────
    bucket_source    = closed_week if closed_week else closed_all
    niche_buckets_sc = categorize_by_niches(bucket_source)
    niche_scores     = {}
    for niche, niche_positions in niche_buckets_sc.items():
        ns = calc_stats(niche_positions)
        if ns:
            niche_scores[niche] = {
                "win_rate":      ns["win_rate"],
                "profit_factor": ns["profit_factor"],
                "total_trades":  ns["total"],
                "avg_size":      ns["avg_size"],
                "niche_score":   calc_niche_score(ns)
            }

    return {
        "wallet":             wallet,
        "name":               trader.get("userName", "")[:30],
        "monthly_pnl":        round(float(trader.get("pnl", 0))),
        "category":           category,
        "primary_niche":      primary_niche,
        # cerradas históricas
        "win_rate":           hist_stats["win_rate"],
        "wins":               hist_stats["wins"],
        "losses":             hist_stats["losses"],
        "profit_factor":      hist_stats["profit_factor"],
        "total":              hist_stats["total"],
        "closed_total":       len(closed_all),
        # cerradas recientes (ventana del nicho)
        "closed_week":        week_stats["total"] if week_stats else 0,
        "win_rate_week":      week_stats["win_rate"] if week_stats else hist_stats["win_rate"],
        "profit_factor_week": week_stats["profit_factor"] if week_stats else hist_stats["profit_factor"],
        # score final
        "insider_score":      round(insider_score, 3),
        "niche_scores":       niche_scores
    }

def run_analysis(limit=200):
    print("Obteniendo top " + str(limit) + " traders...")
    traders = get_leaderboard(limit)
    print("Analizando " + str(len(traders)) + " traders...")
    print()
    insiders = []
    # Acumulamos niche data para calcular elite_traders al final
    niche_candidates = {n: [] for n in NICHE_KEYWORDS}

    for i, t in enumerate(traders):
        print("  [" + str(i+1) + "/" + str(len(traders)) + "] " + t.get("userName", "")[:25] + "...", end="\r")
        result = analyze_trader(t)
        if result:
            insiders.append(result)
            # Threshold consistente con config.MIN_WIN_RATE (bajado 0.75→0.70 2026-05-25).
            # Si el WR real unbiased < MIN_WIN_RATE, forzar active=0.
            import config as _cfg
            real_wr = result["win_rate"]
            min_wr = _cfg.MIN_WIN_RATE
            active  = 1 if real_wr >= min_wr else 0
            if active == 0:
                print(f"  ↳ WR real {round(real_wr*100)}% < {round(min_wr*100)}% → active=0")
            database.save_wallet(result["wallet"], {
                "total_trades":   result["total"],
                "winning_trades": result["wins"],
                "win_rate":       result["win_rate"],
                "avg_entry_odds": 0.5,
                "insider_score":  result["insider_score"],
                "first_seen":     datetime.now().isoformat(),
                "username":       result["name"],
                "closed_week":    result["closed_week"],
                "win_rate_week":  result["win_rate_week"],
                "profit_factor":  result["profit_factor"],
                "closed_total":   result["closed_total"],
                "active":         active,
            })
            # Guardar niche scores en BD
            if result["niche_scores"]:
                database.save_niche_scores(result["wallet"], result["niche_scores"])
                for niche, ns in result["niche_scores"].items():
                    if ns["niche_score"] > 0:
                        niche_candidates[niche].append({
                            "wallet": result["wallet"],
                            "win_rate": ns["win_rate"],
                            "profit_factor": ns["profit_factor"],
                            "total_trades": ns["total_trades"],
                            "avg_size": ns["avg_size"],
                            "niche_score": ns["niche_score"]
                        })

    # Actualizar elite_traders: top 5 por nicho
    print()
    print("Actualizando elite traders por nicho...")
    for niche, candidates in niche_candidates.items():
        candidates.sort(key=lambda x: x["niche_score"], reverse=True)
        database.update_elite_traders(niche, candidates)
        print("  [" + niche.upper() + "] " + str(len(candidates)) + " candidatos -> top " + str(min(5, len(candidates))) + " elite")

    print()
    print("INSIDERS DETECTADOS: " + str(len(insiders)))
    print("=" * 70)
    pol = [i for i in insiders if i["category"] == "political"]
    spo = [i for i in insiders if i["category"] == "sports"]
    print("Politicos: " + str(len(pol)) + " | Deportes: " + str(len(spo)))
    print()
    insiders.sort(key=lambda x: x["insider_score"], reverse=True)
    for ins in insiders:
        niche_tag    = (ins.get("primary_niche") or ins["category"][:3].upper()).upper()
        closed_total = ins.get("closed_total", ins["total"])
        closed_week  = ins.get("closed_week", 0)
        wr_closed    = round(ins["win_rate"] * 100)
        wr_week      = round(ins["win_rate_week"] * 100) if ins["closed_week"] > 0 else "—"
        pf           = ins["profit_factor"]
        pf_week      = ins.get("profit_factor_week", pf)
        week_tag     = f"  reciente:{closed_week}pos/{wr_week}%WR/PF{pf_week}" if closed_week > 0 else "  sin datos recientes"
        print(f"[{niche_tag}] {ins['name']}"
              f"  closed:{closed_total}  WR:{wr_closed}%  {ins['wins']}W/{ins['losses']}L"
              f"  PF:{pf}"
              + week_tag
              + f"  score:{ins['insider_score']}  ${ins['monthly_pnl']}")
    return insiders

if __name__ == "__main__":
    run_analysis(200)
