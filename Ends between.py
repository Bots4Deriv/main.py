import asyncio
import json
import websockets
import statistics
import time
import os
import requests
import signal
import sys
from aiohttp import web
import threading
from datetime import datetime
import pytz

# =========================
# TIMEZONE CONFIGURATION (GMT+3 / Kenya / EAT)
# =========================
EAT = pytz.timezone('Africa/Nairobi')  # GMT+3

def get_eat_time():
    """Get current time in GMT+3 (Kenya/EAT)"""
    return datetime.now(EAT)

def get_eat_timestamp():
    """Get formatted timestamp string in EAT"""
    return get_eat_time().strftime("%Y-%m-%d %H:%M:%S")

def get_eat_clock():
    """Get clock time only (HH:MM:SS) in EAT"""
    return get_eat_time().strftime("%H:%M:%S")

# =========================
# CONFIG (via Environment Variables)
# =========================
DERIV_APP_ID = os.getenv("DERIV_APP_ID", "1089")
SYMBOL = os.getenv("SYMBOL", "R_25")
MAX_TICKS = int(os.getenv("MAX_TICKS", "200"))

RANGE_LIMIT_30 = float(os.getenv("RANGE_LIMIT_30", "1.8"))
STRETCH_LIMIT = float(os.getenv("STRETCH_LIMIT", "0.7"))
MOMENTUM_LIMIT = int(os.getenv("MOMENTUM_LIMIT", "11"))
SPIKE_LIMIT = float(os.getenv("SPIKE_LIMIT", "0.8"))

PRINT_EVERY_TICK = os.getenv("PRINT_EVERY_TICK", "true").lower() == "true"
SIGNAL_COOLDOWN = int(os.getenv("SIGNAL_COOLDOWN", "2"))

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# Koyeb requires PORT env var
PORT = int(os.getenv("PORT", "8080"))

# =========================
# STATE
# =========================
prices = []
times = []
last_signal = None
last_signal_time = 0
bot_status = {"running": False, "last_price": None, "last_signal": None, "started_at": None}
price_history = []  # Store last 100 prices for chart
shutdown_event = asyncio.Event()

# =========================
# TELEGRAM FUNCTION
# =========================
def send_telegram(msg):
    if TELEGRAM_TOKEN and CHAT_ID:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {"chat_id": CHAT_ID, "text": msg}
        try:
            requests.post(url, json=data, timeout=5)
        except Exception as e:
            print(f"Telegram send failed: {e}")

# =========================
# SIGNAL LOGIC (Unchanged)
# =========================
def analyze_stays_between_signal(prices_list):
    if len(prices_list) < 30:
        return "WAIT - NOT ENOUGH DATA", {}

    last_30 = prices_list[-30:]
    last_15 = prices_list[-15:]
    last_10 = prices_list[-10:]
    last_6 = prices_list[-6:]
    current = prices_list[-1]

    range_30 = max(last_30) - min(last_30)
    if range_30 > RANGE_LIMIT_30:
        return "WAIT - HIGH VOLATILITY", {"range_30": round(range_30, 3)}

    avg_10 = sum(last_10) / len(last_10)
    stretch = abs(current - avg_10)
    if stretch > STRETCH_LIMIT:
        return "WAIT - PRICE STRETCHED", {
            "range_30": round(range_30, 3),
            "avg_10": round(avg_10, 3),
            "stretch": round(stretch, 3)
        }

    moves = [last_15[i] - last_15[i-1] for i in range(1, len(last_15))]
    up = sum(1 for m in moves if m > 0)
    down = sum(1 for m in moves if m < 0)
    if up >= MOMENTUM_LIMIT or down >= MOMENTUM_LIMIT:
        return "WAIT - MOMENTUM TOO STRONG", {
            "range_30": round(range_30, 3),
            "up_moves": up,
            "down_moves": down
        }

    recent_changes = [abs(last_6[i]-last_6[i-1]) for i in range(1, len(last_6))]
    max_spike = max(recent_changes) if recent_changes else 0
    if max_spike > SPIKE_LIMIT:
        return "WAIT - BREAKOUT RISK", {
            "range_30": round(range_30, 3),
            "max_spike": round(max_spike, 3)
        }

    return "ENTER STAYS BETWEEN", {
        "range_30": round(range_30, 3),
        "avg_10": round(avg_10, 3),
        "stretch": round(stretch, 3),
        "up_moves": up,
        "down_moves": down,
        "max_spike": round(max_spike, 3)
    }

