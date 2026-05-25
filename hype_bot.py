#!/usr/bin/env python3
"""
Polymarket Hype Pair Market-Making Bot
- Dynamically scans active 5-minute Hype markets by Event ID
- Extracts live token IDs for current UP and DOWN contracts
- Polls live top bids from the CLOB orderbook API
- Places simulated/live limit orders at (bid + $0.01) for both sides
- Closes orders dynamically at the 4:30 minute cutoff mark
- Built-in live Flask web dashboard
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
# CONFIGURATION
# ===========================================================================
DEMO_MODE = True
INITIAL_BALANCE = 1000.0
SHARES_PER_SIDE = 20
ORDER_OFFSET = 0.01  # Place orders at bid + $0.01
PLACE_ORDER_INTERVAL = 10  # Seconds between placing/refreshing orders
CUTOFF_TIME = 270  # 4:30 minutes = 270 seconds (stop placing orders)
WINDOW_DURATION = 300  # 5 minutes = 300 seconds

# Target Hype 5m Event ID from URL
EVENT_ID = "1779714000"

GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"

# ===========================================================================
# BOT STATE
# ===========================================================================
class BotState:
    def __init__(self):
        self.balance = INITIAL_BALANCE
        self.up_token_id = "N/A"
        self.down_token_id = "N/A"
        self.up_bid = 0.0
        self.down_bid = 0.0
        self.up_target_price = 0.0
        self.down_target_price = 0.0
        self.time_elapsed_in_window = 0
        self.current_market_id = "N/A"
        self.current_market_title = "Fetching active market..."
        self.orders = []
        self.trades = []
        self.pnl = 0.0

    def to_dict(self):
        return {
            "balance": round(self.balance, 2),
            "up_token_id": self.up_token_id,
            "down_token_id": self.down_token_id,
            "up_bid": round(self.up_bid, 4),
            "down_bid": round(self.down_bid, 4),
            "up_target_price": round(self.up_target_price, 4),
            "down_target_price": round(self.down_target_price, 4),
            "time_elapsed_in_window": self.time_elapsed_in_window,
            "current_market_id": self.current_market_id,
            "current_market_title": self.current_market_title,
            "orders": self.orders,
            "trades": self.trades,
            "pnl": round(self.pnl, 4)
        }

bot_state = BotState()
app = Flask(__name__)

# ===========================================================================
# POLYMARKET LIVE PRICE & MARKET DATA FETCHERS
# ===========================================================================
def fetch_active_hype_market(event_id):
    """
    Queries Gamma API to dynamically locate the current active, unexpired 
    5-minute window contract within the continuous Hype event series.
    """
    url = f"{GAMMA_API_BASE}/events/{event_id}"
    try:
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            data = response.json()
            markets = data.get("markets", [])
            
            # Filter for active, unclosed markets
            active_markets = [m for m in markets if not m.get("closed", False) and m.get("active", True)]
            
            if not active_markets:
                return None
            
            # Sort by expiration date string to find the current active window
            def parse_date(date_str):
                if not date_str:
                    return datetime.max
                try:
                    return datetime.strptime(date_str.replace("Z", ""), "%Y-%m-%dT%H:%M:%S.%f")
                except ValueError:
                    try:
                        return datetime.strptime(date_str.replace("Z", ""), "%Y-%m-%dT%H:%M:%S")
                    except:
                        return datetime.max

            now = datetime.utcnow()
            valid_future_markets = [m for m in active_markets if parse_date(m.get("end_date")) > now]
            
            if valid_future_markets:
                valid_future_markets.sort(key=lambda x: parse_date(x.get("end_date")))
                return valid_future_markets[0]
            
            return active_markets[0]
    except Exception as e:
        print(f"[ERROR] Error fetching dynamic market data: {e}")
    return None

def fetch_live_clob_bid(token_id):
    """
    Queries the live CLOB Orderbook API for a given token ID to isolate the 
    highest active buying price (top bid).
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
                # Top bid is always the first item in the orderbook
                return float(bids[0].get("price", 0.0))
    except Exception as e:
        print(f"[ERROR] Error pulling orderbook for token {token_id}: {e}")
    return 0.0

