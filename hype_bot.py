#!/usr/bin/env python3
"""
Polymarket Hype Pair Market-Making Bot
- Scans Hype pair prices every 10 seconds
- Places limit orders at (bid + $0.01) for both UP and DOWN
- 20 shares per side
- Places orders from 0:00 to 4:30 minutes of window
- Cancels unfilled orders at window end
- Demo mode: $1000 balance
- Beautiful web dashboard
"""

import os
import sys
import json
import time
import requests
import threading
from datetime import datetime
from collections import defaultdict
from flask import Flask, jsonify, render_template_string
from decimal import Decimal

# ============================================================================
# CONFIGURATION
# ============================================================================

DEMO_MODE = True
INITIAL_BALANCE = 1000.0
SHARES_PER_SIDE = 20
ORDER_OFFSET = 0.01  # Place orders at bid + $0.01
PLACE_ORDER_INTERVAL = 10  # seconds between placing new orders
CUTOFF_TIME = 270  # 4:30 minutes = 270 seconds (stop placing orders)
WINDOW_DURATION = 300  # 5 minutes = 300 seconds

GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_PRICE_API = "https://clob.polymarket.com/price"
CLOB_ORDER_API = "https://clob.polymarket.com/orders"

# ============================================================================
# BOT STATE
# ============================================================================

class BotState:
    def __init__(self):
        self.balance = INITIAL_BALANCE
        self.active_orders = {}  # order_id -> order_details
        self.filled_trades = []  # completed trades with P&L
        self.market_data = {
            'hype': {
                'up_bid': None,
                'down_bid': None,
                'up_ask': None,
                'down_ask': None,
                'token_ids': {}
            }
        }
        self.window_info = {
            'start_time': None,
            'current_window_ts': None,
            'time_in_window': 0,
            'status': 'loading'  # loading, ready, trading, ended
        }
        self.lock = threading.Lock()
    
    def to_dict(self):
        with self.lock:
            return {
                'balance': round(self.balance, 2),
                'active_orders': self.active_orders,
                'filled_trades': self.filled_trades,
                'market_data': self.market_data,
                'window_info': self.window_info,
                'stats': self.get_stats()
            }
    
    def get_stats(self):
        total_trades = len(self.filled_trades)
        if total_trades == 0:
            return {
                'total_trades': 0,
                'win_rate': 0,
                'total_pnl': 0,
                'roi_percent': 0,
                'wins': 0,
                'losses': 0
            }
        
        wins = sum(1 for t in self.filled_trades if t['pnl'] > 0)
        losses = total_trades - wins
        total_pnl = sum(t['pnl'] for t in self.filled_trades)
        roi = (total_pnl / INITIAL_BALANCE) * 100 if INITIAL_BALANCE > 0 else 0
        
        return {
            'total_trades': total_trades,
            'win_rate': round((wins / total_trades) * 100, 1) if total_trades > 0 else 0,
            'total_pnl': round(total_pnl, 2),
            'roi_percent': round(roi, 2),
            'wins': wins,
            'losses': losses
        }

bot_state = BotState()

# ============================================================================
# MARKET DISCOVERY - HYPE PAIR
# ============================================================================

