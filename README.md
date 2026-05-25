# Hype Pair Market-Making Bot

A market-making bot for Polymarket's Hype pair that places limit orders at optimal prices to capture spreads.

**Features:**
- ✅ Scans Hype pair prices every 10 seconds
- ✅ Places limit orders at (BID + $0.01) for both UP and DOWN
- ✅ 20 shares per side per order
- ✅ Places orders from 0:00 to 4:30 minutes of 5-minute window
- ✅ Cancels unfilled orders at window end (5:00)
- ✅ Demo mode with $1000 starting balance
- ✅ Beautiful real-time web dashboard
- ✅ One-click Railway deployment

---

## 🚀 Quick Start (No Coding Required!)

### Step 1: Create GitHub Repository

1. Go to [github.com/new](https://github.com/new)
2. Fill in:
   - **Repository name:** `hype-market-making-bot`
   - **Description:** `Hype pair market-making bot for Polymarket`
   - **Visibility:** Public
3. Click "Create repository"

### Step 2: Upload Files to GitHub (Using Web)

1. In your repository, click **"Add file"** → **"Upload files"**
2. Drag & drop or select these files:
   - `hype_bot.py` (main bot code)
   - `requirements.txt` (dependencies)
   - `.gitignore` (git configuration)
   - `README.md` (this file)

3. Click "Commit changes"

### Step 3: Deploy to Railway

1. Go to [railway.app](https://railway.app)
2. Click **"Start Project"**
3. Sign in with GitHub
4. Click **"Deploy from GitHub repo"**
5. Select your repository: `hype-market-making-bot`
6. Click **"Deploy"**
7. Wait 2-3 minutes for deployment
8. Click the **Public URL** in Railway dashboard

🎉 **Your bot is LIVE and trading!**

---

## 📊 Dashboard Features

The web dashboard updates every 1 second and shows:

**⏱️ Window Timer**
- Current time in window (0:00 to 5:00)
- Shows when bot stops placing orders (at 4:30)

**💰 Account**
- Current balance
- Total P&L (profit/loss)
- ROI percentage

**📊 Statistics**
- Total filled trades
- Win rate
- Wins / Losses

**🔥 Hype Pair Prices**
- UP BID: Current bid price for UP side
- DOWN BID: Current bid price for DOWN side
- UP ASK: Current ask price for UP side
- DOWN ASK: Current ask price for DOWN side

**🎯 Order Prices**
- UP Order Price: UP BID + $0.01 (where order will be placed)
- DOWN Order Price: DOWN BID + $0.01 (where order will be placed)

**📋 Active Orders**
- Lists all currently placed orders
- Shows side, shares, price, and cost

**📈 Filled Trades**
- All trades that filled
- Entry price, filled price, and P&L

---

## 🎯 How It Works

### Timeline (5-minute window)