# ===========================================================================
# TRADING ENGINE LOOP
# ===========================================================================
def run_trading_loop():
    print("[ENGINE] Initializing Polymarket Market-Making Execution Loop...")
    last_order_time = 0
    previous_market_id = ""

    while True:
        try:
            # Synchronize timing with standard UTC 5-minute intervals
            now = datetime.utcnow()
            time_elapsed = (now.minute % 5) * 60 + now.second
            bot_state.time_elapsed_in_window = time_elapsed

            # Dynamic Market Discovery
            active_market = fetch_active_hype_market(EVENT_ID)
            
            if active_market:
                bot_state.current_market_id = active_market.get("id", "N/A")
                bot_state.current_market_title = active_market.get("title", "Hype Up/Down 5m")
                
                # Check if a new 5-minute cycle has rotated
                if bot_state.current_market_id != previous_market_id:
                    print(f"\n[ROTATION] New Market Detected: {bot_state.current_market_title} ({bot_state.current_market_id})")
                    # Cancel pending orders from the prior window
                    bot_state.orders = []
                    previous_market_id = bot_state.current_market_id

                # Safely unpack token IDs (handle stringified lists or native lists)
                token_ids_raw = active_market.get("clobTokenIds", [])
                if isinstance(token_ids_raw, str):
                    try:
                        token_ids = json.loads(token_ids_raw)
                    except:
                        token_ids = []
                else:
                    token_ids = token_ids_raw

                if len(token_ids) >= 2:
                    bot_state.up_token_id = token_ids[0]     # Index 0 is UP/YES
                    bot_state.down_token_id = token_ids[1]   # Index 1 is DOWN/NO

                    # Pull live top-level order book bids
                    bot_state.up_bid = fetch_live_clob_bid(bot_state.up_token_id)
                    bot_state.down_bid = fetch_live_clob_bid(bot_state.down_token_id)

                    # Compute target order spreads (Bid + $0.01)
                    if bot_state.up_bid > 0:
                        bot_state.up_target_price = bot_state.up_bid + ORDER_OFFSET
                    else:
                        bot_state.up_target_price = 0.50 # Default baseline fallback if book is momentarily empty

                    if bot_state.down_bid > 0:
                        bot_state.down_target_price = bot_state.down_bid + ORDER_OFFSET
                    else:
                        bot_state.down_target_price = 0.50
                else:
                    print("[WARN] Token IDs could not be resolved from market data.")
            else:
                print("[WARN] No active 5-minute Hype contracts returned from Gamma API.")

            # Execution Logic based on the 4:30 Cutoff Rule
            current_time_ms = time.time()
            if time_elapsed < CUTOFF_TIME:
                if current_time_ms - last_order_time >= PLACE_ORDER_INTERVAL:
                    if bot_state.up_target_price > 0 and bot_state.down_target_price > 0:
                        # Clear old un-filled orders, replace with updated tier prices
                        bot_state.orders = []
                        
                        # Generate UP Layer Order
                        up_order = {
                            "timestamp": datetime.now().strftime("%H:%M:%S"),
                            "side": "UP",
                            "token_id": bot_state.up_token_id,
                            "price": bot_state.up_target_price,
                            "shares": SHARES_PER_SIDE,
                            "status": "PLACED"
                        }
                        # Generate DOWN Layer Order
                        down_order = {
                            "timestamp": datetime.now().strftime("%H:%M:%S"),
                            "side": "DOWN",
                            "token_id": bot_state.down_token_id,
                            "price": bot_state.down_target_price,
                            "shares": SHARES_PER_SIDE,
                            "status": "PLACED"
                        }
                        
                        bot_state.orders.extend([up_order, down_order])
                        print(f"[ORDER] Refreshed Orders -> UP: ${bot_state.up_target_price:.2f} | DOWN: ${bot_state.down_target_price:.2f} at window second: {time_elapsed}s")
                        last_order_time = current_time_ms
            else:
                # Cutoff achieved; clear out open order logs to simulate structural cancellation
                if bot_state.orders:
                    print(f"[CUTOFF] Reached {time_elapsed}s. Automatically purging unfilled orders.")
                    # In a real environment, you execute order cancellation payload against /orders endpoint here
                    bot_state.orders = []

            time.sleep(1)
        except Exception as e:
            print(f"[CRITICAL ERROR] Core trading loop error: {e}")
            time.sleep(2)

