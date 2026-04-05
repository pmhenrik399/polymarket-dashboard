# -*- coding: utf-8 -*-
"""
Polymarket Bot Engine v3 - Bookmaker Edge Edition
Runs every 5 min via GitHub Actions cron.

Key strategy: Compare Polymarket prices against real bookmaker odds
to find genuine mispricings. Falls back to orderbook/line signals
only when no bookmaker match is found.
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
ODDS_API_KEY = os.environ.get('ODDS_API_KEY', '')

REPO_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
DATA_DIR = os.path.join(REPO_ROOT, 'data')
BOT1_FILE = os.path.join(DATA_DIR, 'portfolio.json')
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
        return {'last_scan': '', 'price_snapshots': {},
                'category_stats': {}}


def save_state(state):
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)


# ===== [0] BOOKMAKER ODDS =====

ODDS_SPORTS = [
    'soccer_epl', 'soccer_spain_la_liga', 'soccer_italy_serie_a',
    'soccer_germany_bundesliga', 'soccer_france_ligue_one', 'soccer_brazil_serie_a',
    'soccer_portugal_primeira_liga', 'soccer_netherlands_eredivisie',
    'soccer_turkey_super_league', 'soccer_korea_kleague1',
    'basketball_nba', 'icehockey_nhl', 'baseball_mlb', 'mma_mixed_martial_arts',
    'soccer_efl_champ', 'soccer_argentina_primera_division',
]

# Common suffixes to strip for matching
TEAM_NOISE = ['fc', 'sc', 'cf', 'afc', 'bc', 'sk', 'fk', 'club', 'city', 'united',
              'sporting', 'athletic', 'real', 'deportivo', 'sd', 'cd', 'ca', 'ec']


def normalize_team(name):
    """Normalize team name for fuzzy matching."""
    n = name.lower().strip()
    # Remove common prefixes/suffixes
    for noise in TEAM_NOISE:
        n = n.replace(f' {noise} ', ' ').replace(f' {noise}', '').replace(f'{noise} ', '')
    # Remove non-alpha
    n = ''.join(c for c in n if c.isalpha() or c == ' ')
    return ' '.join(n.split())


def team_match(poly_text, odds_team):
    """Check if a bookmaker team name appears in Polymarket text."""
    poly = normalize_team(poly_text)
    team = normalize_team(odds_team)
    if not team or len(team) < 3:
        return False
    # Check if major words match
    team_words = [w for w in team.split() if len(w) >= 3]
    if not team_words:
        return False
    matches = sum(1 for w in team_words if w in poly)
    return matches >= max(1, len(team_words) * 0.5)


def fetch_bookmaker_odds(state):
    """Fetch odds from the-odds-api.com. Cache for 30 min in state."""
    if not ODDS_API_KEY:
        return state.get('cached_odds', {})

    last_fetch = state.get('odds_last_fetched', 0)
    now = int(time.time())
    if now - last_fetch < 1800 and state.get('cached_odds'):
        return state['cached_odds']

    all_odds = {}
    fetched_sports = 0
    for sport in ODDS_SPORTS:
        if fetched_sports >= 6:  # limit API calls per run
            break
        url = (f'https://api.the-odds-api.com/v4/sports/{sport}/odds/?'
               f'apiKey={ODDS_API_KEY}&regions=eu&markets=h2h&oddsFormat=decimal')
        data = api_fetch(url)
        if not data or not isinstance(data, list):
            continue
        fetched_sports += 1
        for event in data:
            home = event.get('home_team', '')
            away = event.get('away_team', '')
            commence = event.get('commence_time', '')
            bookmakers = event.get('bookmakers', [])
            if not bookmakers:
                continue
            # Average odds across bookmakers for more accurate line
            home_odds_list = []
            away_odds_list = []
            draw_odds_list = []
            for bm in bookmakers:
                for market in bm.get('markets', []):
                    if market.get('key') != 'h2h':
                        continue
                    for outcome in market.get('outcomes', []):
                        name = outcome.get('name', '')
                        price = outcome.get('price', 0)
                        if name == home:
                            home_odds_list.append(price)
                        elif name == away:
                            away_odds_list.append(price)
                        elif name == 'Draw':
                            draw_odds_list.append(price)

            if not home_odds_list or not away_odds_list:
                continue

            avg_home = sum(home_odds_list) / len(home_odds_list)
            avg_away = sum(away_odds_list) / len(away_odds_list)
            avg_draw = sum(draw_odds_list) / len(draw_odds_list) if draw_odds_list else 0

            # Convert decimal odds to implied probability (remove vig)
            raw_total = 1/avg_home + 1/avg_away + (1/avg_draw if avg_draw else 0)
            home_prob = (1/avg_home) / raw_total
            away_prob = (1/avg_away) / raw_total
            draw_prob = (1/avg_draw) / raw_total if avg_draw else 0

            key = f"{normalize_team(home)} vs {normalize_team(away)}"
            all_odds[key] = {
                'home': home, 'away': away,
                'home_prob': round(home_prob, 4),
                'away_prob': round(away_prob, 4),
                'draw_prob': round(draw_prob, 4),
                'commence': commence,
                'sport': sport,
                'bookmaker_count': len(bookmakers)
            }
        time.sleep(0.3)

    if all_odds:
        state['cached_odds'] = all_odds
        state['odds_last_fetched'] = now
        print(f"Fetched {len(all_odds)} events from {fetched_sports} sports")
    return state.get('cached_odds', {})


def find_bookmaker_edge(market, odds_cache):
    """Match a Polymarket market to bookmaker odds and calculate real edge.
    Returns (side, our_prob, market_price, edge, signal_str) or None."""
    if not odds_cache:
        return None

    try:
        prices = json.loads(market.get('outcomePrices', '[]'))
        yes_p = float(prices[0])
        no_p = float(prices[1])
    except:
        return None

    question = market.get('question', '')
    slug = market.get('slug', '')
    poly_text = f"{question} {slug}".lower()

    for key, odds in odds_cache.items():
        home = odds['home']
        away = odds['away']

        # Try to match this Polymarket market to this bookmaker event
        home_in = team_match(poly_text, home)
        away_in = team_match(poly_text, away)

        if not (home_in or away_in):
            continue

        # Determine which team the Polymarket question is about
        q_lower = question.lower()
        is_draw_market = 'draw' in q_lower
        is_over_under = any(x in q_lower for x in ['o/u', 'over', 'under', 'total'])

        if is_over_under:
            continue  # odds-api h2h doesn't cover totals

        if is_draw_market:
            bookie_prob = odds['draw_prob']
            if bookie_prob <= 0:
                continue
            # For "will it draw? NO" — poly NO price vs (1 - draw_prob)
            poly_no = no_p
            real_no_prob = 1 - bookie_prob
            edge_no = real_no_prob - poly_no
            if edge_no > 0.03 and 0.20 < poly_no < 0.80:
                return ('NO', real_no_prob, poly_no, edge_no, f'bookie:draw={bookie_prob:.0%}')
            # Check YES side too
            edge_yes = bookie_prob - yes_p
            if edge_yes > 0.03 and 0.20 < yes_p < 0.80:
                return ('YES', bookie_prob, yes_p, edge_yes, f'bookie:draw={bookie_prob:.0%}')
            continue

        # Moneyline: "Will [team] win?"
        # Figure out which team the question is about
        target_prob = None
        if home_in and 'win' in q_lower:
            # Check if question is about home team winning
            if team_match(q_lower.split('win')[0], home):
                target_prob = odds['home_prob']
            elif team_match(q_lower.split('win')[0], away):
                target_prob = odds['away_prob']
        if target_prob is None and away_in and 'win' in q_lower:
            if team_match(q_lower.split('win')[0], away):
                target_prob = odds['away_prob']
            elif team_match(q_lower.split('win')[0], home):
                target_prob = odds['home_prob']

        # Fallback: if only one team matches, assume it's about that team
        if target_prob is None:
            if home_in and not away_in:
                target_prob = odds['home_prob']
            elif away_in and not home_in:
                target_prob = odds['away_prob']

        if target_prob is None:
            continue

        # Compare bookmaker probability vs Polymarket price
        # YES side: bookie says team wins with prob X, Polymarket prices YES at yes_p
        edge_yes = target_prob - yes_p
        # NO side: bookie says team loses with prob (1-X), Polymarket prices NO at no_p
        edge_no = (1 - target_prob) - no_p

        best_side = None
        best_edge = 0
        best_price = 0
        best_prob = 0

        if edge_yes > best_edge and 0.20 < yes_p < 0.80:
            best_side = 'YES'
            best_edge = edge_yes
            best_price = yes_p
            best_prob = target_prob

        if edge_no > best_edge and 0.20 < no_p < 0.80:
            best_side = 'NO'
            best_edge = edge_no
            best_price = no_p
            best_prob = 1 - target_prob

        if best_side and best_edge >= 0.03:
            sig = f'bookie:{target_prob:.0%}vs{best_price:.0%}'
            return (best_side, best_prob, best_price, best_edge, sig)

    return None


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


# ===== [3] ADVANCED SELF-LEARNING =====

def get_category_weights(portfolio):
    """Advanced category learning with continuous scaling, profit-weighting, and time decay.
    Recent results matter more than old ones."""
    closed = portfolio.get('closed_positions', [])
    if not closed:
        return {}

    now_ts = time.time()
    cats = {}

    for pos in closed:
        cat = classify_bet(pos.get('question', ''), pos.get('slug', ''))
        if cat not in cats:
            cats[cat] = {'weighted_wins': 0, 'weighted_losses': 0, 'total_profit': 0, 'count': 0}

        # Time decay: recent trades weighted more (half-life = 3 days)
        closed_at = pos.get('closed_at', '')
        try:
            if 'T' in closed_at:
                ct = datetime.datetime.fromisoformat(closed_at.replace('Z', '+00:00'))
                age_hours = (now_ts - ct.timestamp()) / 3600
            else:
                age_hours = 168  # default 1 week
        except:
            age_hours = 168

        decay = 0.5 ** (age_hours / 72)  # half-life 3 days

        result = pos.get('result', '')
        profit = pos.get('profit_nok', 0)

        if result in ('WIN', 'TAKE_PROFIT'):
            cats[cat]['weighted_wins'] += decay
        elif result in ('LOSS', 'STOP_LOSS'):
            cats[cat]['weighted_losses'] += decay

        cats[cat]['total_profit'] += profit * decay
        cats[cat]['count'] += 1

    weights = {}
    for cat, data in cats.items():
        total = data['weighted_wins'] + data['weighted_losses']
        if total < 1.5:  # need ~2 recent trades minimum
            weights[cat] = 0
            continue

        win_rate = data['weighted_wins'] / total
        avg_profit = data['total_profit'] / total

        # Continuous scaling: 50% = 0, 60% = +0.02, 70% = +0.04, 80% = +0.06
        # Below 50%: 40% = -0.02, 30% = -0.04
        edge_adj = (win_rate - 0.5) * 0.2

        # Profit bonus: if avg profit per trade is high, boost further
        if avg_profit > 50:
            edge_adj += 0.01
        elif avg_profit < -50:
            edge_adj -= 0.01

        # Cap at +/- 5%
        weights[cat] = max(-0.05, min(0.05, round(edge_adj, 3)))

    return weights


def get_learning_summary(portfolio):
    """Get a summary of what the bot has learned for logging."""
    weights = get_category_weights(portfolio)
    if not weights:
        return "No data yet"
    parts = []
    for cat, w in sorted(weights.items(), key=lambda x: x[1], reverse=True):
        if w != 0:
            parts.append(f"{cat}:{w:+.1%}")
    return ' | '.join(parts) if parts else "Neutral"


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


# ===== [MULTI-MARKET] CRYPTO MOMENTUM SIGNAL =====

def fetch_crypto_momentum():
    """Fetch BTC/ETH prices and calculate short-term momentum.
    Used as signal for crypto-related Polymarket bets."""
    try:
        btc = api_fetch('https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum,solana&vs_currencies=usd&include_24hr_change=true')
        if not btc:
            return {}
        momentum = {}
        for coin in ['bitcoin', 'ethereum', 'solana']:
            if coin in btc:
                change = btc[coin].get('usd_24h_change', 0)
                # Normalize: >3% = bullish, <-3% = bearish
                if change > 5:
                    momentum[coin] = 0.02
                elif change > 3:
                    momentum[coin] = 0.01
                elif change < -5:
                    momentum[coin] = -0.02
                elif change < -3:
                    momentum[coin] = -0.01
                else:
                    momentum[coin] = 0
        return momentum
    except:
        return {}


def crypto_signal_for_market(question, slug, crypto_momentum):
    """Check if this market is crypto-related and apply momentum signal."""
    q = (question or '').lower()
    s = (slug or '').lower()

    if not crypto_momentum:
        return 0

    # Map market keywords to coins
    if 'bitcoin' in q or 'btc' in q or 'btc' in s:
        return crypto_momentum.get('bitcoin', 0)
    elif 'ethereum' in q or 'eth' in q or 'eth-' in s:
        return crypto_momentum.get('ethereum', 0)
    elif 'solana' in q or 'sol' in q or 'sol-' in s:
        return crypto_momentum.get('solana', 0)
    elif 'crypto' in q:
        # General crypto sentiment = average
        vals = list(crypto_momentum.values())
        return sum(vals) / len(vals) if vals else 0

    return 0


# ===== [SMART ENTRIES] ORDERBOOK TIMING =====

def should_wait_for_better_entry(market, side):
    """Check orderbook for entry timing. Returns True if we should wait."""
    try:
        tokens = json.loads(market.get('clobTokenIds', '[]'))
        if not tokens:
            return False
        token_idx = 0 if side == 'YES' else 1
        if token_idx >= len(tokens):
            return False
        ob = fetch_orderbook(tokens[token_idx])
        if not ob:
            return False

        spread = ob['spread']
        imbalance = ob['imbalance']

        # Don't buy if spread is too wide (>4c) — wait for tighter market
        if spread > 0.04:
            return True

        # Don't buy if strong sell pressure (imbalance < -0.4)
        if imbalance < -0.4:
            return True

        return False
    except:
        return False


# ===== [4] EDGE ESTIMATION (SMART VERSION) =====

def estimate_edge_smart(market, portfolio, state, crypto_momentum=None, odds_cache=None):
    """Edge estimation: bookmaker odds first, then orderbook/line/category signals.
    Returns (side, our_prob, market_price, edge, signals_used) or None."""

    # [PRIORITY] Try bookmaker odds match first — this is real edge
    bookie_result = find_bookmaker_edge(market, odds_cache or {})
    if bookie_result:
        side, prob, price, edge, sig = bookie_result
        # Still check orderbook for entry timing signal
        ob_adj = orderbook_signal(market, side)
        signals = [sig]
        if ob_adj != 0:
            edge += ob_adj
            signals.append(f'ob:{ob_adj:.3f}')
        if edge >= 0.03:  # lower threshold for bookie-backed edges
            return (side, prob, price, edge, signals)
        return None
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

    # [FILTER] Skip high-price NO bets (>0.80) — bad risk/reward
    if no_p > 0.80 and yes_p < 0.20:
        return None
    if yes_p > 0.80 and no_p < 0.20:
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

    # [MULTI-MARKET] Crypto momentum signal
    crypto_adj = crypto_signal_for_market(question, slug, crypto_momentum or {})
    if crypto_adj != 0:
        best_edge += crypto_adj
        best_prob += crypto_adj
        signals.append(f'crypto:{crypto_adj:.3f}')

    # [4] Dead zone check: 45-55c requires extra edge
    if 0.45 <= best_price <= 0.55:
        in_deadzone = True
        if best_edge < 0.07:
            return None

    # Minimum edge threshold (5% required)
    if best_edge < 0.05:
        return None

    # [1] Orderbook analysis (only for candidates that pass base filter)
    ob_adj = orderbook_signal(market, best_side)
    if ob_adj != 0:
        best_edge += ob_adj
        best_prob += ob_adj
        signals.append(f'ob:{ob_adj:.3f}')

    # Re-check after orderbook adjustment
    min_edge = 0.07 if in_deadzone else 0.05
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
            else:
                pos['result'] = 'LOSS'
                pos['profit_nok'] = -cost_nok
                stats['total_losses'] = stats.get('total_losses', 0) + 1
                monthly['losses_this_month'] = monthly.get('losses_this_month', 0) + 1

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

def hours_until_resolution(market):
    """Estimate hours until market resolves."""
    end_date = market.get('endDate', market.get('end_date_iso', ''))[:10]
    if not end_date:
        return 999
    try:
        end_dt = datetime.datetime.strptime(end_date, '%Y-%m-%d')
        end_dt = end_dt.replace(hour=23, minute=59, tzinfo=datetime.timezone.utc)
        hours = (end_dt - datetime.datetime.now(datetime.timezone.utc)).total_seconds() / 3600
        return max(0, hours)
    except:
        return 999


def bot1_scan_and_trade(portfolio, rate, state):
    if not portfolio:
        return False

    account = portfolio.get('account', {})
    positions = portfolio.get('positions', [])
    balance = account.get('balance_nok', 0)
    total_cost = sum(p.get('cost_nok', 0) for p in positions)
    port_value = balance + total_cost

    if balance < port_value * 0.05:
        return False

    # Scan every 5 minutes (no throttle — GitHub Actions already runs every 5 min)

    if len(positions) >= 15:
        return False

    today = datetime.date.today()
    tomorrow = (today + datetime.timedelta(days=1)).isoformat()
    end_max_3d = (today + datetime.timedelta(days=3)).isoformat()

    # Fetch bookmaker odds (cached, refreshes every 30 min)
    odds_cache = fetch_bookmaker_odds(state)

    # PRIORITY 1: Same-day markets (resolve TODAY — fastest capital rotation)
    today_markets = fetch_markets({
        'active': 'true', 'closed': 'false',
        'limit': 100, 'order': 'volume', 'ascending': 'false',
        'liquidity_num_min': 500,
        'end_date_min': today.isoformat(),
        'end_date_max': today.isoformat()
    })
    time.sleep(0.5)

    # PRIORITY 2: Tomorrow markets
    tomorrow_markets = fetch_markets({
        'active': 'true', 'closed': 'false',
        'limit': 80, 'order': 'volume', 'ascending': 'false',
        'liquidity_num_min': 1000,
        'end_date_min': tomorrow,
        'end_date_max': tomorrow
    })
    time.sleep(0.5)

    # PRIORITY 3: Sports + general (1-3 days)
    sports = fetch_markets({
        'active': 'true', 'closed': 'false',
        'limit': 80, 'order': 'volume', 'ascending': 'false',
        'tag': 'sports',
        'liquidity_num_min': 500,
        'end_date_min': today.isoformat(),
        'end_date_max': end_max_3d
    })
    time.sleep(0.5)

    # PRIORITY 4: Newest markets (most likely mispriced)
    new_markets = fetch_markets({
        'active': 'true', 'closed': 'false',
        'limit': 50, 'order': 'startDate', 'ascending': 'false',
        'liquidity_num_min': 500,
        'end_date_min': today.isoformat(),
        'end_date_max': end_max_3d
    })
    time.sleep(0.5)

    seen_ids = set()
    all_markets = []
    for m in today_markets + tomorrow_markets + sports + new_markets:
        mid = m.get('id', '')
        if mid and mid not in seen_ids:
            seen_ids.add(mid)
            all_markets.append(m)

    existing_ids = set(p.get('market_id', '') for p in positions)
    candidates = [m for m in all_markets if str(m.get('id', '')) not in existing_ids]

    crypto_momentum = fetch_crypto_momentum()
    time.sleep(0.3)

    scored = []
    for m in candidates:
        result = estimate_edge_smart(m, portfolio, state, crypto_momentum, odds_cache)
        if not result:
            continue
        side, our_prob, market_price, edge, signals = result

        # Sort score: prioritize fast resolution + bookmaker edge
        hours = hours_until_resolution(m)
        has_bookie = any('bookie' in s for s in signals)

        # Speed bonus: same-day gets big boost, tomorrow gets small boost
        speed_bonus = 0
        if hours <= 12:
            speed_bonus = 0.04   # resolves within 12h — huge bonus
        elif hours <= 24:
            speed_bonus = 0.02   # resolves within 24h
        elif hours <= 36:
            speed_bonus = 0.01   # resolves tomorrow

        bookie_bonus = 0.02 if has_bookie else 0
        sort_edge = edge + speed_bonus + bookie_bonus

        scored.append((sort_edge, edge, m, side, our_prob, market_price, signals, hours))
        time.sleep(0.2)

    scored.sort(key=lambda x: x[0], reverse=True)

    trades_opened = 0
    changed = False
    next_id = portfolio.get('next_position_id', len(positions) + 1)

    for sort_edge, edge, m, side, our_prob, market_price, signals, hours in scored:
        if trades_opened >= 8:
            break
        if len(positions) >= 15:
            break
        if balance < 25:
            break

        # [SMART ENTRY] Check if orderbook suggests waiting
        if should_wait_for_better_entry(m, side):
            print(f"  Skipping {m.get('question','?')[:40]} — spread too wide or sell pressure")
            continue

        kelly_f = half_kelly(our_prob, market_price)
        if kelly_f <= 0:
            continue

        size_nok = int(min(kelly_f * port_value, port_value * 0.04, balance * 0.3))
        if size_nok < 20:
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
        hours_str = f"{int(hours)}h" if hours < 48 else f"{int(hours/24)}d"
        print(f"[NY TRADE] BOT1: {question} | {side} @ {price_cents}c | {size_nok} kr | Edge {edge_pct}% | {hours_str} | {sig_str}")
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


# ===== MAIN =====

def get_summary(portfolio):
    if not portfolio:
        return 0, 0, 0, 0, 0, 0
    positions = portfolio.get('positions', [])
    account = portfolio.get('account', {})
    stats = portfolio.get('statistics', {})
    balance = account.get('balance_nok', 0)
    invested = sum(p.get('cost_nok', 0) for p in positions)
    unrealized = sum(p.get('unrealized_pnl_nok', 0) for p in positions)
    total_value = balance + invested + unrealized
    wins = stats.get('total_wins', 0)
    losses = stats.get('total_losses', 0)
    return total_value, balance, invested, unrealized, wins, losses


def check_telegram_commands(bot1, state):
    """Check for /dashboard commands in Telegram and respond."""
    if not TOKEN or not CHAT_ID:
        return
    try:
        last_update_id = state.get('last_telegram_update_id', 0)
        url = f'https://api.telegram.org/bot{TOKEN}/getUpdates?offset={last_update_id + 1}&timeout=0&limit=10'
        data = api_fetch(url)
        if not data or not data.get('ok'):
            return
        results = data.get('result', [])
        for update in results:
            update_id = update.get('update_id', 0)
            state['last_telegram_update_id'] = max(state.get('last_telegram_update_id', 0), update_id)
            msg = update.get('message', {})
            text = (msg.get('text') or '').strip().lower()
            chat_id = str(msg.get('chat', {}).get('id', ''))
            if chat_id != CHAT_ID:
                continue
            if text == '/dashboard':
                send_dashboard_reply(bot1)
            elif text == '/positions':
                send_positions_reply(bot1)
            elif text == '/help':
                send_telegram("Kommandoer:\n/dashboard - Full oversikt\n/positions - Alle apne posisjoner\n/help - Vis kommandoer")
    except Exception as e:
        print(f"Telegram commands check error: {e}")


def send_dashboard_reply(bot1):
    """Send a formatted dashboard to Telegram."""
    v1, b1, i1, u1, w1, l1 = get_summary(bot1)
    n1 = len(bot1.get('positions', [])) if bot1 else 0
    r1 = (bot1 or {}).get('statistics', {}).get('total_realized_pnl_nok', 0)
    wr1 = round(w1 / (w1 + l1) * 100) if (w1 + l1) > 0 else 0
    start = (bot1 or {}).get('account', {}).get('starting_capital', 1000)
    ath = (bot1 or {}).get('telegram', {}).get('ath_value_nok', start)

    lines = [
        "POLYMARKET DASHBOARD",
        "",
        f"Total verdi: {v1} kr",
        f"ATH: {ath} kr",
        f"Ledig: {b1} kr | Investert: {i1} kr",
        f"Urealisert P&L: {u1:+d} kr",
        f"Realisert P&L: {r1:+.0f} kr",
        f"Posisjoner: {n1} | W{w1}/L{l1} ({wr1}%)",
        f"USD/NOK: {USD_NOK:.2f}",
    ]
    send_telegram('\n'.join(lines))


def send_positions_reply(bot1):
    """Send all open positions to Telegram."""
    lines = ["APNE POSISJONER", ""]
    positions = (bot1 or {}).get('positions', [])
    if not positions:
        lines.append("Ingen posisjoner")
    else:
        lines.append(f"--- {len(positions)} stk ---")
        for p in positions:
            q = p.get('question', '?')[:40]
            side = p.get('side', '?')
            entry = p.get('entry_price_usd', 0)
            curr = p.get('current_price_usd', entry)
            pnl = p.get('unrealized_pnl_nok', 0)
            end = p.get('end_date', '?')
            lines.append(f"{side} {q}")
            lines.append(f"  Inn: ${entry:.3f} Na: ${curr:.3f} P&L: {pnl:+.0f} kr | {end}")
    send_telegram('\n'.join(lines))


def check_and_send_ath(bot1):
    """Send Telegram only when bot1 hits a new all-time high value."""
    if not bot1:
        return
    v1, b1, i1, u1, w1, l1 = get_summary(bot1)
    telegram_cfg = bot1.get('telegram', {})
    ath = telegram_cfg.get('ath_value_nok', bot1.get('account', {}).get('starting_capital', 1000))

    if v1 > ath:
        old_ath = ath
        telegram_cfg['ath_value_nok'] = v1
        bot1['telegram'] = telegram_cfg

        gain_from_start = v1 - bot1.get('account', {}).get('starting_capital', 1000)
        gain_pct = (v1 / bot1.get('account', {}).get('starting_capital', 1000) - 1) * 100
        wr = round(w1 / (w1 + l1) * 100) if (w1 + l1) > 0 else 0

        msg = (f"NY ATH! {v1} kr (forrige: {old_ath} kr)\n"
               f"Avkastning: +{gain_from_start:.0f} kr ({gain_pct:.1f}%)\n"
               f"W{w1}/L{l1} ({wr}%)\n"
               f"Posisjoner: {len(bot1.get('positions', []))}")
        send_telegram(msg)
        print(f"ATH alert sent: {v1} kr")
    else:
        print(f"No ATH (current: {v1}, ath: {ath})")


def main():
    print(f"Bot engine v2 starting at {datetime.datetime.now(datetime.timezone.utc).isoformat()}")

    state = load_state()
    if 'price_snapshots' not in state:
        state['price_snapshots'] = {}
    if 'category_stats' not in state:
        state['category_stats'] = {}

    rate = fetch_usd_nok()
    print(f"USD/NOK rate: {rate}")

    bot1 = load_json(BOT1_FILE)

    if not bot1:
        print("ERROR: Could not load Bot1 portfolio")

    # 0. Check Telegram commands (/dashboard, /positions, /help)
    check_telegram_commands(bot1, state)

    # 1. Resolve positions
    c1 = check_and_resolve(bot1, 'BOT1', rate)
    print(f"Resolve: bot1={c1}")

    # Store learning data in portfolio for dashboard
    if bot1:
        weights = get_category_weights(bot1)
        bot1['learning'] = {'category_weights': weights, 'summary': get_learning_summary(bot1)}
        print(f"Bot1 learning: {get_learning_summary(bot1)}")

    # 2. Bot 1: smart scan and trade
    c3 = bot1_scan_and_trade(bot1, rate, state)
    print(f"Bot1 scan: {c3}")

    # 3. Save
    if bot1 and (c1 or c3):
        save_json(BOT1_FILE, bot1)
        print("Saved Bot1 portfolio")

    # 4. ATH check — only send Telegram when new all-time high
    check_and_send_ath(bot1)

    # Always save after ATH check (ath_value_nok may have updated)
    if bot1:
        save_json(BOT1_FILE, bot1)

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
