# -*- coding: utf-8 -*-
"""
Polymarket Bot Engine v2 - Smart Edition
Runs every 5 min via GitHub Actions cron.

Improvements over v1:
1. Orderbook analysis - reads CLOB orderbook for buy/sell pressure signals
2. Line movement - tracks price changes, follows sharp money
3. Category learning - weights bets toward historically winning categories
4. Avoids 50c zone - skips 45-55c markets unless strong signal
5. CLV tracking - measures closing line value to validate real edge
"""
import json
import os
import urllib.request
import urllib.parse
import time
import random
import datetime
import re

os.environ['PYTHONIOENCODING'] = 'utf-8'

TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')

REPO_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
DATA_DIR = os.path.join(REPO_ROOT, 'data')
BOT1_FILE = os.path.join(DATA_DIR, 'portfolio.json')
BOT2_FILE = os.path.join(DATA_DIR, 'portfolio_bot2.json')
STATE_FILE = os.path.join(DATA_DIR, 'bot_engine_state.json')

USD_NOK = 9.70
POLYMARKET_FEE_PCT = 0.02


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
                'known_copy_slugs': [], 'price_snapshots': {},
                'category_stats': {}}


def save_state(state):
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)


# ===== [1] ORDERBOOK ANALYSIS =====

def fetch_orderbook(token_id):
    """Fetch CLOB orderbook and analyze buy/sell pressure."""
    data = api_fetch(f'https://clob.polymarket.com/book?token_id={token_id}')
    if not data:
        return None
    try:
        bids = data.get('bids', [])
        asks = data.get('asks', [])

        # Sum up volume within 5c of best bid/ask
        bid_volume = 0
        ask_volume = 0
        best_bid = float(bids[0]['price']) if bids else 0
        best_ask = float(asks[0]['price']) if asks else 1

        for b in bids[:10]:
            p = float(b['price'])
            s = float(b['size'])
            if p >= best_bid - 0.05:
                bid_volume += s

        for a in asks[:10]:
            p = float(a['price'])
            s = float(a['size'])
            if p <= best_ask + 0.05:
                ask_volume += s

        spread = best_ask - best_bid
        total = bid_volume + ask_volume
        if total == 0:
            return None

        # Imbalance: positive = more buyers (bullish for YES)
        imbalance = (bid_volume - ask_volume) / total

        return {
            'bid_volume': bid_volume,
            'ask_volume': ask_volume,
            'imbalance': imbalance,
            'spread': spread,
            'best_bid': best_bid,
            'best_ask': best_ask
        }
    except:
        return None


def orderbook_signal(market, side):
    """Get orderbook signal for a specific side.
    Returns edge adjustment based on order flow."""
    try:
        tokens = json.loads(market.get('clobTokenIds', '[]'))
        if not tokens:
            return 0

        # tokens[0] = YES token, tokens[1] = NO token
        token_idx = 0 if side == 'YES' else 1
        if token_idx >= len(tokens):
            return 0

        ob = fetch_orderbook(tokens[token_idx])
        if not ob:
            return 0

        imbalance = ob['imbalance']
        spread = ob['spread']

        # Wide spread = illiquid, penalize
        if spread > 0.05:
            return -0.01

        # Strong buy imbalance = smart money signal
        if imbalance > 0.3:
            return 0.02  # +2% edge boost
        elif imbalance > 0.15:
            return 0.01  # +1% edge boost
        elif imbalance < -0.3:
            return -0.02  # negative signal, buyers fleeing
        elif imbalance < -0.15:
            return -0.01

        return 0
    except:
        return 0


# ===== [2] LINE MOVEMENT TRACKING =====

def track_price(state, market_id, current_price):
    """Store price snapshot and detect line movement."""
    snapshots = state.get('price_snapshots', {})
    now_ts = int(time.time())

    if market_id not in snapshots:
        snapshots[market_id] = []

    snaps = snapshots[market_id]
    snaps.append({'t': now_ts, 'p': round(current_price, 4)})

    # Keep only last 24 hours of snapshots
    cutoff = now_ts - 86400
    snaps = [s for s in snaps if s['t'] > cutoff]
    snapshots[market_id] = snaps
    state['price_snapshots'] = snapshots

    return snaps


def line_movement_signal(snaps, current_price):
    """Analyze line movement for sharp money detection.
    Returns edge adjustment."""
    if len(snaps) < 2:
        return 0

    # Look at price change over last 2 hours
    now_ts = int(time.time())
    recent = [s for s in snaps if s['t'] > now_ts - 7200]
    if len(recent) < 2:
        return 0

    oldest_price = recent[0]['p']
    move = current_price - oldest_price

    # Strong movement in one direction = sharp money
    if abs(move) > 0.08:
        # Price moved 8+ cents — follow the money
        return 0.025 if move > 0 else -0.025
    elif abs(move) > 0.04:
        return 0.015 if move > 0 else -0.015
    elif abs(move) > 0.02:
        return 0.005 if move > 0 else -0.005

    return 0