def discover_hype_market():
    """
    Discover current Hype market using dual approach:
    1. Try /events endpoint with active=true
    2. Fall back to /markets endpoint
    
    Returns market data with token IDs and current prices
    """
    # Try to find any active Hype market
    print("[DISCOVERY] Searching for Hype pair market...")
    
    market = None
    
    # ===== APPROACH 1: Try /events endpoint =====
    try:
        response = requests.get(
            f"{GAMMA_API_BASE}/events",
            params={'search': 'hype', 'active': 'true'},
            timeout=10
        )
        data = response.json()
        
        if isinstance(data, list) and len(data) > 0:
            # Look for Hype market with UP/DOWN outcomes
            for event in data:
                if event.get('markets') and len(event['markets']) > 0:
                    for m in event['markets']:
                        outcomes_raw = m.get('outcomes', [])
                        if isinstance(outcomes_raw, str):
                            try:
                                outcomes = json.loads(outcomes_raw)
                            except:
                                outcomes = outcomes_raw
                        else:
                            outcomes = outcomes_raw
                        
                        # Check if it's UP/DOWN market
                        has_up = any('up' in str(o).lower() for o in outcomes)
                        has_down = any('down' in str(o).lower() for o in outcomes)
                        
                        if has_up and has_down:
                            market = m
                            print("[DISCOVERY] ✓ Found via /events endpoint")
                            break
                if market:
                    break
    except Exception as e:
        print(f"[DISCOVERY] /events search failed: {str(e)[:50]}")
    
    # ===== APPROACH 2: Try /markets endpoint with hype search =====
    if not market:
        try:
            response = requests.get(
                f"{GAMMA_API_BASE}/markets",
                params={'search': 'hype'},
                timeout=10
            )
            data = response.json()
            
            if isinstance(data, list) and len(data) > 0:
                market = data[0]
                print("[DISCOVERY] ✓ Found via /markets endpoint")
        except Exception as e:
            print(f"[DISCOVERY] /markets search failed: {str(e)[:50]}")
    
    if not market:
        print("[DISCOVERY] ✗ No Hype market found")
        return None
    
    # ===== PARSE MARKET DATA =====
    try:
        # Parse token IDs
        token_ids_raw = market.get('clobTokenIds') or market.get('clob_token_ids')
        if isinstance(token_ids_raw, str):
            token_ids_list = json.loads(token_ids_raw)
        else:
            token_ids_list = token_ids_raw or []
        
        # Parse outcomes
        outcomes_raw = market.get('outcomes', ['Up', 'Down'])
        if isinstance(outcomes_raw, str):
            outcomes = json.loads(outcomes_raw)
        else:
            outcomes = outcomes_raw or ['Up', 'Down']
        
        # Find up/down indices
        up_idx = next((i for i, o in enumerate(outcomes) if 'up' in str(o).lower()), 0)
        dn_idx = next((i for i, o in enumerate(outcomes) if 'down' in str(o).lower()), 1)
        
        token_up = token_ids_list[up_idx] if up_idx < len(token_ids_list) else None
        token_down = token_ids_list[dn_idx] if dn_idx < len(token_ids_list) else None
        
        if not token_up or not token_down:
            print("[DISCOVERY] ✗ Missing token IDs")
            return None
        
        result = {
            'slug': market.get('slug', 'hype-market'),
            'token_ids': {'up': token_up, 'down': token_down},
            'outcomes': [outcomes[up_idx], outcomes[dn_idx]],
            'market_id': market.get('id'),
            'end_date': market.get('endDate')
        }
        
        print(f"[DISCOVERY] ✓ Found Hype market!")
        print(f"  UP Token: {token_up[:16]}...")
        print(f"  DOWN Token: {token_down[:16]}...")
        
        return result
    
    except Exception as e:
        print(f"[DISCOVERY] ✗ Error parsing market: {e}")
        import traceback
        traceback.print_exc()
        return None

# ============================================================================
# PRICE FETCHING (Live BID/ASK prices)
# ============================================================================

def get_bid_ask_prices(asset_side, token_id):
    """
    Fetch live BID and ASK prices for a token
    Returns: {'bid': float, 'ask': float} or None
    """
    try:
        # Fetch BID price
        resp_bid = requests.get(
            f"{CLOB_PRICE_API}?token_id={token_id}&side=BUY",
            timeout=5
        )
        bid_price = float(resp_bid.json().get('price', 0))
        
        # Fetch ASK price
        resp_ask = requests.get(
            f"{CLOB_PRICE_API}?token_id={token_id}&side=SELL",
            timeout=5
        )
        ask_price = float(resp_ask.json().get('price', 0))
        
        return {'bid': bid_price, 'ask': ask_price}
    
    except Exception as e:
        print(f"[PRICES] Error fetching prices for {token_id[:16]}...: {e}")
        return None

# ============================================================================
# WINDOW & TIMING
# ============================================================================

