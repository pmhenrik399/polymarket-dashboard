# -*- coding: utf-8 -*-
"""
Polymarket Bot Engine - GitHub Actions version
Runs every 5 min via GitHub Actions cron.
- Checks live prices
- Resolves positions (WIN/LOSS)
- Scans for new trades (Bot 1)
- Copies trades from GamblingIsAllYouNeed (Bot 2)
- Sends Telegram notifications
"""
import json
import os
import urllib.request
import urllib.parse
import time
import random
import datetime

os.environ['PYTHONIOENCODING'] = 'utf-8'

TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')

# Paths relative to repo root
REPO_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
DATA_DIR = os.path.join(REPO_ROOT, 'data')
BOT1_FILE = os.path.join(DATA_DIR, 'portfolio.json')
BOT2_FILE = os.path.join(DATA_DIR, 'portfolio_bot2.json')
STATE_FILE = os.path.join(DATA_DIR, 'bot_engine_state.json')

USD_NOK = 9.70


# ===== UTILITIES =====

def send_telegram(msg):
    if not TOKEN or not CHAT_ID:
        print(f"[TELEGRAM] {msg}")
        return False
    try:
        data = urllib.parse.urlencode({'chat_id': CHAT_ID, 'text': msg}).encode('utf-8')
        req = urllib.request.Request(f'https://api.telegram.org/bot{TOKEN}/sendMessage', data)
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        print(f"Telegram error: {e}")
        return False


def api_fetch(url):
    try:
        req = urllib.request.Request(url)
        req.add_header('User-Agent', 'Mozilla/5.0')
        r = urllib.request.urlopen(req, timeout=15)
        return json.loads(r.read().decode('utf-8'))
    except:
        return None


def fetch_market(market_id):
    return api_fetch(f'https://gamma-api.polymarket.com/markets/{market_id}')


def fetch_markets(params):
    base = 'https://gamma-api.polymarket.com/markets?'
    url = base + urllib.parse.urlencode(params)
    data = api_fetch(url)
    return data if isinstance(data, list) else []


def fetch_usd_nok():
    global USD_NOK
    try:
        data = api_fetch('https://open.er-api.com/v6/latest/USD')
        if data:
            USD_NOK = data.get('rates', {}).get('NOK', 9.70)
    except:
        pass
    return USD_NOK


def load_json(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return None


def save_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_state():
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {'last_heartbeat': '', 'last_scan': '', 'last_copy_sync': '',
                'known_copy_slugs': []}


def save_state(state):
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)


# ===== RESOLVE POSITIONS =====

def check_and_resolve(portfolio, bot_name, rate):
    if not portfolio:
        return False

    positions = portfolio.get('positions', [])
    closed = portfolio.get('closed_positions', [])
    account = portfolio.get('account', {})
    stats = portfolio.get('statistics', {})
    monthly = portfolio.get('monthly', {})
    changed = False
    still_open = []

    for pos in positions:
        mid = pos.get('market_id', '')
        if not mid:
            still_open.append(pos)
            continue

        market = fetch_market(mid)
        time.sleep(0.3)
        if not market:
            still_open.append(pos)
            continue

        # Update price
        try:
            prices = json.loads(market.get('outcomePrices', '[]'))
            side = pos.get('side', 'YES')
            pos['current_price_usd'] = float(prices[0] if side == 'YES' else prices[1])
        except:
            pass

        is_closed = market.get('closed', False)
        resolved = market.get('umaResolutionStatus', '') == 'resolved'

        if is_closed or resolved:
            try:
                prices = json.loads(market.get('outcomePrices', '[]'))
                yes_p = float(prices[0])
                no_p = float(prices[1])
            except:
                still_open.append(pos)
                continue

            side = pos.get('side', 'YES')
            shares = pos.get('shares', 0)
            cost_nok = pos.get('cost_nok', 0)
            won = (yes_p > 0.9) if side == 'YES' else (no_p > 0.9)

            if won:
                payout_nok = int(shares * rate)
                profit = payout_nok - cost_nok
                pos['result'] = 'WIN'
                pos['profit_nok'] = profit
                account['balance_nok'] = account.get('balance_nok', 0) + payout_nok
                stats['total_wins'] = stats.get('total_wins', 0) + 1
                monthly['wins_this_month'] = monthly.get('wins_this_month', 0) + 1
                send_telegram(f"[WIN] {bot_name}: {pos.get('question','?')} | +{profit} kr")
            else:
                pos['result'] = 'LOSS'
                pos['profit_nok'] = -cost_nok
                stats['total_losses'] = stats.get('total_losses', 0) + 1
                monthly['losses_this_month'] = monthly.get('losses_this_month', 0) + 1
                send_telegram(f"[LOSS] {bot_name}: {pos.get('question','?')} | -{cost_nok} kr")

            pos['closed_at'] = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            pnl = pos.get('profit_nok', 0)
            stats['total_realized_pnl_nok'] = stats.get('total_realized_pnl_nok', 0) + pnl
            monthly['realized_pnl_nok'] = monthly.get('realized_pnl_nok', 0) + pnl
            closed.append(pos)
            changed = True
            time.sleep(0.3)
        else:
            entry = pos.get('entry_price_usd', 0)
            cur = pos.get('current_price_usd', entry)
            shares = pos.get('shares', 0)
            pos['unrealized_pnl_nok'] = int((cur - entry) * shares * rate)
            still_open.append(pos)
            changed = True

    portfolio['positions'] = still_open
    portfolio['closed_positions'] = closed
    portfolio['account'] = account
    portfolio['statistics'] = stats
    portfolio['monthly'] = monthly
    portfolio['meta']['last_updated'] = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    portfolio['meta']['usd_nok_rate'] = rate
    return changed