# =========================
# PRINT HELPERS (Now with EAT)
# =========================
def should_print_signal(signal):
    global last_signal, last_signal_time
    now = time.time()
    if PRINT_EVERY_TICK:
        return True
    if signal != last_signal:
        last_signal = signal
        last_signal_time = now
        return True
    if now - last_signal_time >= SIGNAL_COOLDOWN:
        last_signal_time = now
        return True
    return False

def print_signal(signal, price, debug):
    ts = get_eat_clock()  # GMT+3 time
    debug_text = ""
    if debug:
        debug_parts = [f"{k}={v}" for k, v in debug.items()]
        debug_text = " | " + " | ".join(debug_parts)
    print(f"[{ts} EAT] PRICE={price:.3f} | SIGNAL={signal}{debug_text}")

# =========================
# DERIV WS LOOP (With EAT timestamps)
# =========================
async def stream_ticks():
    global prices, times, bot_status, price_history
    url = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"
    
    bot_status["running"] = True
    bot_status["started_at"] = get_eat_timestamp()  # GMT+3 timestamp

    while not shutdown_event.is_set():
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                print(f"\n✅ Connected to Deriv WebSocket | Symbol: {SYMBOL}")
                print(f"🕐 Local Time (EAT/GMT+3): {get_eat_timestamp()}")

                subscribe_msg = {"ticks": SYMBOL, "subscribe": 1}
                await ws.send(json.dumps(subscribe_msg))

                while not shutdown_event.is_set():
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        data = json.loads(msg)

                        if "error" in data:
                            print("❌ Deriv Error:", data["error"])
                            break

                        if "tick" in data:
                            tick = data["tick"]
                            quote = float(tick["quote"])
                            epoch = tick["epoch"]

                            prices.append(quote)
                            times.append(epoch)
                            
                            # Store for chart with EAT timestamp
                            price_history.append({
                                "price": quote,
                                "time": get_eat_clock(),  # HH:MM:SS in GMT+3
                                "full_time": get_eat_timestamp(),  # Full timestamp
                                "epoch": epoch
                            })
                            if len(price_history) > 100:
                                price_history.pop(0)

                            if len(prices) > MAX_TICKS:
                                prices.pop(0)
                                times.pop(0)

                            signal, debug = analyze_stays_between_signal(prices)
                            bot_status["last_price"] = quote
                            bot_status["last_signal"] = signal
                            bot_status["last_debug"] = debug
                            bot_status["tick_count"] = len(prices)

                            if should_print_signal(signal):
                                print_signal(signal, quote, debug)
                                if signal == "ENTER STAYS BETWEEN":
                                    eat_time = get_eat_timestamp()
                                    send_telegram(f"🚀 Signal: {signal}\nPrice: {quote}\nSymbol: {SYMBOL}\nTime (EAT): {eat_time}")

                    except asyncio.TimeoutError:
                        continue
                    except Exception as e:
                        print(f"❌ Message error: {e}")
                        break

        except Exception as e:
            print(f"❌ Connection error: {e}")
            if not shutdown_event.is_set():
                print("🔄 Reconnecting in 3 seconds...\n")
                await asyncio.sleep(3)
    
    bot_status["running"] = False
    print("🛑 Bot stopped gracefully")

# =========================
# WEB SERVER WITH LIVE DASHBOARD (EAT Time)
# =========================

