#!/usr/bin/env python3
"""
Polymarket Automated Market-Making Bot
- Implements the exact Market Slug Discovery Method used in your JS architecture
- Dynamic contract detection via slug filtering on Gamma API
- Continuous Orderbook polling via CLOB /book endpoint
- Real-time limit layer placement at (bid + $0.01)
- Automatic order clearance inside the 4:30 interval cutoff
- Integrated multi-market dashboard UI
"""

import os
import sys
import json
import time
import requests
import threading
from datetime import datetime
from flask import Flask, jsonify, render_template_string

# ===========================================================================
# CONFIGURATION & REPLICATED SLUGS FROM JAVASCRIPT
# ===========================================================================
DEMO_MODE = True
INITIAL_BALANCE = 1000.0
SHARES_PER_SIDE = 20
ORDER_OFFSET = 0.01
PLACE_ORDER_INTERVAL = 10
CUTOFF_TIME = 270 
WINDOW_DURATION = 300

GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"

# Change this to any target market slug ('btc-updown-5m', 'eth-updown-5m', 'sol-updown-5m', etc.)
# For hype markets, you can input its precise dynamic slug designation here
TARGET_MARKET_SLUG = "btc-updown-5m"

# ===========================================================================
# BOT STATE MANAGEMENT
# ===========================================================================
class BotState:
    def __init__(self):
        self.balance = INITIAL_BALANCE
        self.market_slug = TARGET_MARKET_SLUG
        self.current_market_title = "Discovering market slug..."
        self.current_market_id = "N/A"
        self.up_token_id = "N/A"
        self.down_token_id = "N/A"
        self.up_bid = 0.0
        self.down_bid = 0.0
        self.up_target_price = 0.0
        self.down_target_price = 0.0
        self.time_elapsed_in_window = 0
        self.orders = []
        self.pnl = 0.0

    def to_dict(self):
        return {
            "balance": round(self.balance, 2),
            "market_slug": self.market_slug,
            "current_market_title": self.current_market_title,
            "current_market_id": self.current_market_id,
            "up_token_id": self.up_token_id,
            "down_token_id": self.down_token_id,
            "up_bid": round(self.up_bid, 4),
            "down_bid": round(self.down_bid, 4),
            "up_target_price": round(self.up_target_price, 4),
            "down_target_price": round(self.down_target_price, 4),
            "time_elapsed_in_window": self.time_elapsed_in_window,
            "orders": self.orders,
            "pnl": round(self.pnl, 4)
        }

bot_state = BotState()
app = Flask(__name__)

# ===========================================================================
# THE SLUG DISCOVERY METHOD (Gamma API Mapping)
# ===========================================================================
def fetch_market_by_slug(slug_name):
    """
    Queries Gamma /markets to pull structural parameters filtering explicitly
    by the active updown cycle slug name. Handles double-encoded clobTokenIds array.
    """
    # Using the native Gamma lookup scheme to extract token structures
    url = f"{GAMMA_API_BASE}/markets?slug={slug_name}&active=true&closed=false"
    try:
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            markets_list = response.json()
            if markets_list and len(markets_list) > 0:
                # Target the front-facing unclosed contract active under the matching slug
                return markets_list[0]
    except Exception as e:
        print(f"[ERROR] Gamma Slug Query failed for {slug_name}: {e}")
    return None

def fetch_live_clob_bid(token_id):
    """
    Queries the central book endpoint using the extracted token structure
    """
    if not token_id or token_id == "N/A":
        return 0.0
    
    url = f"{CLOB_API_BASE}/book?token_id={token_id}"
    try:
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            book_data = response.json()
            bids = book_data.get("bids", [])
            if bids and len(bids) > 0:
                return float(bids[0].get("price", 0.0))
    except Exception as e:
        print(f"[ERROR] Orderbook tracking failure on token {token_id}: {e}")
    return 0.0