# ===== [3] CATEGORY LEARNING =====

def get_category_weights(portfolio):
    """Calculate category weights based on historical win rates."""
    closed = portfolio.get('closed_positions', [])
    if not closed:
        return {}

    cats = {}
    for pos in closed:
        # Determine category from question/slug
        cat = classify_bet(pos.get('question', ''), pos.get('slug', ''))
        if cat not in cats:
            cats[cat] = {'wins': 0, 'losses': 0}
        if pos.get('result') == 'WIN':
            cats[cat]['wins'] += 1
        elif pos.get('result') in ('LOSS', 'STOP_LOSS'):
            cats[cat]['losses'] += 1

    weights = {}
    for cat, record in cats.items():
        total = record['wins'] + record['losses']
        if total >= 2:
            win_rate = record['wins'] / total
            # Boost categories with >55% win rate, penalize <45%
            if win_rate > 0.55:
                weights[cat] = 0.02  # +2% edge boost
            elif win_rate < 0.45:
                weights[cat] = -0.02  # -2% edge penalty
            else:
                weights[cat] = 0
        else:
            weights[cat] = 0  # not enough data

    return weights


def classify_bet(question, slug=''):
    """Classify a bet into a category for learning."""
    q = (question or '').lower()
    s = (slug or '').lower()

    if 'draw' in q:
        return 'no_draw'
    elif 'o/u' in q or 'under' in q or 'over' in q or 'total' in s:
        return 'over_under'
    elif 'counter-strike' in q or 'cs2' in s or 'lol' in q or 'dota' in q or 'valorant' in q:
        return 'esport'
    elif 'up or down' in q or 'updown' in s:
        return 'crypto_flip'
    elif 'unemployment' in q or 'gdp' in q or 'inflation' in q or 'rate' in q:
        return 'economics'
    elif 'tweet' in q or 'elon' in q:
        return 'social_media'
    elif 'win' in q or 'nhl' in s or 'nba' in s or 'mls' in s or 'mlb' in s:
        return 'moneyline'
    else:
        return 'other'


# ===== [4] EDGE ESTIMATION (SMART VERSION) =====

def estimate_edge_smart(market, portfolio, state):
    """Smart edge estimation using orderbook, line movement, and category learning.
    Returns (side, our_prob, market_price, edge, signals_used) or None."""
    try:
        prices = json.loads(market.get('outcomePrices', '[]'))
        yes_p = float(prices[0])
        no_p = float(prices[1])
    except:
        return None

    question = market.get('question', '').lower()
    slug = market.get('slug', '').lower()
    market_id = str(market.get('id', ''))
    liquidity = float(market.get('liquidity', 0) or 0)

    if liquidity < 1000:
        return None

    # [FILTER] Skip crypto coin flips
    coin_flip_tokens = ['sol-updown', 'bnb-updown', 'btc-updown', 'eth-updown',
                        'doge-updown', 'xrp-updown', 'ada-updown', 'avax-updown',
                        'up or down']
    if any(t in slug or t in question for t in coin_flip_tokens):
        return None

    # [FILTER 4] Skip 45-55c zone unless we find strong signals later
    in_deadzone = False

    # Get category weight [3]
    cat = classify_bet(question, slug)
    cat_weights = get_category_weights(portfolio) if portfolio else {}
    cat_adj = cat_weights.get(cat, 0)

    # Track price and get line movement signal [2]
    snaps_yes = track_price(state, f'{market_id}_yes', yes_p)
    snaps_no = track_price(state, f'{market_id}_no', no_p)

    signals = []
    best_side = None
    best_edge = 0
    best_prob = 0
    best_price = 0

    # Evaluate YES side
    if 0.05 < yes_p < 0.95:
        # Base heuristic
        if 'under' in question or 'o/u' in question or 'total' in slug:
            base_adj = 0.03
        elif 'draw' in question:
            base_adj = -0.02
        elif yes_p > 0.70:
            base_adj = -0.02
        elif yes_p < 0.35:
            base_adj = -0.03
        else:
            base_adj = 0.02

        # Line movement [2]
        line_adj_yes = line_movement_signal(snaps_yes, yes_p)

        # Total edge for YES
        total_adj_yes = base_adj + cat_adj + line_adj_yes
        our_yes = yes_p + total_adj_yes
        edge_yes = total_adj_yes

        if edge_yes > best_edge:
            best_side = 'YES'
            best_edge = edge_yes
            best_prob = our_yes
            best_price = yes_p
            signals = [f'base:{base_adj:.3f}', f'cat:{cat_adj:.3f}', f'line:{line_adj_yes:.3f}']

    # Evaluate NO side
    if 0.05 < no_p < 0.95:
        if 'win' in question and no_p > 0.55:
            base_adj = 0.04
        elif 'draw' in question:
            base_adj = 0.04
        elif no_p > 0.70:
            base_adj = 0.03
        else:
            base_adj = 0.02

        line_adj_no = line_movement_signal(snaps_no, no_p)

        total_adj_no = base_adj + cat_adj + line_adj_no
        our_no = no_p + total_adj_no
        edge_no = total_adj_no

        if edge_no > best_edge:
            best_side = 'NO'
            best_edge = edge_no
            best_prob = our_no
            best_price = no_p
            signals = [f'base:{base_adj:.3f}', f'cat:{cat_adj:.3f}', f'line:{line_adj_no:.3f}']

    if not best_side:
        return None

    # [4] Dead zone check: 45-55c requires extra edge
    if 0.45 <= best_price <= 0.55:
        in_deadzone = True
        # Need at least 5% edge in dead zone (normally 3%)
        if best_edge < 0.05:
            return None

    # Minimum edge threshold
    if best_edge < 0.03:
        return None

    # [1] Orderbook analysis (only for candidates that pass base filter)
    ob_adj = orderbook_signal(market, best_side)
    if ob_adj != 0:
        best_edge += ob_adj
        best_prob += ob_adj
        signals.append(f'ob:{ob_adj:.3f}')

    # Re-check after orderbook adjustment
    min_edge = 0.05 if in_deadzone else 0.03
    if best_edge < min_edge:
        return None

    return (best_side, best_prob, best_price, best_edge, signals)