# HTML Dashboard with auto-fetch
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Deriv Trading Bot - Live Dashboard (EAT/GMT+3)</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #1e3c72 0%, #2a5298 100%);
            color: white;
            min-height: 100vh;
            padding: 20px;
        }
        .container { max-width: 1200px; margin: 0 auto; }
        h1 { text-align: center; margin-bottom: 10px; font-size: 2.5em; text-shadow: 2px 2px 4px rgba(0,0,0,0.3); }
        .timezone { text-align: center; margin-bottom: 30px; opacity: 0.8; font-size: 1.1em; }
        .status-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        .card {
            background: rgba(255,255,255,0.1);
            backdrop-filter: blur(10px);
            border-radius: 15px;
            padding: 25px;
            border: 1px solid rgba(255,255,255,0.2);
            transition: transform 0.3s, box-shadow 0.3s;
        }
        .card:hover { transform: translateY(-5px); box-shadow: 0 10px 30px rgba(0,0,0,0.3); }
        .card h3 { font-size: 0.9em; text-transform: uppercase; letter-spacing: 1px; opacity: 0.8; margin-bottom: 10px; }
        .card .value { font-size: 2em; font-weight: bold; }
        .price { color: #00ff88; }
        .signal-enter { color: #00ff88; animation: pulse 2s infinite; }
        .signal-wait { color: #ffaa00; }
        .signal-wait-vol { color: #ff4444; }
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.6; }
        }
        .chart-container {
            background: rgba(0,0,0,0.3);
            border-radius: 15px;
            padding: 20px;
            margin-top: 20px;
            height: 300px;
            position: relative;
        }
        #priceChart { width: 100%; height: 100%; }
        .debug-info {
            background: rgba(0,0,0,0.2);
            border-radius: 10px;
            padding: 15px;
            margin-top: 20px;
            font-family: monospace;
            font-size: 0.9em;
        }
        .debug-item { display: flex; justify-content: space-between; padding: 5px 0; border-bottom: 1px solid rgba(255,255,255,0.1); }
        .last-update { text-align: center; margin-top: 20px; opacity: 0.7; font-size: 0.9em; }
        .connection-status {
            position: fixed;
            top: 20px;
            right: 20px;
            padding: 10px 20px;
            border-radius: 20px;
            font-weight: bold;
            transition: all 0.3s;
        }
        .connected { background: #00ff88; color: #000; }
        .disconnected { background: #ff4444; color: white; }
        .timezone-badge {
            display: inline-block;
            background: rgba(255,255,255,0.2);
            padding: 5px 15px;
            border-radius: 20px;
            font-size: 0.9em;
            margin-left: 10px;
        }
    </style>
</head>
<body>
    <div class="connection-status connected" id="connStatus">● LIVE</div>
    
    <div class="container">
        <h1>🚀 Deriv Trading Bot Dashboard</h1>
        <div class="timezone">
            Timezone: <span class="timezone-badge">EAT (GMT+3) - Kenya/Nairobi</span>
        </div>
        
        <div class="status-grid">
            <div class="card">
                <h3>Symbol</h3>
                <div class="value" id="symbol">-</div>
            </div>
            <div class="card">
                <h3>Current Price</h3>
                <div class="value price" id="price">-</div>
            </div>
            <div class="card">
                <h3>Signal</h3>
                <div class="value" id="signal">-</div>
            </div>
            <div class="card">
                <h3>Status</h3>
                <div class="value" id="status">-</div>
            </div>
            <div class="card">
                <h3>Tick Count</h3>
                <div class="value" id="tickCount">-</div>
            </div>
            <div class="card">
                <h3>Started At (EAT)</h3>
                <div class="value" id="uptime">-</div>
            </div>
        </div>

        <div class="chart-container">
            <canvas id="priceChart"></canvas>
        </div>

        <div class="debug-info" id="debugInfo">
            <div class="debug-item"><span>Range (30):</span><span id="range30">-</span></div>
            <div class="debug-item"><span>Avg (10):</span><span id="avg10">-</span></div>
            <div class="debug-item"><span>Stretch:</span><span id="stretch">-</span></div>
            <div class="debug-item"><span>Up Moves:</span><span id="upMoves">-</span></div>
            <div class="debug-item"><span>Down Moves:</span><span id="downMoves">-</span></div>
            <div class="debug-item"><span>Max Spike:</span><span id="maxSpike">-</span></div>
        </div>

        <div class="last-update">Last updated (EAT): <span id="lastUpdate">-</span></div>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script>
        let priceChart;
        let priceData = [];
        let timeLabels = [];
        
        // Initialize Chart
        function initChart() {
            const ctx = document.getElementById('priceChart').getContext('2d');
            priceChart = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: timeLabels,
                    datasets: [{
                        label: 'Price',
                        data: priceData,
                        borderColor: '#00ff88',
                        backgroundColor: 'rgba(0, 255, 136, 0.1)',
                        borderWidth: 2,
                        tension: 0.4,
                        fill: true,
                        pointRadius: 0
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { display: false }
                    },
                    scales: {
                        x: { 
                            display: true,
                            ticks: { color: 'rgba(255,255,255,0.7)', maxTicksLimit: 10 }
                        },
                        y: { 
                            display: true,
                            ticks: { color: 'rgba(255,255,255,0.7)' }
                        }
                    },
                    animation: { duration: 0 }
                }
            });
        }

        // Fetch data from API
        async function fetchData() {
            try {
                const response = await fetch('/api/status');
                if (!response.ok) throw new Error('Network response was not ok');
                
                const data = await response.json();
                updateDashboard(data);
                updateConnectionStatus(true);
            } catch (error) {
                console.error('Fetch error:', error);
                updateConnectionStatus(false);
            }
        }

        // Update dashboard with new data
        function updateDashboard(data) {
            document.getElementById('symbol').textContent = data.symbol || '-';
            document.getElementById('price').textContent = data.last_price ? data.last_price.toFixed(3) : '-';
            document.getElementById('status').textContent = data.bot_running ? 'RUNNING' : 'STOPPED';
            document.getElementById('tickCount').textContent = data.tick_count || '-';
            document.getElementById('uptime').textContent = data.started_at || '-';
            
            // Update signal with color coding
            const signalEl = document.getElementById('signal');
            signalEl.textContent = data.last_signal || '-';
            signalEl.className = 'value';
            if (data.last_signal === 'ENTER STAYS BETWEEN') {
                signalEl.classList.add('signal-enter');
            } else if (data.last_signal && data.last_signal.includes('HIGH VOLATILITY')) {
                signalEl.classList.add('signal-wait-vol');
            } else {
                signalEl.classList.add('signal-wait');
            }

            // Update debug info
            if (data.last_debug) {
                document.getElementById('range30').textContent = data.last_debug.range30 || '-';
                document.getElementById('avg10').textContent = data.last_debug.avg10 || '-';
                document.getElementById('stretch').textContent = data.last_debug.stretch || '-';
                document.getElementById('upMoves').textContent = data.last_debug.up_moves || '-';
                document.getElementById('downMoves').textContent = data.last_debug.down_moves || '-';
                document.getElementById('maxSpike').textContent = data.last_debug.max_spike || '-';
            }

            // Update chart
            if (data.price_history && data.price_history.length > 0) {
                priceData = data.price_history.map(p => p.price);
                timeLabels = data.price_history.map(p => p.time);
                
                if (priceChart) {
                    priceChart.data.labels = timeLabels;
                    priceChart.data.datasets[0].data = priceData;
                    priceChart.update('none');
                }
            }

            // Show EAT time in last update
            const now = new Date();
            const eatOffset = 3 * 60 * 60 * 1000; // GMT+3 in milliseconds
            const eatTime = new Date(now.getTime() + eatOffset);
            document.getElementById('lastUpdate').textContent = eatTime.toLocaleTimeString('en-GB', {timeZone: 'Africa/Nairobi'});
        }

        // Update connection status indicator
        function updateConnectionStatus(connected) {
            const statusEl = document.getElementById('connStatus');
            if (connected) {
                statusEl.textContent = '● LIVE';
                statusEl.className = 'connection-status connected';
            } else {
                statusEl.textContent = '● OFFLINE';
                statusEl.className = 'connection-status disconnected';
            }
        }

        // Start everything
        window.onload = function() {
            initChart();
            fetchData(); // Initial load
            setInterval(fetchData, 1000); // Update every 1 second
        };
    </script>
</body>
</html>
"""

async def dashboard(request):
    """Serve the main dashboard HTML"""
    return web.Response(text=DASHBOARD_HTML, content_type='text/html')

async def api_status(request):
    """API endpoint for live data"""
    return web.json_response({
        "status": "healthy",
        "bot_running": bot_status["running"],
        "symbol": SYMBOL,
        "last_price": bot_status.get("last_price"),
        "last_signal": bot_status.get("last_signal"),
        "last_debug": bot_status.get("last_debug"),
        "tick_count": bot_status.get("tick_count", 0),
        "started_at": bot_status["started_at"],
        "price_history": price_history[-50:],  # Last 50 points for chart
        "timezone": "EAT (GMT+3)"
    })

async def health_check(request):
    """Simple health check for Koyeb"""
    return web.json_response({
        "status": "healthy",
        "bot_running": bot_status["running"]
    })

async def start_web_server():
    app = web.Application()
    
    # Routes
    app.router.add_get('/', dashboard)           # Main dashboard
    app.router.add_get('/api/status', api_status)  # Live data API
    app.router.add_get('/health', health_check)    # Koyeb health check
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"🌐 Dashboard running at http://0.0.0.0:{PORT}")
    print(f"🕐 All times displayed in EAT (GMT+3) - Kenya/Nairobi")
    print(f"📊 Live data: http://0.0.0.0:{PORT}/api/status")

def signal_handler(sig, frame):
    print("\n⚠️ Shutdown signal received, stopping gracefully...")
    shutdown_event.set()
    sys.exit(0)

async def main():
    await asyncio.gather(
        stream_ticks(),
        start_web_server()
    )

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Bot stopped manually.")