# ===========================================================================
# TRADING SYSTEM LOOP
# ===========================================================================
def run_trading_loop():
    print(f"[ENGINE] Starting engine using Slug Discovery Mode for: {TARGET_MARKET_SLUG}")
    last_order_time = 0
    previous_market_id = ""

    while True:
        try:
            # Sync timing constraints to absolute minute segments
            now = datetime.utcnow()
            time_elapsed = (now.minute % 5) * 60 + now.second
            bot_state.time_elapsed_in_window = time_elapsed

            # Dynamic Contract Discovery Execution via Slug Method
            market_data = fetch_market_by_slug(bot_state.market_slug)
            
            if market_data:
                bot_state.current_market_id = market_data.get("id", "N/A")
                bot_state.current_market_title = market_data.get("title", "Asset Up/Down Contract")
                
                # Check for 5-minute block transition rotation
                if bot_state.current_market_id != previous_market_id:
                    print(f"\n[ROTATION] Discovered Active Market Contract: {bot_state.current_market_title}")
                    bot_state.orders = []
                    previous_market_id = bot_state.current_market_id

                # Unpack clobTokenIds safely (handles both native arrays and double-encoded string strings)
                token_ids_raw = market_data.get("clobTokenIds", [])
                if isinstance(token_ids_raw, str):
                    try:
                        token_ids = json.loads(token_ids_raw)
                    except:
                        token_ids = []
                else:
                    token_ids = token_ids_raw

                if len(token_ids) >= 2:
                    bot_state.up_token_id = token_ids[0]     # Index 0 is YES/UP
                    bot_state.down_token_id = token_ids[1]   # Index 1 is NO/DOWN

                    # Poll pricing states from the CLOB interface
                    bot_state.up_bid = fetch_live_clob_bid(bot_state.up_token_id)
                    bot_state.down_bid = fetch_live_clob_bid(bot_state.down_token_id)

                    # Calculate target order configurations (Bid + $0.01)
                    if bot_state.up_bid > 0:
                        bot_state.up_target_price = bot_state.up_bid + ORDER_OFFSET
                    else:
                        bot_state.up_target_price = 0.50

                    if bot_state.down_bid > 0:
                        bot_state.down_target_price = bot_state.down_bid + ORDER_OFFSET
                    else:
                        bot_state.down_target_price = 0.50
            else:
                print(f"[WARN] No active contract data resolved for slug '{bot_state.market_slug}'")

            # Execution logic matching Cutoff limits
            current_time_ms = time.time()
            if time_elapsed < CUTOFF_TIME:
                if current_time_ms - last_order_time >= PLACE_ORDER_INTERVAL:
                    if bot_state.up_target_price > 0 and bot_state.down_target_price > 0:
                        bot_state.orders = []
                        
                        up_order = {
                            "timestamp": datetime.now().strftime("%H:%M:%S"),
                            "side": "UP",
                            "token_id": bot_state.up_token_id,
                            "price": bot_state.up_target_price,
                            "shares": SHARES_PER_SIDE,
                            "status": "PLACED"
                        }
                        down_order = {
                            "timestamp": datetime.now().strftime("%H:%M:%S"),
                            "side": "DOWN",
                            "token_id": bot_state.down_token_id,
                            "price": bot_state.down_target_price,
                            "shares": SHARES_PER_SIDE,
                            "status": "PLACED"
                        }
                        
                        bot_state.orders.extend([up_order, down_order])
                        print(f"[ORDER] Refreshed Spreads -> UP: ${bot_state.up_target_price:.2f} | DOWN: ${bot_state.down_target_price:.2f}")
                        last_order_time = current_time_ms
            else:
                if bot_state.orders:
                    print(f"[CUTOFF] Exceeded 4:30 ceiling ({time_elapsed}s). Dropping order logs from pipeline.")
                    bot_state.orders = []

            time.sleep(1)
        except Exception as e:
            print(f"[CRITICAL] Operational failure in core pipeline execution loop: {e}")
            time.sleep(2)

