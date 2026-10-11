import requests
from datetime import datetime
import database

database.init_db()

categories = ['politics', 'sports', 'crypto', 'culture', 'economics']
total_saved = 0

for cat in categories:
    traders = []
    for offset in [0, 50, 100]:
        r = requests.get('https://data-api.polymarket.com/v1/leaderboard?timePeriod=month&orderBy=PNL&limit=50&offset=' + str(offset) + '&category=' + cat, timeout=10)
        data = r.json()
        if not data:
            break
        traders.extend(data)
    print(cat.upper() + ': ' + str(len(traders)) + ' traders encontrados')
    for t in traders[:20]:
        wallet = t.get('proxyWallet', '')
        pnl = float(t.get('pnl', 0))
        name = t.get('userName', '')[:25]
        if not wallet or pnl <= 0:
            continue
        database.save_wallet(wallet, {
            'total_trades': 10,
            'winning_trades': 8,
            'win_rate': 0.75,
            'avg_entry_odds': 0.5,
            'insider_score': 0.75,
            'first_seen': datetime.now().isoformat()
        })
        total_saved += 1
        print('  [' + cat[:3].upper() + '] ' + name + ' | $' + str(round(pnl)))

print()
print('Total traders fichados: ' + str(total_saved))
wallets = database.get_top_wallets(100)
print('Total en base de datos: ' + str(len(wallets)))