def get_current_window_ts():
    """Get current 5-minute window timestamp"""
    return (int(time.time()) // 300) * 300

def get_window_time_elapsed():
    """Get time elapsed in current window (in seconds)"""
    if not bot_state.window_info['start_time']:
        return 0
    return time.time() - bot_state.window_info['start_time']

def should_place_orders():
    """Check if we should place new orders (within first 4:30)"""
    elapsed = get_window_time_elapsed()
    return 0 <= elapsed < CUTOFF_TIME

def is_window_ended():
    """Check if window has ended"""
    elapsed = get_window_time_elapsed()
    return elapsed >= WINDOW_DURATION

# ============================================================================
# ORDER MANAGEMENT
# ============================================================================

def create_order_id(side, timestamp):
    """Create unique order ID"""
    return f"hype_{side}_{int(timestamp * 1000)}"

def place_orders():
    """
    Place limit orders at both UP and DOWN sides
    UP: current UP bid + $0.01
    DOWN: current DOWN bid + $0.01
    """
    if not should_place_orders():
        return
    
    with bot_state.lock:
        elapsed = get_window_time_elapsed()
        up_bid = bot_state.market_data['hype']['up_bid']
        dn_bid = bot_state.market_data['hype']['down_bid']
        
        if up_bid is None or dn_bid is None:
            print(f"[ORDERS] Cannot place orders - missing prices")
            return
        
        # Calculate order prices
        up_order_price = up_bid + ORDER_OFFSET
        dn_order_price = dn_bid + ORDER_OFFSET
        
        # Calculate total cost
        total_cost = (up_order_price * SHARES_PER_SIDE) + (dn_order_price * SHARES_PER_SIDE)
        
        if bot_state.balance < total_cost:
            print(f"[ORDERS] Insufficient balance: ${bot_state.balance:.2f} < ${total_cost:.2f}")
            return
        
        # Create order IDs
        up_order_id = create_order_id('up', time.time())
        dn_order_id = create_order_id('dn', time.time())
        
        # Create orders
        up_order = {
            'id': up_order_id,
            'side': 'UP',
            'price': up_order_price,
            'shares': SHARES_PER_SIDE,
            'cost': up_order_price * SHARES_PER_SIDE,
            'status': 'placed',
            'placed_at': datetime.now().isoformat(),
            'filled_at': None,
            'filled_price': None
        }
        
        dn_order = {
            'id': dn_order_id,
            'side': 'DOWN',
            'price': dn_order_price,
            'shares': SHARES_PER_SIDE,
            'cost': dn_order_price * SHARES_PER_SIDE,
            'status': 'placed',
            'placed_at': datetime.now().isoformat(),
            'filled_at': None,
            'filled_price': None
        }
        
        # Add to active orders
        bot_state.active_orders[up_order_id] = up_order
        bot_state.active_orders[dn_order_id] = dn_order
        
        # Deduct cost from balance (reserved)
        bot_state.balance -= total_cost
        
        print(f"[ORDERS] ✓ Placed orders at {elapsed:.1f}s")
        print(f"  UP: {SHARES_PER_SIDE} @ ${up_order_price:.4f} = ${up_order['cost']:.2f}")
        print(f"  DOWN: {SHARES_PER_SIDE} @ ${dn_order_price:.4f} = ${dn_order['cost']:.2f}")
        print(f"  Total cost: ${total_cost:.2f} | Balance: ${bot_state.balance:.2f}")

def simulate_order_fills():
    """
    Simulate order fills (in demo mode)
    In real mode, would connect to WebSocket for live fills
    """
    with bot_state.lock:
        for order_id, order in list(bot_state.active_orders.items()):
            if order['status'] == 'placed':
                # Simulate fill after a small delay
                if time.time() - time.mktime(datetime.fromisoformat(order['placed_at']).timetuple()) > 2:
                    # Simulate fill at slightly better price
                    if order['side'] == 'UP':
                        fill_price = bot_state.market_data['hype']['up_bid'] + ORDER_OFFSET
                    else:
                        fill_price = bot_state.market_data['hype']['down_bid'] + ORDER_OFFSET
                    
                    # Mark as filled (in demo, assume 30% chance to fill)
                    import random
                    if random.random() < 0.3:
                        order['status'] = 'filled'
                        order['filled_price'] = fill_price
                        order['filled_at'] = datetime.now().isoformat()

def cancel_unfilled_orders():
    """Cancel all unfilled orders at window end"""
    with bot_state.lock:
        cancelled_count = 0
        for order_id, order in list(bot_state.active_orders.items()):
            if order['status'] == 'placed':
                order['status'] = 'cancelled'
                # Refund the cost
                bot_state.balance += order['cost']
                cancelled_count += 1
        
        if cancelled_count > 0:
            print(f"[ORDERS] ✓ Cancelled {cancelled_count} unfilled orders at window end")
            print(f"  Balance refunded: ${sum(o['cost'] for o in bot_state.active_orders.values() if o['status'] == 'cancelled'):.2f}")

def settle_filled_orders():
    """
    Process filled orders and calculate P&L
    Move filled orders to trade history
    """
    with bot_state.lock:
        filled_orders = [o for o in bot_state.active_orders.values() if o['status'] == 'filled']
        
        for order in filled_orders:
            # Check if we have a pair (UP and DOWN filled together)
            side = 'UP' if order['side'] == 'UP' else 'DOWN'
            
            # For now, just track as individual fills
            trade = {
                'id': order['id'],
                'side': order['side'],
                'shares': order['shares'],
                'entry_price': order['price'],
                'filled_price': order['filled_price'],
                'entry_cost': order['cost'],
                'filled_at': order['filled_at'],
                'pnl': round((order['filled_price'] - order['price']) * order['shares'], 4)
            }
            
            bot_state.filled_trades.append(trade)
            bot_state.balance += (order['filled_price'] * order['shares'])
            
            print(f"[TRADES] ✓ {side} order filled")
            print(f"  Shares: {order['shares']} @ ${order['filled_price']:.4f}")
            print(f"  P&L: ${trade['pnl']:.2f}")

# ============================================================================
# MAIN TRADING LOOP
# ============================================================================

def run_trading_loop():
    """
    Main trading loop:
    1. Discover Hype market
    2. Start window timer
    3. Every 10 seconds: Fetch prices and place orders
    4. At 4:30: Stop placing orders
    5. At 5:00: Cancel unfilled orders, settle trades, restart
    """
    
    print("\n" + "="*70)
    print("STARTING HYPE PAIR MARKET-MAKING BOT")
    print("="*70)
    
    while True:
        try:
            # === MARKET DISCOVERY ===
            if not bot_state.market_data['hype']['token_ids']:
                hype_market = discover_hype_market()
                if not hype_market:
                    print("[BOT] Hype market not found, retrying in 10 seconds...")
                    time.sleep(10)
                    continue
                
                with bot_state.lock:
                    bot_state.market_data['hype']['token_ids'] = hype_market['token_ids']
                    bot_state.window_info['current_window_ts'] = get_current_window_ts()
                    bot_state.window_info['start_time'] = time.time()
                    bot_state.window_info['status'] = 'ready'
            
            # === WINDOW TIMING ===
            elapsed = get_window_time_elapsed()
            
            with bot_state.lock:
                bot_state.window_info['time_in_window'] = elapsed
            
            # === CHECK IF WINDOW ENDED ===
            if is_window_ended():
                print(f"\n[WINDOW] Window ended at {elapsed:.1f}s")
                cancel_unfilled_orders()
                settle_filled_orders()
                
                # Reset for next window
                with bot_state.lock:
                    bot_state.window_info['start_time'] = time.time()
                    bot_state.active_orders = {}
                    bot_state.window_info['status'] = 'ready'
                
                continue
            
            # === FETCH PRICES ===
            up_token = bot_state.market_data['hype']['token_ids']['up']
            dn_token = bot_state.market_data['hype']['token_ids']['down']
            
            up_prices = get_bid_ask_prices('up', up_token)
            dn_prices = get_bid_ask_prices('dn', dn_token)
            
            if up_prices and dn_prices:
                with bot_state.lock:
                    bot_state.market_data['hype']['up_bid'] = up_prices['bid']
                    bot_state.market_data['hype']['up_ask'] = up_prices['ask']
                    bot_state.market_data['hype']['down_bid'] = dn_prices['bid']
                    bot_state.market_data['hype']['down_ask'] = dn_prices['ask']
                
                print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Window: {elapsed:.1f}s")
                print(f"[PRICES] UP bid: ${up_prices['bid']:.4f} | DOWN bid: ${dn_prices['bid']:.4f}")
            
            # === PLACE ORDERS (every 10 seconds) ===
            place_orders()
            
            # === SIMULATE FILLS (demo mode) ===
            simulate_order_fills()
            
            # === SLEEP UNTIL NEXT ITERATION ===
            time.sleep(1)
        
        except KeyboardInterrupt:
            print("\n[BOT] Shutting down...")
            break
        except Exception as e:
            print(f"[ERROR] {e}")
            import traceback
            traceback.print_exc()
            time.sleep(2)

# ============================================================================
# WEB DASHBOARD
# ============================================================================

app = Flask(__name__)

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Hype Pair Market-Making Bot</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
            color: #e2e8f0;
            line-height: 1.6;
        }
        
        .container {
            max-width: 1400px;
            margin: 0 auto;
            padding: 20px;
        }
        
        header {
            text-align: center;
            margin-bottom: 30px;
            padding: 30px 0;
            border-bottom: 2px solid #334155;
        }
        
        h1 {
            font-size: 2.5em;
            margin-bottom: 10px;
            background: linear-gradient(135deg, #06b6d4 0%, #3b82f6 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        
        .status-badge {
            display: inline-block;
            padding: 8px 16px;
            background: #10b981;
            border-radius: 20px;
            font-size: 0.9em;
            margin-top: 10px;
        }
        
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        
        .card {
            background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
            border: 1px solid #334155;
            border-radius: 12px;
            padding: 20px;
            box-shadow: 0 4px 6px rgba(0, 0, 0, 0.3);
            transition: all 0.3s ease;
        }
        
        .card:hover {
            border-color: #64748b;
            box-shadow: 0 8px 12px rgba(0, 0, 0, 0.5);
        }
        
        .card h2 {
            font-size: 1.1em;
            margin-bottom: 15px;
            color: #cbd5e1;
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        
        .stat {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 10px 0;
            border-bottom: 1px solid #334155;
        }
        
        .stat:last-child {
            border-bottom: none;
        }
        
        .stat-label {
            color: #94a3b8;
        }
        
        .stat-value {
            font-size: 1.3em;
            font-weight: bold;
            color: #06b6d4;
        }
        
        .positive {
            color: #10b981;
        }
        
        .negative {
            color: #ef4444;
        }
        
        .price-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 15px;
        }
        
        .price-card {
            background: #0f172a;
            border: 1px solid #334155;
            padding: 12px;
            border-radius: 8px;
            text-align: center;
        }
        
        .price-card h3 {
            font-size: 0.9em;
            color: #94a3b8;
            margin-bottom: 8px;
        }
        
        .price {
            font-size: 1.5em;
            font-weight: bold;
            color: #06b6d4;
        }
        
        table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 15px;
        }
        
        th {
            background: #0f172a;
            padding: 12px;
            text-align: left;
            font-weight: 600;
            color: #cbd5e1;
            border-bottom: 2px solid #334155;
        }
        
        td {
            padding: 12px;
            border-bottom: 1px solid #334155;
        }
        
        tr:hover {
            background: rgba(6, 182, 212, 0.05);
        }
        
        .full-width {
            grid-column: 1 / -1;
        }
        
        .window-timer {
            font-size: 1.8em;
            font-weight: bold;
            color: #06b6d4;
            text-align: center;
            padding: 20px;
            background: #0f172a;
            border-radius: 8px;
            border: 1px solid #334155;
        }
        
        .refresh-time {
            color: #64748b;
            font-size: 0.85em;
            margin-top: 15px;
            text-align: center;
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>🤖 Hype Pair Market-Making Bot</h1>
            <div class="status-badge">🟢 LIVE TRADING</div>
            <p style="margin-top: 10px; color: #94a3b8;">Limit order market-making on Hype pair</p>
        </header>
        
        <div class="grid">
            <!-- Window Timer -->
            <div class="card full-width">
                <h2>⏱️ Window Timer</h2>
                <div class="window-timer" id="window-timer">0:00 / 5:00</div>
            </div>
            
            <!-- Account Card -->
            <div class="card">
                <h2>💰 Account</h2>
                <div class="stat">
                    <span class="stat-label">Balance</span>
                    <span class="stat-value" id="balance">$1,000.00</span>
                </div>
                <div class="stat">
                    <span class="stat-label">Total P&L</span>
                    <span class="stat-value" id="total-pnl">$0.00</span>
                </div>
                <div class="stat">
                    <span class="stat-label">ROI</span>
                    <span class="stat-value" id="roi">0.00%</span>
                </div>
            </div>
            
            <!-- Trade Stats -->
            <div class="card">
                <h2>📊 Statistics</h2>
                <div class="stat">
                    <span class="stat-label">Total Trades</span>
                    <span class="stat-value" id="total-trades">0</span>
                </div>
                <div class="stat">
                    <span class="stat-label">Win Rate</span>
                    <span class="stat-value" id="win-rate">0%</span>
                </div>
                <div class="stat">
                    <span class="stat-label">Wins / Losses</span>
                    <span class="stat-value" id="win-loss">0 / 0</span>
                </div>
            </div>
            
            <!-- Active Orders -->
            <div class="card">
                <h2>📋 Active Orders</h2>
                <div id="active-orders" style="color: #64748b;">
                    <p>No active orders</p>
                </div>
            </div>
            
            <!-- Hype Prices -->
            <div class="card">
                <h2>🔥 Hype Pair Prices</h2>
                <div class="price-grid">
                    <div class="price-card">
                        <h3>UP BID</h3>
                        <div class="price" id="up-bid">—</div>
                    </div>
                    <div class="price-card">
                        <h3>DOWN BID</h3>
                        <div class="price" id="down-bid">—</div>
                    </div>
                    <div class="price-card">
                        <h3>UP ASK</h3>
                        <div class="price" id="up-ask">—</div>
                    </div>
                    <div class="price-card">
                        <h3>DOWN ASK</h3>
                        <div class="price" id="down-ask">—</div>
                    </div>
                </div>
            </div>
            
            <!-- Order Prices -->
            <div class="card">
                <h2>🎯 Order Prices (bid + $0.01)</h2>
                <div class="stat">
                    <span class="stat-label">UP Order Price</span>
                    <span class="stat-value" id="up-order-price">—</span>
                </div>
                <div class="stat">
                    <span class="stat-label">DOWN Order Price</span>
                    <span class="stat-value" id="down-order-price">—</span>
                </div>
            </div>
            
            <!-- Trade History -->
            <div class="card full-width">
                <h2>📈 Filled Trades</h2>
                <table id="trade-history">
                    <thead>
                        <tr>
                            <th>Time</th>
                            <th>Side</th>
                            <th>Shares</th>
                            <th>Entry Price</th>
                            <th>Filled Price</th>
                            <th>P&L</th>
                        </tr>
                    </thead>
                    <tbody id="trade-rows">
                        <tr><td colspan="6" style="text-align: center; color: #64748b;">No filled trades yet</td></tr>
                    </tbody>
                </table>
            </div>
        </div>
        
        <div class="refresh-time">
            Last updated: <span id="last-update">—</span> | Refreshing every 1 second
        </div>
    </div>
    
    <script>
        async function fetchData() {
            try {
                const response = await fetch('/api/state');
                const data = await response.json();
                
                // Update account
                document.getElementById('balance').textContent = '$' + data.balance.toFixed(2);
                
                // Update stats
                const stats = data.stats;
                document.getElementById('total-pnl').textContent = 
                    (stats.total_pnl >= 0 ? '+' : '') + '$' + stats.total_pnl.toFixed(2);
                document.getElementById('total-pnl').classList.toggle('positive', stats.total_pnl >= 0);
                document.getElementById('total-pnl').classList.toggle('negative', stats.total_pnl < 0);
                
                document.getElementById('roi').textContent = 
                    (stats.roi_percent >= 0 ? '+' : '') + stats.roi_percent.toFixed(2) + '%';
                document.getElementById('roi').classList.toggle('positive', stats.roi_percent >= 0);
                
                document.getElementById('total-trades').textContent = stats.total_trades;
                document.getElementById('win-rate').textContent = stats.win_rate.toFixed(1) + '%';
                document.getElementById('win-loss').textContent = stats.wins + ' / ' + stats.losses;
                
                // Update window timer
                const timeElapsed = data.window_info.time_in_window;
                const minutes = Math.floor(timeElapsed / 60);
                const seconds = Math.floor(timeElapsed % 60);
                document.getElementById('window-timer').textContent = 
                    minutes.toString().padStart(1, '0') + ':' + seconds.toString().padStart(2, '0') + ' / 5:00';
                
                // Update prices
                const md = data.market_data;
                if (md.hype.up_bid !== null) {
                    document.getElementById('up-bid').textContent = '$' + md.hype.up_bid.toFixed(4);
                    document.getElementById('up-ask').textContent = '$' + md.hype.up_ask.toFixed(4);
                    document.getElementById('down-bid').textContent = '$' + md.hype.down_bid.toFixed(4);
                    document.getElementById('down-ask').textContent = '$' + md.hype.down_ask.toFixed(4);
                    
                    // Update order prices
                    document.getElementById('up-order-price').textContent = 
                        '$' + (md.hype.up_bid + 0.01).toFixed(4);
                    document.getElementById('down-order-price').textContent = 
                        '$' + (md.hype.down_bid + 0.01).toFixed(4);
                }
                
                // Update active orders
                const ordersContainer = document.getElementById('active-orders');
                const orders = Object.values(data.active_orders).filter(o => o.status === 'placed');
                if (orders.length === 0) {
                    ordersContainer.innerHTML = '<p style="color: #64748b;">No active orders</p>';
                } else {
                    ordersContainer.innerHTML = orders
                        .map(o => `
                            <div style="padding: 10px 0; border-bottom: 1px solid #334155;">
                                <strong>${o.side}</strong><br>
                                ${o.shares} shares @ $${o.price.toFixed(4)}<br>
                                <span style="color: #94a3b8;">Cost: $${o.cost.toFixed(2)}</span>
                            </div>
                        `).join('');
                }
                
                // Update trade history
                const tbody = document.getElementById('trade-rows');
                if (data.filled_trades.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: #64748b;">No filled trades yet</td></tr>';
                } else {
                    tbody.innerHTML = data.filled_trades
                        .slice(0, 20)  // Last 20 trades
                        .reverse()
                        .map(t => `
                            <tr>
                                <td>${new Date(t.filled_at).toLocaleTimeString()}</td>
                                <td><strong>${t.side}</strong></td>
                                <td>${t.shares}</td>
                                <td>$${t.entry_price.toFixed(4)}</td>
                                <td>$${t.filled_price.toFixed(4)}</td>
                                <td style="color: ${t.pnl >= 0 ? '#10b981' : '#ef4444'}">${t.pnl >= 0 ? '+' : ''}$${t.pnl.toFixed(4)}</td>
                            </tr>
                        `).join('');
                }
                
                document.getElementById('last-update').textContent = new Date().toLocaleTimeString();
            } catch (error) {
                console.error('Error fetching data:', error);
            }
        }
        
        // Initial fetch and then every 1 second
        fetchData();
        setInterval(fetchData, 1000);
    </script>
</body>
</html>
"""

@app.route('/')
def dashboard():
    return render_template_string(DASHBOARD_HTML)

@app.route('/api/state')
def api_state():
    return jsonify(bot_state.to_dict())

def run_dashboard(port=5000):
    """Run Flask app in background"""
    print(f"[DASHBOARD] Starting on http://localhost:{port}")
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

# ============================================================================
# MAIN
# ============================================================================

if __name__ == '__main__':
    # Start dashboard in background thread
    dashboard_thread = threading.Thread(
        target=run_dashboard,
        kwargs={'port': int(os.getenv('PORT', 5000))},
        daemon=True
    )
    dashboard_thread.start()
    
    # Start trading loop in main thread
    try:
        run_trading_loop()
    except KeyboardInterrupt:
        print("\n[BOT] Shutdown complete")
        sys.exit(0)