# ===== SLIPPAGE & KELLY =====

def calc_slippage(market_price, size_usd, liquidity):
    if liquidity <= 0:
        return 0.03
    ratio = size_usd / liquidity
    slip = 0.005 + ratio * 0.5
    return min(slip, 0.05)


def half_kelly(prob, price):
    if price <= 0 or price >= 1:
        return 0
    b = (1.0 / price) - 1.0
    q = 1.0 - prob
    f = ((b * prob - q) / b) * 0.5
    return max(0, min(f, 0.15))


# ===== [5] CLV TRACKING =====

def record_clv(pos, closing_price):
    """Record Closing Line Value. If we consistently beat the closing line,
    we have real edge."""
    entry = pos.get('entry_price_usd', 0)
    mid = pos.get('mid_price_usd', entry)
    side = pos.get('side', 'YES')

    # CLV = how much better our entry was vs closing price
    # For YES: we want entry < closing (we bought cheap)
    # For NO: we want entry < closing (we bought cheap)
    if side == 'YES':
        clv = closing_price - mid  # positive = we got a better price
    else:
        clv = closing_price - mid

    pos['closing_price'] = closing_price
    pos['clv'] = round(clv, 4)
    return clv


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

            # [5] Record CLV - use last price before close
            closing_price = yes_p if side == 'YES' else no_p
            clv = record_clv(pos, closing_price)

            if won:
                gross_payout_nok = int(shares * rate)
                winnings = gross_payout_nok - cost_nok
                fee_nok = int(max(winnings, 0) * POLYMARKET_FEE_PCT)
                payout_nok = gross_payout_nok - fee_nok
                profit = payout_nok - cost_nok
                pos['result'] = 'WIN'
                pos['profit_nok'] = profit
                pos['fee_nok'] = fee_nok
                account['balance_nok'] = account.get('balance_nok', 0) + payout_nok
                stats['total_wins'] = stats.get('total_wins', 0) + 1
                monthly['wins_this_month'] = monthly.get('wins_this_month', 0) + 1
                send_telegram(f"[WIN] {bot_name}: {pos.get('question','?')} | +{profit} kr (fee: {fee_nok} kr) CLV: {clv:+.3f}")
            else:
                pos['result'] = 'LOSS'
                pos['profit_nok'] = -cost_nok
                stats['total_losses'] = stats.get('total_losses', 0) + 1
                monthly['losses_this_month'] = monthly.get('losses_this_month', 0) + 1
                send_telegram(f"[LOSS] {bot_name}: {pos.get('question','?')} | -{cost_nok} kr CLV: {clv:+.3f}")

            pos['closed_at'] = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            pnl = pos.get('profit_nok', 0)
            stats['total_realized_pnl_nok'] = stats.get('total_realized_pnl_nok', 0) + pnl
            monthly['realized_pnl_nok'] = monthly.get('realized_pnl_nok', 0) + pnl

            # [5] Track cumulative CLV
            clv_list = stats.get('clv_history', [])
            clv_list.append(round(clv, 4))
            stats['clv_history'] = clv_list
            stats['avg_clv'] = round(sum(clv_list) / len(clv_list), 4) if clv_list else 0

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