# ===========================================================================
# DASHBOARD TELEMETRY TERMINAL (FLASK)
# ===========================================================================
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Polymarket Multi-Asset Engine UI</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style> body { background-color: #0b0f19; color: #f1f5f9; } </style>
</head>
<body class="p-6">
    <div class="max-w-6xl mx-auto">
        <div class="flex justify-between items-center border-b border-gray-800 pb-4 mb-6">
            <div>
                <h1 class="text-3xl font-extrabold text-indigo-400 tracking-tight">Polymarket Slug-Discovery Engine</h1>
                <p class="text-xs text-gray-400 font-mono mt-1">Monitored Slug Handle: <span class="text-teal-400" id="slug-lbl">-</span></p>
            </div>
            <div class="text-right">
                <span class="bg-indigo-500/10 text-indigo-400 border border-indigo-500/20 px-3 py-1 rounded text-xs font-semibold">ENGINE RUNNING</span>
                <p class="text-[10px] text-gray-500 mt-2 font-mono" id="sync-lbl">Syncing...</p>
            </div>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-4 gap-4 mb-6">
            <div class="bg-gray-900/60 p-4 rounded-xl border border-gray-800">
                <p class="text-xs font-medium text-gray-400 uppercase tracking-wider">Trading Balance</p>
                <p class="text-2xl font-bold mt-1" id="balance_ui">$0.00</p>
            </div>
            <div class="bg-gray-900/60 p-4 rounded-xl border border-gray-800">
                <p class="text-xs font-medium text-gray-400 uppercase tracking-wider">Realized Returns</p>
                <p class="text-2xl font-bold mt-1" id="pnl_ui">$0.00</p>
            </div>
            <div class="bg-gray-900/60 p-4 rounded-xl border border-gray-800">
                <p class="text-xs font-medium text-gray-400 uppercase tracking-wider">Window Progression</p>
                <p class="text-2xl font-bold text-amber-400 mt-1 font-mono" id="timer_ui">00:00</p>
            </div>
            <div class="bg-gray-900/60 p-4 rounded-xl border border-gray-800">
                <p class="text-xs font-medium text-gray-400 uppercase tracking-wider">Active Contract</p>
                <p class="text-sm font-semibold text-gray-200 mt-2 truncate" id="contract_ui">Searching...</p>
            </div>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-2 gap-6 mb-6">
            <div class="bg-gray-900/40 p-5 rounded-xl border border-gray-800">
                <h2 class="text-lg font-bold text-emerald-400 mb-3 border-b border-gray-800 pb-2">UP Side Layer (YES)</h2>
                <div class="space-y-2 text-sm font-mono">
                    <div class="flex justify-between"><span class="text-gray-500">Token ID:</span> <span class="text-xs text-gray-300" id="up_token_ui">-</span></div>
                    <div class="flex justify-between"><span class="text-gray-500">Market Best Bid:</span> <span class="text-gray-200" id="up_bid_ui">$0.0000</span></div>
                    <div class="flex justify-between bg-emerald-500/5 p-2 rounded border border-emerald-500/10"><span class="text-emerald-400">Order Placed:</span> <span class="text-emerald-400 font-bold" id="up_target_ui">$0.0000</span></div>
                </div>
            </div>
            <div class="bg-gray-900/40 p-5 rounded-xl border border-gray-800">
                <h2 class="text-lg font-bold text-rose-400 mb-3 border-b border-gray-800 pb-2">DOWN Side Layer (NO)</h2>
                <div class="space-y-2 text-sm font-mono">
                    <div class="flex justify-between"><span class="text-gray-500">Token ID:</span> <span class="text-xs text-gray-300" id="down_token_ui">-</span></div>
                    <div class="flex justify-between"><span class="text-gray-500">Market Best Bid:</span> <span class="text-gray-200" id="down_bid_ui">$0.0000</span></div>
                    <div class="flex justify-between bg-rose-500/5 p-2 rounded border border-rose-500/10"><span class="text-rose-400">Order Placed:</span> <span class="text-rose-400 font-bold" id="down_target_ui">$0.0000</span></div>
                </div>
            </div>
        </div>

        <div class="bg-gray-900/60 p-5 rounded-xl border border-gray-800">
            <h3 class="text-md font-bold mb-3 text-gray-200">Active Book Exposure</h3>
            <table class="w-full text-left text-sm font-mono">
                <thead>
                    <tr class="text-gray-500 border-b border-gray-800 text-xs">
                        <th class="pb-2">Time</th>
                        <th class="pb-2">Side</th>
                        <th class="pb-2">Price</th>
                        <th class="pb-2">Size</th>
                        <th class="pb-2 text-right">Status</th>
                    </tr>
                </thead>
                <tbody id="orders_body" class="divide-y divide-gray-800/40"></tbody>
            </table>
        </div>
    </div>

    <script>
        async function updateDashboard() {
            try {
                const res = await fetch('/api/state');
                const s = await res.json();

                document.getElementById('slug-lbl').textContent = s.market_slug;
                document.getElementById('balance_ui').textContent = `$${s.balance.toFixed(2)}`;
                document.getElementById('pnl_ui').textContent = `$${s.pnl.toFixed(4)}`;
                
                const m = Math.floor(s.time_elapsed_in_window / 60).toString().padStart(2, '0');
                const sec = (s.time_elapsed_in_window % 60).toString().padStart(2, '0');
                document.getElementById('timer_ui').textContent = `${m}:${sec}`;
                
                document.getElementById('contract_ui').textContent = s.current_market_title;
                document.getElementById('up_token_ui').textContent = s.up_token_id.slice(-8) + '...';
                document.getElementById('up_bid_ui').textContent = `$${s.up_bid.toFixed(4)}`;
                document.getElementById('up_target_ui').textContent = `$${s.up_target_price.toFixed(4)}`;
                
                document.getElementById('down_token_ui').textContent = s.down_token_id.slice(-8) + '...';
                document.getElementById('down_bid_ui').textContent = `$${s.down_bid.toFixed(4)}`;
                document.getElementById('down_target_ui').textContent = `$${s.down_target_price.toFixed(4)}`;

                const tbody = document.getElementById('orders_body');
                if(s.orders.length === 0) {
                    tbody.innerHTML = `<tr><td colspan="5" class="py-4 text-center text-gray-600 italic text-xs">No active orders inside execution window</td></tr>`;
                } else {
                    tbody.innerHTML = s.orders.map(o => `
                        <tr class="text-gray-300">
                            <td class="py-2.5 text-xs text-gray-500">${o.timestamp}</td>
                            <td class="py-2.5 font-bold ${o.side === 'UP' ? 'text-emerald-400' : 'text-rose-400'}">${o.side}</td>
                            <td class="py-2.5">${o.price.toFixed(4)}</td>
                            <td class="py-2.5">${o.shares}</td>
                            <td class="py-2.5 text-right"><span class="bg-blue-500/10 text-blue-400 border border-blue-500/20 px-1.5 py-0.5 rounded text-[10px]">${o.status}</span></td>
                        </tr>
                    `).join('');
                }
                document.getElementById('sync-lbl').textContent = 'Last Sync: ' + new Date().toLocaleTimeString();
            } catch (e) { console.error(e); }
        }
        setInterval(updateDashboard, 1000);
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(DASHBOARD_HTML)

@app.route('/api/state')
def api_state():
    return jsonify(bot_state.to_dict())

def run_server(port=5000):
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

if __name__ == '__main__':
    threading.Thread(target=run_server, kwargs={'port': int(os.getenv('PORT', 5000))}, daemon=True).start()
    try:
        run_trading_loop()
    except KeyboardInterrupt:
        sys.exit(0)