# ===== BOT 1: SCANNER =====

def estimate_edge(market):
    try:
        prices = json.loads(market.get('outcomePrices', '[]'))
        yes_p = float(prices[0])
        no_p = float(prices[1])
    except:
        return None

    question = market.get('question', '').lower()
    liquidity = float(market.get('liquidity', 0) or 0)

    if liquidity < 1000:
        return None

    best_side = None
    best_edge = 0
    best_prob = 0
    best_price = 0

    if 0.05 < yes_p < 0.95:
        if 'under' in question or 'o/u' in question:
            adj = 0.03
        elif 'draw' in question:
            adj = -0.02
        elif yes_p > 0.70:
            adj = -0.02
        elif yes_p < 0.35:
            adj = -0.03
        else:
            adj = 0.04
        our_yes = yes_p + adj
        edge_yes = our_yes - yes_p
        if edge_yes > best_edge:
            best_side = 'YES'
            best_edge = edge_yes
            best_prob = our_yes
            best_price = yes_p

    if 0.05 < no_p < 0.95:
        if 'win' in question and no_p > 0.55:
            adj = 0.04
        elif 'draw' in question:
            adj = 0.04
        elif no_p > 0.70:
            adj = 0.03
        else:
            adj = 0.02
        our_no = no_p + adj
        edge_no = our_no - no_p
        if edge_no > best_edge:
            best_side = 'NO'
            best_edge = edge_no
            best_prob = our_no
            best_price = no_p

    if best_edge >= 0.03 and best_side:
        return (best_side, best_prob, best_price, best_edge)
    return None


def half_kelly(prob, price):
    if price <= 0 or price >= 1:
        return 0
    b = (1.0 / price) - 1.0
    q = 1.0 - prob
    f = ((b * prob - q) / b) * 0.5
    return max(0, min(f, 0.15))


