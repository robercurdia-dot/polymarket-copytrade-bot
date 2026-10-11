import requests
from datetime import datetime, timedelta
import database
import config

GAMMA_URL = 'https://gamma-api.polymarket.com'
DATA_URL = 'https://data-api.polymarket.com'

POLITICAL_KEYWORDS = ['Trump', 'Iran', 'Russia', 'Ukraine', 'China', 'election', 'ceasefire', 'NATO', 'sanction', 'war', 'president', 'congress', 'senate', 'nuclear', 'tariff', 'Putin', 'Xi', 'Netanyahu', 'Gaza', 'Israel', 'OPEC', 'coup', 'resign', 'impeach']

def is_resolving_soon(market, days=7):
    end_date = market.get('endDate', market.get('end_date', ''))
    if not end_date:
        return False
    try:
        if 'T' in str(end_date):
            dt = datetime.fromisoformat(str(end_date).replace('Z', ''))
        else:
            dt = datetime.fromisoformat(str(end_date))
        now = datetime.utcnow()
        return now < dt < now + timedelta(days=days)
    except:
        return False

def get_political_markets(limit=300):
    political = []
    offset = 0
    while offset < limit:
        r = requests.get(GAMMA_URL + '/markets?limit=100&active=true&offset=' + str(offset), timeout=10)
        markets = r.json()
        if not markets:
            break
        for m in markets:
            q = m.get('question', '')
            if any(kw.lower() in q.lower() for kw in POLITICAL_KEYWORDS):
                if is_resolving_soon(m, days=7):
                    political.append({'id': m.get('id'), 'conditionId': m.get('conditionId'), 'question': q, 'volume': float(m.get('volume') or 0), 'endDate': m.get('endDate', '')})
        offset += 100
    political.sort(key=lambda x: x['volume'], reverse=True)
    return political

def get_top_holders(condition_id):
    try:
        r = requests.get(DATA_URL + '/holders?market=' + condition_id + '&limit=20', timeout=10)
        if r.status_code == 200:
            data = r.json()
            wallets = []
            for token_data in data:
                for h in token_data.get('holders', []):
                    wallet = h.get('proxyWallet', '')
                    if wallet and wallet not in wallets:
                        wallets.append(wallet)
            return wallets
    except Exception as e:
        print('Error holders: ' + str(e))
    return []

def get_wallet_history(wallet_address):
    try:
        r = requests.get(DATA_URL + '/activity?user=' + wallet_address + '&limit=50', timeout=10)
        return r.json() if r.status_code == 200 else []
    except Exception as e:
        print('Error wallet history: ' + str(e))
        return []

def calculate_insider_score(wallet_address):
    trades = get_wallet_history(wallet_address)
    if not trades or len(trades) < config.MIN_TRADES_TO_QUALIFY:
        return None
    political_trades = [t for t in trades if any(kw.lower() in t.get('title', '').lower() for kw in POLITICAL_KEYWORDS)]
    if len(political_trades) < 3:
        return None
    total = len(political_trades)
    winning = sum(1 for t in political_trades if float(t.get('cashPnl', 0)) > 0)
    win_rate = winning / total if total > 0 else 0
    if win_rate < config.MIN_WIN_RATE:
        return None
    score = win_rate * min(total / 10, 1.0)
    stats = {
        'total_trades': total,
        'winning_trades': winning,
        'win_rate': round(win_rate, 4),
        'avg_entry_odds': 0.5,
        'insider_score': round(score, 4),
        'first_seen': datetime.now().isoformat()
    }
    database.save_wallet(wallet_address, stats)
    return stats

def scan_top_traders(limit=300):
    print('Obteniendo mercados politicos que resuelven en 7 dias...')
    markets = get_political_markets(limit)
    print('Mercados encontrados: ' + str(len(markets)))
    if not markets:
        print('No hay mercados politicos proximos. Ampliando a 30 dias...')
        markets = get_political_markets_extended(limit, days=30)
    for m in markets[:5]:
        print(' - ' + m['question'][:70] + ' | ends: ' + str(m.get('endDate', ''))[:10])
    seen_wallets = set()
    insiders = []
    for m in markets[:10]:
        holders = get_top_holders(m['conditionId'])
        for wallet in holders:
            if wallet in seen_wallets:
                continue
            seen_wallets.add(wallet)
            stats = calculate_insider_score(wallet)
            if stats:
                insiders.append({'address': wallet, **stats})
                print('Insider: ' + wallet[:10] + ' score=' + str(stats['insider_score']) + ' win_rate=' + str(round(stats['win_rate']*100)) + '%')
    print('Total insiders: ' + str(len(insiders)))
    return insiders

def get_political_markets_extended(limit=300, days=30):
    political = []
    offset = 0
    while offset < limit:
        r = requests.get(GAMMA_URL + '/markets?limit=100&active=true&offset=' + str(offset), timeout=10)
        markets = r.json()
        if not markets:
            break
        for m in markets:
            q = m.get('question', '')
            if any(kw.lower() in q.lower() for kw in POLITICAL_KEYWORDS):
                if is_resolving_soon(m, days=days):
                    political.append({'id': m.get('id'), 'conditionId': m.get('conditionId'), 'question': q, 'volume': float(m.get('volume') or 0), 'endDate': m.get('endDate', '')})
        offset += 100
    political.sort(key=lambda x: x['volume'], reverse=True)
    return political

if __name__ == '__main__':
    print('Escaneando insiders politicos...')
    results = scan_top_traders(300)