# ===== BOT 1: SMART SCANNER =====

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

    # Score all candidates first, then pick best ones (not random)
    scored = []
    for m in candidates:
        result = estimate_edge_smart(m, portfolio, state)
        if not result:
            continue
        side, our_prob, market_price, edge, signals = result
        scored.append((edge, m, side, our_prob, market_price, signals))
        time.sleep(0.2)  # rate limit for orderbook calls

    # Sort by edge descending — take the best opportunities
    scored.sort(key=lambda x: x[0], reverse=True)

    trades_opened = 0
    changed = False
    next_id = portfolio.get('next_position_id', len(positions) + 1)

    for edge, m, side, our_prob, market_price, signals in scored:
        if trades_opened >= 4:
            break
        if len(positions) >= 15:
            break
        if balance < 50:
            break

        kelly_f = half_kelly(our_prob, market_price)
        if kelly_f <= 0:
            continue

        size_nok = int(min(kelly_f * port_value, port_value * 0.12, balance * 0.5))
        if size_nok < 40:
            continue

        size_usd = size_nok / rate
        liquidity = float(m.get('liquidity', 0) or 0)
        slippage = calc_slippage(market_price, size_usd, liquidity)

        fill_price = round(market_price * (1 + slippage), 4)
        if fill_price >= 0.98:
            continue

        shares = round(size_usd / fill_price, 2)

        question = m.get('question', '?')
        slug = m.get('slug', '')
        end_date = m.get('endDate', m.get('end_date_iso', ''))[:10]
        cat = classify_bet(question, slug)

        pos = {
            'id': f'pos_{next_id:03d}',
            'market_id': str(m.get('id', '')),
            'question': question,
            'slug': slug,
            'side': side,
            'entry_price_usd': fill_price,
            'mid_price_usd': market_price,
            'current_price_usd': fill_price,
            'slippage_pct': round(slippage * 100, 2),
            'shares': shares,
            'cost_usd': round(size_usd, 2),
            'cost_nok': size_nok,
            'usd_nok_at_entry': rate,
            'edge_at_entry': round(edge, 3),
            'our_probability': round(our_prob, 3),
            'signals': signals,
            'category': cat,
            'opened_at': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'end_date': end_date,
            'unrealized_pnl_nok': 0
        }

        positions.append(pos)
        balance -= size_nok
        next_id += 1
        trades_opened += 1
        changed = True

        price_cents = int(market_price * 100)
        edge_pct = int(edge * 100)
        sig_str = ' '.join(signals)
        send_telegram(f"[NY TRADE] BOT1: {question} | {side} @ {price_cents}c | {size_nok} kr | Edge {edge_pct}% | {sig_str}")
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
        mid_val = str(m.get('id', ''))
        slug = m.get('slug', '')
        question = m.get('question', '')
        closed = m.get('closed', False)

        if closed:
            continue

        if any(p.get('market_id') == mid_val for p in positions):
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
            'market_id': mid_val,
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
        return 0, 0, 0, 0, 0
    positions = portfolio.get('positions', [])
    account = portfolio.get('account', {})
    stats = portfolio.get('statistics', {})
    balance = account.get('balance_nok', 0)
    total_cost = sum(p.get('cost_nok', 0) for p in positions)
    value = balance + total_cost
    wins = stats.get('total_wins', 0)
    losses = stats.get('total_losses', 0)
    avg_clv = stats.get('avg_clv', 0)
    return value, len(positions), wins, losses, avg_clv


def main():
    print(f"Bot engine v2 starting at {datetime.datetime.now(datetime.timezone.utc).isoformat()}")

    state = load_state()
    # Ensure new state fields exist
    if 'price_snapshots' not in state:
        state['price_snapshots'] = {}
    if 'category_stats' not in state:
        state['category_stats'] = {}

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

    # 2. Bot 1: smart scan and trade
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
        v1, p1, w1, l1, clv1 = get_summary(bot1)
        v2, p2, w2, l2, clv2 = get_summary(bot2)
        msg = (f"[{now}] POLYMARKET STATUS v2\n"
               f"Bot1: {v1} kr | {p1} pos | W{w1}/L{l1} | CLV: {clv1:+.3f}\n"
               f"Bot2: {v2} kr | {p2} pos | W{w2}/L{l2}\n"
               f"Totalt: {v1 + v2} kr | Kurs: {rate:.2f}")
        send_telegram(msg)
        state['last_heartbeat'] = now
        print(f"Heartbeat sent")

    # Clean old price snapshots (keep last 24h only)
    cutoff = int(time.time()) - 86400
    snaps = state.get('price_snapshots', {})
    for k in list(snaps.keys()):
        snaps[k] = [s for s in snaps[k] if s.get('t', 0) > cutoff]
        if not snaps[k]:
            del snaps[k]
    state['price_snapshots'] = snaps

    save_state(state)
    print("Done!")


if __name__ == '__main__':
    main()