def bot1_scan_and_trade(portfolio, rate, state):
    if not portfolio:
        return False

    account = portfolio.get('account', {})
    positions = portfolio.get('positions', [])
    balance = account.get('balance_nok', 0)
    total_cost = sum(p.get('cost_nok', 0) for p in positions)
    port_value = balance + total_cost

    if balance < port_value * 0.15:
        return False

    # Only scan every 15 minutes
    last_scan = state.get('last_scan', '')
    now_str = time.strftime('%H:%M', time.gmtime())
    if last_scan:
        try:
            lm = int(last_scan[:2]) * 60 + int(last_scan[3:5])
            nm = int(now_str[:2]) * 60 + int(now_str[3:5])
            if abs(nm - lm) < 14:
                return False
        except:
            pass

    state['last_scan'] = now_str

    if len(positions) >= 15:
        return False

    today = datetime.date.today()
    end_max = (today + datetime.timedelta(days=3)).isoformat()

    markets = fetch_markets({
        'active': 'true', 'closed': 'false',
        'limit': 80, 'order': 'volume', 'ascending': 'false',
        'liquidity_num_min': 2000,
        'end_date_min': today.isoformat(),
        'end_date_max': end_max
    })
    time.sleep(0.5)

    sports = fetch_markets({
        'active': 'true', 'closed': 'false',
        'limit': 50, 'order': 'volume', 'ascending': 'false',
        'tag': 'sports',
        'liquidity_num_min': 2000,
        'end_date_min': today.isoformat(),
        'end_date_max': end_max
    })
    time.sleep(0.5)

    seen_ids = set()
    all_markets = []
    for m in markets + sports:
        mid = m.get('id', '')
        if mid and mid not in seen_ids:
            seen_ids.add(mid)
            all_markets.append(m)

    existing_ids = set(p.get('market_id', '') for p in positions)
    candidates = [m for m in all_markets if str(m.get('id', '')) not in existing_ids]

    trades_opened = 0
    changed = False
    next_id = portfolio.get('next_position_id', len(positions) + 1)

    random.shuffle(candidates)

    for m in candidates:
        if trades_opened >= 4:
            break
        if len(positions) >= 15:
            break
        if balance < 50:
            break

        result = estimate_edge(m)
        if not result:
            continue

        side, our_prob, market_price, edge = result

        kelly_f = half_kelly(our_prob, market_price)
        if kelly_f <= 0:
            continue

        size_nok = int(min(kelly_f * port_value, port_value * 0.12, balance * 0.5))
        if size_nok < 40:
            continue

        size_usd = size_nok / rate
        shares = round(size_usd / market_price, 2)

        question = m.get('question', '?')
        slug = m.get('slug', '')
        end_date = m.get('endDate', m.get('end_date_iso', ''))[:10]

        pos = {
            'id': f'pos_{next_id:03d}',
            'market_id': str(m.get('id', '')),
            'question': question,
            'slug': slug,
            'side': side,
            'entry_price_usd': market_price,
            'current_price_usd': market_price,
            'shares': shares,
            'cost_usd': round(size_usd, 2),
            'cost_nok': size_nok,
            'usd_nok_at_entry': rate,
            'edge_at_entry': round(edge, 3),
            'our_probability': round(our_prob, 3),
            'opened_at': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'end_date': end_date,
            'category': 'sport',
            'unrealized_pnl_nok': 0
        }

        positions.append(pos)
        balance -= size_nok
        next_id += 1
        trades_opened += 1
        changed = True

        price_cents = int(market_price * 100)
        edge_pct = int(edge * 100)
        send_telegram(f"[NY TRADE] BOT1: {question} | {side} @ {price_cents}c | {size_nok} kr | Edge {edge_pct}%")
        time.sleep(0.3)

    if changed:
        account['balance_nok'] = balance
        portfolio['positions'] = positions
        portfolio['account'] = account
        portfolio['next_position_id'] = next_id
        monthly = portfolio.get('monthly', {})
        monthly['trades_this_month'] = monthly.get('trades_this_month', 0) + trades_opened
        portfolio['monthly'] = monthly

    return changed


# ===== BOT 2: COPY TRADER =====

def bot2_copy_sync(portfolio, rate, state):
    if not portfolio:
        return False

    last_sync = state.get('last_copy_sync', '')
    now_str = time.strftime('%H:%M', time.gmtime())
    if last_sync:
        try:
            lm = int(last_sync[:2]) * 60 + int(last_sync[3:5])
            nm = int(now_str[:2]) * 60 + int(now_str[3:5])
            if abs(nm - lm) < 28:
                return False
        except:
            pass

    state['last_copy_sync'] = now_str

    try:
        url = 'https://polymarket.com/@gamblingisallyouneed'
        req = urllib.request.Request(url)
        req.add_header('User-Agent', 'Mozilla/5.0')
        r = urllib.request.urlopen(req, timeout=20)
        html = r.read().decode('utf-8', errors='ignore')
    except:
        return False

    import re
    slugs = re.findall(r'/event/([a-zA-Z0-9_-]+)', html)
    unique_slugs = list(dict.fromkeys(slugs))

    if not unique_slugs:
        return False

    positions = portfolio.get('positions', [])
    known_slugs = set(state.get('known_copy_slugs', []))
    account = portfolio.get('account', {})
    balance = account.get('balance_nok', 0)
    next_id = portfolio.get('next_position_id', len(positions) + 1)
    changed = False
    trades_opened = 0

    for event_slug in unique_slugs[:20]:
        if trades_opened >= 3:
            break
        if balance < 25:
            break
        if len(positions) >= 15:
            break
        if event_slug in known_slugs:
            continue

        known_slugs.add(event_slug)

        event_data = api_fetch(f'https://gamma-api.polymarket.com/events?slug={event_slug}')
        time.sleep(0.5)
        if not event_data or not isinstance(event_data, list) or len(event_data) == 0:
            continue

        event = event_data[0]
        event_markets = event.get('markets', [])
        if not event_markets:
            continue

        m = event_markets[0]
        mid = str(m.get('id', ''))
        slug = m.get('slug', '')
        question = m.get('question', '')
        closed = m.get('closed', False)

        if closed:
            continue

        if any(p.get('market_id') == mid for p in positions):
            continue

        try:
            prices = json.loads(m.get('outcomePrices', '[]'))
            yes_p = float(prices[0])
            no_p = float(prices[1])
        except:
            continue

        if yes_p > 0.5:
            side = 'YES'
            price = yes_p
        else:
            side = 'NO'
            price = no_p

        if price < 0.10 or price > 0.95:
            continue

        total_cost = sum(p.get('cost_nok', 0) for p in positions)
        port_value = balance + total_cost
        size_nok = min(int(port_value * 0.07), int(balance * 0.4), 80)
        if size_nok < 25:
            continue

        size_usd = size_nok / rate
        shares = round(size_usd / price, 2)
        end_date = m.get('endDate', '')[:10]

        pos = {
            'id': f'cp_{next_id:03d}',
            'market_id': mid,
            'question': question,
            'slug': slug,
            'side': side,
            'entry_price_usd': price,
            'current_price_usd': price,
            'shares': shares,
            'cost_usd': round(size_usd, 2),
            'cost_nok': size_nok,
            'usd_nok_at_entry': rate,
            'opened_at': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'end_date': end_date,
            'category': 'sport',
            'unrealized_pnl_nok': 0
        }

        positions.append(pos)
        balance -= size_nok
        next_id += 1
        trades_opened += 1
        changed = True

        price_cents = int(price * 100)
        send_telegram(f"[COPY] BOT2: {question} | {side} @ {price_cents}c | {size_nok} kr")
        time.sleep(0.3)

    if changed:
        account['balance_nok'] = balance
        portfolio['positions'] = positions
        portfolio['account'] = account
        portfolio['next_position_id'] = next_id

    state['known_copy_slugs'] = list(known_slugs)
    return changed