# ===========================================================================
# FLASK LIVE WEB VISUALIZATION DASHBOARD
# ===========================================================================
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Polymarket Hype Maker Dashboard</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body { background-color: #0f172a; color: #e2e8f0; font-family: system-ui, sans-serif; }
    </style>
</head>
<body class="p-6">
    <div class="max-w-6xl mx-auto">
        <div class="flex justify-between items-center border-b border-gray-700 pb-4 mb-6">
            <div>
                <h1 class="text-3xl font-bold text-blue-400">Polymarket Hype Automated MM</h1>
                <p class="text-sm text-gray-400 mt-1">Target Event ID: <span class="text-yellow-400 font-mono">1779714000</span></p>
            </div>
            <div class="text-right">
                <span class="bg-green-500/20 text-green-400 border border-green-500/30 px-3 py-1 rounded text-xs font-semibold uppercase tracking-wider">
                    Live System Active
                </span>
                <p class="text-xs text-gray-500 mt-2">Dashboard Sync: <span id="last-update" class="font-mono">-</span></p>
            </div>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-4 gap-4 mb-6">
            <div class="bg-gray-800 p-4 rounded-lg border border-gray-700 shadow-lg">
                <p class="text-xs font-semibold text-gray-400 uppercase tracking-wider">Demo Balance</p>
                <p class="text-2xl font-bold text-white mt-1" id="balance">$0.00</p>
            </div>
            <div class="bg-gray-800 p-4 rounded-lg border border-gray-700 shadow-lg">
                <p class="text-xs font-semibold text-gray-400 uppercase tracking-wider">Session Return (PnL)</p>
                <p class="text-2xl font-bold mt-1" id="pnl">$0.00</p>
            </div>
            <div class="bg-gray-800 p-4 rounded-lg border border-gray-700 shadow-lg">
                <p class="text-xs font-semibold text-gray-400 uppercase tracking-wider">Window Counter</p>
                <div class="flex items-baseline space-x-2 mt-1">
                    <p class="text-2xl font-bold text-yellow-400 id="timer">00:00</p>
                    <p class="text-xs text-gray-400">/ 05:00</p>
                </div>
            </div>
            <div class="bg-gray-800 p-4 rounded-lg border border-gray-700 shadow-lg">
                <p class="text-xs font-semibold text-gray-400 uppercase tracking-wider">Active Contract Reference</p>
                <p class="text-sm font-semibold text-blue-300 mt-2 truncate" id="market-id">Detecting...</p>
            </div>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-2 gap-6 mb-6">
            <div class="bg-gray-800/60 p-5 rounded-lg border border-gray-700">
                <h2 class="text-xl font-bold text-emerald-400 mb-3 border-b border-gray-700 pb-2">UP Contract Book Layer</h2>
                <div class="space-y-2 text-sm">
                    <div class="flex justify-between"><span class="text-gray-400">Token Contract ID:</span> <span class="font-mono text-xs text-gray-300" id="up-token">Fetching...</span></div>
                    <div class="flex justify-between"><span class="text-gray-400">Live Orderbook Top Bid:</span> <span class="font-bold text-white" id="up-bid">$0.0000</span></div>
                    <div class="flex justify-between bg-emerald-500/10 p-2 rounded"><span class="text-emerald-400 font-medium">Target Order (Bid + $0.01):</span> <span class="font-bold text-emerald-400" id="up-target">$0.0000</span></div>
                </div>
            </div>

            <div class="bg-gray-800/60 p-5 rounded-lg border border-gray-700">
                <h2 class="text-xl font-bold text-red-400 mb-3 border-b border-gray-700 pb-2">DOWN Contract Book Layer</h2>
                <div class="space-y-2 text-sm">
                    <div class="flex justify-between"><span class="text-gray-400">Token Contract ID:</span> <span class="font-mono text-xs text-gray-300" id="down-token">Fetching...</span></div>
                    <div class="flex justify-between"><span class="text-gray-400">Live Orderbook Top Bid:</span> <span class="font-bold text-white" id="down-bid">$0.0000</span></div>
                    <div class="flex justify-between bg-red-500/10 p-2 rounded"><span class="text-red-400 font-medium">Target Order (Bid + $0.01):</span> <span class="font-bold text-red-400" id="down-target">$0.0000</span></div>
                </div>
            </div>
        </div>

        <div class="bg-gray-800 p-5 rounded-lg border border-gray-700 mb-6">
            <h2 class="text-lg font-bold text-white mb-3">Active Pipeline Orders (<span id="cutoff-status" class="text-xs font-normal">Trading Window Open</span>)</h2>
            <div class="overflow-x-auto">
                <table class="w-full text-left text-sm">
                    <thead>
                        <tr class="border-b border-gray-700 text-gray-400 text-xs uppercase tracking-wider">
                            <th class="pb-2">Timestamp</th>
                            <th class="pb-2">Direction</th>
                            <th class="pb-2">Target Price Limit</th>
                            <th class="pb-2">Shares Allocation</th>
                            <th class="pb-2 text-right">Status</th>
                        </tr>
                    </thead>
                    <tbody id="orders-table-body" class="divide-y divide-gray-700/50">
                        </tbody>
                </table>
            </div>
        </div>
    </div>

    <script>
        async function refreshDashboardMetrics() {
            try {
                const res = await fetch('/api/state');
                const data = await res.json();

                // Hydrate core metrics cards
                document.getElementById('balance').textContent = `$${data.balance.toFixed(2)}`;
                document.getElementById('pnl').textContent = `${data.pnl >= 0 ? '+' : ''}$${data.pnl.toFixed(4)}`;
                document.getElementById('pnl').className = `text-2xl font-bold mt-1 ${data.pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`;
                
                // Track standard minute:second notation
                const mins = Math.floor(data.time_elapsed_in_window / 60).toString().padStart(2, '0');
                const secs = (data.time_elapsed_in_window % 60).toString().padStart(2, '0');
                document.getElementById('timer').textContent = `${mins}:${secs}`;
                
                // Cutoff status text rendering
                const cutoffLabel = document.getElementById('cutoff-status');
                if (data.time_elapsed_in_window >= 270) {
                    cutoffLabel.textContent = "CUTOFF TRIGGERED - Post-processing/Cancelling Pending Orders";
                    cutoffLabel.className = "text-xs font-bold text-red-400 animate-pulse";
                } else {
                    cutoffLabel.textContent = "Trading Window Open";
                    cutoffLabel.className = "text-xs font-medium text-emerald-400";
                }

                document.getElementById('market-id').textContent = data.current_market_title;
                document.getElementById('market-id').title = data.current_market_id;

                // Hydrate token detail matrices
                document.getElementById('up-token').textContent = data.up_token_id;
                document.getElementById('up-bid').textContent = `$${data.up_bid.toFixed(4)}`;
                document.getElementById('up-target').textContent = `$${data.up_target_price.toFixed(4)}`;

                document.getElementById('down-token').textContent = data.down_token_id;
                document.getElementById('down-bid').textContent = `$${data.down_bid.toFixed(4)}`;
                document.getElementById('down-target').textContent = `$${data.down_target_price.toFixed(4)}`;

                // Render Active Orders Table Row Blocks
                const ordersTable = document.getElementById('orders-table-body');
                if (data.orders.length === 0) {
                    ordersTable.innerHTML = `<tr><td colspan="5" class="py-4 text-center text-gray-500 italic">No orders currently resting in book for this block segment</td></tr>`;
                } else {
                    ordersTable.innerHTML = data.orders.map(order => `
                        <tr class="text-gray-300">
                            <td class="py-3 font-mono text-xs">${order.timestamp}</td>
                            <td class="py-3 font-bold ${order.side === 'UP' ? 'text-emerald-400' : 'text-red-400'}">${order.side}</td>
                            <td class="py-3 font-mono">${order.price.toFixed(4)}</td>
                            <td class="py-3 font-mono">${order.shares}</td>
                            <td class="py-3 text-right"><span class="bg-blue-500/10 text-blue-400 border border-blue-500/20 px-2 py-0.5 rounded text-xs font-mono">${order.status}</span></td>
                        </tr>
                    `).join('');
                }

                document.getElementById('last-update').textContent = new Date().toLocaleTimeString();
            } catch (err) {
                console.error("Dashboard metric synchronization fault:", err);
            }
        }

        // Initialize execution grid update interval
        refreshDashboardMetrics();
        setInterval(refreshDashboardMetrics, 1000);
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

def run_dashboard_server(port=5000):
    """Fires up local telemetry server loop"""
    print(f"[DASHBOARD] Routing telemetry engine outward to http://localhost:{port}")
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

# ===========================================================================
# APPLICATION ROUTER ENTRYPOINT
# ===========================================================================
if __name__ == '__main__':
    # Initialize UI dashboard thread loop inside background pipeline
    dashboard_thread = threading.Thread(
        target=run_dashboard_server,
        kwargs={'port': int(os.getenv('PORT', 5000))},
        daemon=True
    )
    dashboard_thread.start()
    
    # Initialize core algorithmic trading execution on main process thread
    try:
        run_trading_loop()
    except KeyboardInterrupt:
        print("\n[SHUTDOWN] Soft shutdown command caught. Exiting system safely.")
        sys.exit(0)