# ===== MAIN =====

def get_summary(portfolio):
    if not portfolio:
        return 0, 0, 0, 0
    positions = portfolio.get('positions', [])
    account = portfolio.get('account', {})
    stats = portfolio.get('statistics', {})
    monthly = portfolio.get('monthly', {})
    balance = account.get('balance_nok', 0)
    total_cost = sum(p.get('cost_nok', 0) for p in positions)
    value = balance + total_cost
    wins = stats.get('total_wins', monthly.get('wins_this_month', 0))
    losses = stats.get('total_losses', monthly.get('losses_this_month', 0))
    return value, len(positions), wins, losses


def main():
    print(f"Bot engine starting at {datetime.datetime.now(datetime.timezone.utc).isoformat()}")

    state = load_state()
    rate = fetch_usd_nok()
    print(f"USD/NOK rate: {rate}")

    bot1 = load_json(BOT1_FILE)
    bot2 = load_json(BOT2_FILE)

    if not bot1:
        print("ERROR: Could not load Bot1 portfolio")
    if not bot2:
        print("ERROR: Could not load Bot2 portfolio")

    # 1. Resolve positions
    c1 = check_and_resolve(bot1, 'BOT1', rate)
    c2 = check_and_resolve(bot2, 'BOT2', rate)
    print(f"Resolve: bot1={c1}, bot2={c2}")

    # 2. Bot 1: scan and trade
    c3 = bot1_scan_and_trade(bot1, rate, state)
    print(f"Bot1 scan: {c3}")

    # 3. Bot 2: copy sync
    c4 = bot2_copy_sync(bot2, rate, state)
    print(f"Bot2 copy: {c4}")

    # 4. Save
    if bot1 and (c1 or c3):
        save_json(BOT1_FILE, bot1)
        print("Saved Bot1 portfolio")
    if bot2 and (c2 or c4):
        save_json(BOT2_FILE, bot2)
        print("Saved Bot2 portfolio")

    # 5. Heartbeat every 30 min
    now = time.strftime('%H:%M', time.gmtime())
    now_m = int(now[:2]) * 60 + int(now[3:5])
    last_hb = state.get('last_heartbeat', '')
    send_hb = True
    if last_hb:
        try:
            lm = int(last_hb[:2]) * 60 + int(last_hb[3:5])
            if abs(now_m - lm) < 25:
                send_hb = False
        except:
            pass

    if send_hb:
        v1, p1, w1, l1 = get_summary(bot1)
        v2, p2, w2, l2 = get_summary(bot2)
        msg = (f"[{now}] POLYMARKET STATUS\n"
               f"Bot1: {v1} kr | {p1} pos | W{w1}/L{l1}\n"
               f"Bot2: {v2} kr | {p2} pos | W{w2}/L{l2}\n"
               f"Totalt: {v1 + v2} kr | Kurs: {rate:.2f}")
        send_telegram(msg)
        state['last_heartbeat'] = now
        print(f"Heartbeat sent")

    save_state(state)
    print("Done!")


if __name__ == '__main__':
    main()
