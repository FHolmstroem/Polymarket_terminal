"""
Polymarket Live Tracker
=======================
Fast, lightweight alternative to Dash/Plotly for real-time Polymarket odds.

Architecture:
  - Flask serves a single-page UI
  - TradingView Lightweight Charts (Canvas, not SVG) for 60fps rendering
  - Server-Sent Events push only NEW data points (no full re-renders)
  - WebSocket for live trade feed from Polymarket CLOB

Install:
  pip install flask requests websocket-client

Run:
  python polymarket_tracker.py
  Open http://localhost:5000
"""

from flask import Flask, Response, request, jsonify
import requests as http_req
import json, threading, time, websocket
from datetime import datetime
from collections import deque
from queue import Queue, Full

# ─── Tracker State ────────────────────────────────────────────────────────────

class MarketTracker:
    """Manages tracked tokens, price polling, WebSocket trades, and SSE broadcast."""

    COLORS = ['#00ff88','#bd00ff','#ff6b35','#00b4d8','#ffd60a','#ff006e','#38b000','#7209b7']

    def __init__(self):
        self.lock = threading.Lock()
        self.tracked = {}          # token_id -> {name, color, prices: deque}
        self.trades = deque(maxlen=500)
        self.ws = None
        self.color_idx = 0
        self.max_points = 120
        self.poll_interval = 3
        self.polling = False
        self.sse_queues = []

    # ── Public API ──

    def add_token(self, token_id, name, color=None):
        with self.lock:
            if token_id in self.tracked:
                return
            c = color or self.COLORS[self.color_idx % len(self.COLORS)]
            self.color_idx += 1
            self.tracked[token_id] = {
                'name': name, 'color': c,
                'prices': deque(maxlen=self.max_points),
            }
        self._restart_ws()
        if not self.polling:
            self._start_polling()

    def remove_token(self, token_id):
        with self.lock:
            self.tracked.pop(token_id, None)
        self._restart_ws()

    def clear(self):
        with self.lock:
            self.tracked.clear()
            self.trades.clear()
            self.color_idx = 0
        self._stop_ws()
        self.polling = False

    def set_max_points(self, n):
        with self.lock:
            if n == 0:
                # Session mode: effectively unlimited
                self.max_points = 999999
                for info in self.tracked.values():
                    old = list(info['prices'])
                    info['prices'] = deque(old, maxlen=999999)
            else:
                self.max_points = max(10, n)
                for info in self.tracked.values():
                    old = list(info['prices'])
                    info['prices'] = deque(old[-self.max_points:], maxlen=self.max_points)

    def get_state(self):
        """Return full current state for new SSE clients."""
        with self.lock:
            return {
                tid: {'name': info['name'], 'color': info['color'],
                       'prices': list(info['prices'])}
                for tid, info in self.tracked.items()
            }

    # ── SSE Broadcast ──

    def broadcast(self, event, data):
        msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
        dead = []
        for q in self.sse_queues:
            try:
                q.put_nowait(msg)
            except Full:
                dead.append(q)
        for q in dead:
            try: self.sse_queues.remove(q)
            except: pass

    # ── Price Polling ──

    def _get_best_bid(self, token_id):
        try:
            r = http_req.get(f"https://clob.polymarket.com/book?token_id={token_id}", timeout=5)
            if r.ok:
                bids = r.json().get('bids', [])
                if bids:
                    return float(max(bids, key=lambda x: float(x['price']))['price'])
        except:
            pass
        return None

    def _start_polling(self):
        self.polling = True
        def loop():
            while self.polling:
                with self.lock:
                    snapshot = {tid: dict(info) for tid, info in self.tracked.items()}
                if not snapshot:
                    time.sleep(1)
                    continue

                ts = time.time()
                for tid, info in snapshot.items():
                    price = self._get_best_bid(tid)
                    if price is None:
                        continue
                    point = {'time': ts, 'price': price}
                    with self.lock:
                        if tid in self.tracked:
                            self.tracked[tid]['prices'].append(point)
                    self.broadcast('price', {
                        'token_id': tid, 'name': info['name'],
                        'color': info['color'], 'time': ts, 'price': price,
                    })
                time.sleep(self.poll_interval)
        threading.Thread(target=loop, daemon=True).start()

    # ── WebSocket (Live Trades) ──

    def _restart_ws(self):
        self._stop_ws()
        with self.lock:
            ids = list(self.tracked.keys())
        if not ids:
            return

        tracker_ref = self

        def on_message(ws, message):
            try:
                payload = json.loads(message)
                for ev in (payload if isinstance(payload, list) else [payload]):
                    if ev.get('event_type') != 'last_trade_price':
                        continue
                    aid = ev.get('asset_id')
                    with tracker_ref.lock:
                        info = tracker_ref.tracked.get(aid)
                    if not info:
                        continue
                    trade = {
                        'time': time.time(),
                        'price': float(ev.get('price', 0)),
                        'size': float(ev.get('size', 0)),
                        'side': ev.get('side', '?').upper(),
                        'token_id': aid,
                        'name': info['name'],
                        'color': info['color'],
                    }
                    with tracker_ref.lock:
                        tracker_ref.trades.append(trade)
                    tracker_ref.broadcast('trade', trade)
            except:
                pass

        def on_open(ws):
            ws.send(json.dumps({'assets_ids': ids, 'type': 'market'}))

        def run():
            tracker_ref.ws = websocket.WebSocketApp(
                'wss://ws-subscriptions-clob.polymarket.com/ws/market',
                on_open=on_open, on_message=on_message,
            )
            tracker_ref.ws.run_forever(ping_interval=10, ping_timeout=8)

        threading.Thread(target=run, daemon=True).start()

    def _stop_ws(self):
        if self.ws:
            try: self.ws.close()
            except: pass
            self.ws = None


tracker = MarketTracker()

# ─── Flask App ────────────────────────────────────────────────────────────────

app = Flask(__name__)

@app.route('/api/event')
def api_event():
    slug = request.args.get('slug', '').strip()
    if not slug:
        return jsonify({'error': 'No slug'}), 400
    try:
        r = http_req.get(f'https://gamma-api.polymarket.com/events?slug={slug}', timeout=10)
        data = r.json()
        if not data:
            return jsonify({'error': 'Not found'}), 404

        event = data[0]
        markets = []
        for m in event.get('markets', []):
            toks = json.loads(m['clobTokenIds']) if isinstance(m['clobTokenIds'], str) else m['clobTokenIds']
            outs = json.loads(m['outcomes']) if isinstance(m['outcomes'], str) else m['outcomes']
            markets.append({
                'question': m.get('groupItemTitle') or m.get('question', ''),
                'tokens': [{'id': toks[i], 'name': outs[i]} for i in range(len(toks))],
            })
        return jsonify({'title': event.get('title', slug), 'markets': markets})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/track', methods=['POST'])
def api_track():
    d = request.json
    tracker.add_token(d['token_id'], d['name'], d.get('color'))
    return jsonify({'ok': True})

@app.route('/api/untrack', methods=['POST'])
def api_untrack():
    tracker.remove_token(request.json['token_id'])
    return jsonify({'ok': True})

@app.route('/api/clear', methods=['POST'])
def api_clear():
    tracker.clear()
    return jsonify({'ok': True})

@app.route('/api/max_points', methods=['POST'])
def api_max_points():
    tracker.set_max_points(request.json.get('n', 120))
    return jsonify({'ok': True})

@app.route('/api/trades')
def api_trades():
    with tracker.lock:
        return jsonify(list(tracker.trades))

@app.route('/api/stream')
def api_stream():
    q = Queue(maxsize=200)
    tracker.sse_queues.append(q)

    def gen():
        try:
            # Send full current state on connect
            state = tracker.get_state()
            yield f"event: init\ndata: {json.dumps(state)}\n\n"
            while True:
                yield q.get()
        except GeneratorExit:
            try: tracker.sse_queues.remove(q)
            except: pass

    return Response(gen(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

@app.route('/')
def index():
    return HTML

# ─── Frontend ─────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Polymarket Tracker</title>
<script src="https://unpkg.com/lightweight-charts@4.1.1/dist/lightweight-charts.standalone.production.js"></script>
<style>
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=DM+Sans:wght@400;500;600;700&display=swap');

  * { margin:0; padding:0; box-sizing:border-box; }

  :root {
    --bg: #0a0a0f;
    --surface: #12121a;
    --surface2: #1a1a26;
    --border: #2a2a3a;
    --text: #e0e0e8;
    --text-dim: #6a6a80;
    --accent: #00ff88;
    --accent-dim: #00ff8833;
    --red: #ff4466;
    --green: #00ff88;
  }

  body {
    font-family: 'DM Sans', sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    overflow-x: hidden;
  }

  /* ── Top Bar ── */
  .topbar {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 14px 20px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    flex-wrap: wrap;
  }
  .topbar .logo {
    font-family: 'JetBrains Mono', monospace;
    font-weight: 700;
    font-size: 14px;
    color: var(--accent);
    letter-spacing: 1px;
    text-transform: uppercase;
    margin-right: 12px;
    white-space: nowrap;
  }
  .topbar input[type="text"] {
    font-family: 'JetBrains Mono', monospace;
    font-size: 13px;
    padding: 8px 14px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--bg);
    color: var(--text);
    width: 360px;
    outline: none;
    transition: border-color .2s;
  }
  .topbar input:focus { border-color: var(--accent); }

  button {
    font-family: 'DM Sans', sans-serif;
    font-size: 13px;
    font-weight: 600;
    padding: 8px 16px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--surface2);
    color: var(--text);
    cursor: pointer;
    transition: all .15s;
    white-space: nowrap;
  }
  button:hover { background: var(--border); }
  button.primary {
    background: var(--accent);
    color: #000;
    border-color: var(--accent);
  }
  button.primary:hover { opacity: .85; }
  button.danger { color: var(--red); border-color: #ff446644; }
  button.danger:hover { background: #ff446622; }

  .spacer { flex:1; }

  /* ── Time window selector ── */
  .time-select {
    font-family: 'JetBrains Mono', monospace;
    font-size: 12px;
    padding: 7px 10px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--bg);
    color: var(--text);
    cursor: pointer;
    outline: none;
  }

  /* ── Markets Panel ── */
  #markets-panel {
    padding: 0 20px;
    max-height: 0;
    overflow: hidden;
    transition: max-height .3s ease, padding .3s ease;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
  }
  #markets-panel.open {
    max-height: 600px;
    padding: 16px 20px;
    overflow-y: auto;
  }
  .event-title {
    font-size: 15px;
    font-weight: 700;
    margin-bottom: 12px;
    color: var(--text);
  }
  .market-group {
    margin-bottom: 14px;
  }
  .market-question {
    font-size: 12px;
    font-weight: 600;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: .5px;
    margin-bottom: 8px;
  }
  .token-chips {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .chip {
    font-family: 'JetBrains Mono', monospace;
    font-size: 12px;
    padding: 6px 14px;
    border-radius: 20px;
    border: 1.5px solid var(--border);
    background: var(--bg);
    color: var(--text-dim);
    cursor: pointer;
    transition: all .2s;
    user-select: none;
  }
  .chip:hover { border-color: var(--text-dim); }
  .chip.active {
    border-color: var(--chip-color, var(--accent));
    color: var(--chip-color, var(--accent));
    background: color-mix(in srgb, var(--chip-color, var(--accent)) 12%, var(--bg));
    box-shadow: 0 0 12px color-mix(in srgb, var(--chip-color, var(--accent)) 25%, transparent);
  }

  /* ── Markets + Kelly Layout ── */
  .markets-layout {
    display: flex;
    gap: 24px;
    align-items: flex-start;
  }
  .markets-left { flex: 1; min-width: 0; }
  .kelly-panel {
    flex: 0 0 340px;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 14px;
  }
  .kelly-header {
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--accent);
    margin-bottom: 10px;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .kelly-bankroll {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 12px;
    padding-bottom: 10px;
    border-bottom: 1px solid var(--border);
  }
  .kelly-bankroll label {
    font-size: 11px;
    color: var(--text-dim);
    white-space: nowrap;
  }
  .kelly-bankroll input {
    font-family: 'JetBrains Mono', monospace;
    font-size: 13px;
    padding: 6px 10px;
    border-radius: 5px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    width: 110px;
    outline: none;
    text-align: right;
  }
  .kelly-bankroll input:focus { border-color: var(--accent); }
  .kelly-row {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 8px;
    padding: 8px;
    border-radius: 6px;
    background: var(--surface);
    border: 1px solid var(--border);
  }
  .kelly-dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
  }
  .kelly-name {
    font-size: 12px;
    font-weight: 600;
    width: 90px;
    flex-shrink: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .kelly-market-price {
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    color: var(--text-dim);
    width: 42px;
    text-align: center;
    flex-shrink: 0;
  }
  .kelly-prob-input {
    font-family: 'JetBrains Mono', monospace;
    font-size: 12px;
    padding: 4px 6px;
    border-radius: 4px;
    border: 1px solid var(--border);
    background: var(--bg);
    color: var(--text);
    width: 52px;
    text-align: right;
    outline: none;
  }
  .kelly-prob-input:focus { border-color: var(--accent); }
  .kelly-mode-toggle {
    font-family: 'JetBrains Mono', monospace;
    font-size: 9px;
    font-weight: 700;
    padding: 3px 6px;
    border-radius: 3px;
    border: 1px solid var(--border);
    background: var(--bg);
    color: var(--text-dim);
    cursor: pointer;
    flex-shrink: 0;
    text-transform: uppercase;
    letter-spacing: .5px;
    transition: all .15s;
  }
  .kelly-mode-toggle:hover { border-color: var(--text-dim); }
  .kelly-mode-toggle.auto {
    background: var(--accent-dim);
    border-color: var(--accent);
    color: var(--accent);
  }
  .kelly-auto-val {
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    color: var(--accent);
    width: 52px;
    text-align: right;
    flex-shrink: 0;
  }
  .kelly-result {
    font-family: 'JetBrains Mono', monospace;
    font-size: 12px;
    font-weight: 700;
    text-align: right;
    flex: 1;
    white-space: nowrap;
  }
  .kelly-result.pos { color: var(--green); }
  .kelly-result.neg { color: var(--red); }
  .kelly-result.zero { color: var(--text-dim); }
  .kelly-subtext {
    font-size: 10px;
    font-weight: 400;
    color: var(--text-dim);
    display: block;
  }
  .kelly-empty {
    font-size: 12px;
    color: var(--text-dim);
    text-align: center;
    padding: 16px 0;
  }
  @media (max-width: 800px) {
    .markets-layout { flex-direction: column; }
    .kelly-panel { flex: none; width: 100%; }
  }

  /* ── Charts ── */
  .chart-section {
    position: relative;
    width: 100%;
    padding: 0 8px;
  }
  .chart-label {
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--text-dim);
    padding: 8px 20px 2px;
    background: var(--bg);
  }
  #chart-wrap {
    height: calc(50vh - 140px);
    min-height: 220px;
  }
  #chart-container {
    width: 100%;
    height: 100%;
  }
  #pos-chart-wrap {
    height: calc(50vh - 140px);
    min-height: 220px;
    border-top: 1px solid var(--border);
  }
  #pos-chart-container {
    width: 100%;
    height: 100%;
  }

  /* ── Trade Feed ── */
  #trade-feed {
    height: 140px;
    overflow-y: auto;
    padding: 8px 20px;
    background: var(--surface);
    border-top: 1px solid var(--border);
    font-family: 'JetBrains Mono', monospace;
    font-size: 11.5px;
    line-height: 1.8;
  }
  .trade-row {
    display: flex;
    gap: 16px;
    opacity: 0;
    animation: fadeIn .25s forwards;
  }
  @keyframes fadeIn { to { opacity: 1; } }
  .trade-time { color: var(--text-dim); width: 80px; flex-shrink:0; }
  .trade-name { width: 140px; flex-shrink:0; font-weight: 600; }
  .trade-side { width: 50px; flex-shrink:0; font-weight: 700; }
  .trade-side.buy { color: var(--green); }
  .trade-side.sell { color: var(--red); }
  .trade-detail { color: var(--text-dim); }

  /* ── Status ── */
  #status {
    position: fixed;
    bottom: 152px;
    right: 20px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    color: var(--text-dim);
    background: var(--surface);
    border: 1px solid var(--border);
    padding: 4px 10px;
    border-radius: 4px;
    z-index: 10;
  }
  #status .dot {
    display: inline-block;
    width: 6px; height: 6px;
    border-radius: 50%;
    background: var(--green);
    margin-right: 6px;
    animation: pulse 2s infinite;
  }
  @keyframes pulse { 50% { opacity: .3; } }

  /* scrollbar */
  ::-webkit-scrollbar { width: 6px; }
  ::-webkit-scrollbar-track { background: var(--bg); }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
</style>
</head>
<body>

<!-- Top Bar -->
<div class="topbar">
  <div class="logo">PM Tracker</div>
  <input type="text" id="slug-input" placeholder="Event slug, e.g. cs2-rei-nt-2026-03-21" spellcheck="false">
  <button class="primary" onclick="loadSlug()">Load</button>
  <div class="spacer"></div>
  <label style="font-size:12px;color:var(--text-dim)">Window:</label>
  <select class="time-select" id="max-points" onchange="setMaxPoints()">
    <option value="30">~1.5 min</option>
    <option value="60">~3 min</option>
    <option value="120" selected>~6 min</option>
    <option value="300">~15 min</option>
    <option value="600">~30 min</option>
    <option value="1200">~1 hr</option>
    <option value="0">Session</option>
  </select>
  <button class="danger" onclick="clearAll()">Clear All</button>
  <label style="font-size:12px;color:var(--text-dim)">T&amp;S:</label>
  <select class="time-select" id="ts-mode" onchange="setTSMode()">
    <option value="0" selected>True T&amp;S</option>
    <option value="1">1s bundle</option>
    <option value="2">2s bundle</option>
    <option value="5">5s bundle</option>
    <option value="10">10s bundle</option>
    <option value="30">30s bundle</option>
  </select>
</div>

<!-- Markets Panel (expandable) -->
<div id="markets-panel">
  <div class="markets-layout">
    <div class="markets-left" id="markets-content"></div>
    <div class="kelly-panel" id="kelly-panel">
      <div class="kelly-header">&#9813; Kelly Criterion</div>
      <div class="kelly-bankroll">
        <label>Bankroll $</label>
        <input type="number" id="kelly-bankroll" value="1000" min="0" step="100" oninput="updateKelly()">
        <div style="flex:1"></div>
        <label>Auto lookback</label>
        <select class="time-select" id="kelly-lookback" onchange="updateKelly()" style="width:auto">
          <option value="30">30s</option>
          <option value="60" selected>1m</option>
          <option value="120">2m</option>
          <option value="300">5m</option>
          <option value="0">All</option>
        </select>
      </div>
      <div id="kelly-rows">
        <div class="kelly-empty">Track outcomes to calculate</div>
      </div>
    </div>
  </div>
</div>

<!-- Price Chart -->
<div class="chart-label">&#9679; Price / Implied Probability</div>
<div class="chart-section" id="chart-wrap">
  <div id="chart-container"></div>
</div>

<!-- Cumulative Position Chart -->
<div class="chart-label">&#9650; Cumulative Net Position (trade volume)</div>
<div class="chart-section" id="pos-chart-wrap">
  <div id="pos-chart-container"></div>
</div>

<!-- Status -->
<div id="status"><span class="dot"></span>Idle</div>

<!-- Trade Feed -->
<div id="trade-feed"></div>

<script>
// ─── Constants ───
const COLORS = ['#00ff88','#bd00ff','#ff6b35','#00b4d8','#ffd60a','#ff006e','#38b000','#7209b7'];

// ─── Shared chart options ───
function makeChartOpts() {
  return {
    layout: {
      background: { type: 'solid', color: '#0a0a0f' },
      textColor: '#6a6a80',
      fontFamily: "'JetBrains Mono', monospace",
      fontSize: 11,
    },
    grid: {
      vertLines: { color: '#1a1a26' },
      horzLines: { color: '#1a1a26' },
    },
    timeScale: {
      timeVisible: true,
      secondsVisible: true,
      borderColor: '#2a2a3a',
    },
    rightPriceScale: {
      borderColor: '#2a2a3a',
      scaleMargins: { top: 0.08, bottom: 0.08 },
    },
    crosshair: {
      mode: LightweightCharts.CrosshairMode.Normal,
      vertLine: { color: '#ffffff33', width: 1, style: 2 },
      horzLine: { color: '#ffffff33', width: 1, style: 2 },
    },
    handleScale: true,
    handleScroll: true,
  };
}

// ─── Price Chart Setup ───
const chartEl = document.getElementById('chart-container');
const chart = LightweightCharts.createChart(chartEl, makeChartOpts());

// ─── Position Chart Setup ───
const posChartEl = document.getElementById('pos-chart-container');
const posChart = LightweightCharts.createChart(posChartEl, makeChartOpts());

// Resize both charts
function resizeChart() {
  const wrap1 = document.getElementById('chart-wrap');
  chart.applyOptions({ width: wrap1.clientWidth, height: wrap1.clientHeight });
  const wrap2 = document.getElementById('pos-chart-wrap');
  posChart.applyOptions({ width: wrap2.clientWidth, height: wrap2.clientHeight });
}
window.addEventListener('resize', resizeChart);
resizeChart();

// ─── State ───
let seriesMap = {};       // token_id -> { series, markers[], info }
let posSeriesMap = {};    // token_id -> { series, info, cumulative, dataPoints[] }
let eventSource = null;
let colorIdx = 0;
let currentEvent = null;
let trackedSet = new Set();
let tsMode = 0;
let rawTrades = [];

function setTSMode() {
  tsMode = parseInt(document.getElementById('ts-mode').value);
  reprocessAllTrades();
}

function reprocessAllTrades() {
  document.getElementById('trade-feed').innerHTML = '';
  for (const s of Object.values(seriesMap)) {
    s.markers = [];
    s.series.setMarkers([]);
  }

  // Reset cumulative position data
  for (const ps of Object.values(posSeriesMap)) {
    ps.cumulative = 0;
    ps.dataPoints = [];
    ps.series.setData([]);
  }

  if (tsMode === 0) {
    for (const d of rawTrades) {
      addTradeRow(d);
      addTradeMarker(d);
      updatePositionChart(d);
    }
  } else {
    const buckets = {};
    for (const d of rawTrades) {
      // Still update position chart per raw trade
      updatePositionChart(d);

      const bucketStart = Math.floor(d.time / tsMode) * tsMode;
      const key = `${d.token_id}|${d.side}|${bucketStart}`;
      if (!buckets[key]) {
        buckets[key] = {
          size: 0, priceSum: 0, count: 0,
          bucketStart, bucketEnd: bucketStart + tsMode,
          side: d.side, token_id: d.token_id,
          name: d.name, color: d.color,
        };
      }
      const b = buckets[key];
      b.size += d.size;
      b.priceSum += d.price * d.size;
      b.count += 1;
    }
    const sorted = Object.values(buckets).sort((a, b) => a.bucketStart - b.bucketStart);
    for (const b of sorted) {
      emitAggregatedTrade(b);
    }
  }
}

let liveBuckets = {};

setInterval(() => {
  if (tsMode === 0) return;
  const now = Date.now() / 1000;
  const toFlush = [];
  for (const [key, b] of Object.entries(liveBuckets)) {
    if (b.bucketEnd <= now) {
      toFlush.push(key);
    }
  }
  for (const key of toFlush) {
    emitAggregatedTrade(liveBuckets[key]);
    delete liveBuckets[key];
  }
}, 500);

function emitAggregatedTrade(b) {
  const vwap = b.priceSum / b.size;
  const d = {
    time: b.bucketStart + (b.bucketEnd - b.bucketStart) / 2,
    price: vwap,
    size: b.size,
    side: b.side,
    token_id: b.token_id,
    name: b.name,
    color: b.color,
    count: b.count,
  };
  addTradeRow(d);
  addTradeMarker(d);
}

function processTrade(d) {
  rawTrades.push(d);
  // Always update the position chart with every raw trade
  updatePositionChart(d);

  if (tsMode === 0) {
    addTradeRow(d);
    addTradeMarker(d);
  } else {
    const bucketStart = Math.floor(d.time / tsMode) * tsMode;
    const bucketEnd = bucketStart + tsMode;
    const key = `${d.token_id}|${d.side}|${bucketStart}`;

    if (!liveBuckets[key]) {
      liveBuckets[key] = {
        size: 0, priceSum: 0, count: 0,
        bucketStart, bucketEnd,
        side: d.side, token_id: d.token_id,
        name: d.name, color: d.color,
      };
    }
    const b = liveBuckets[key];
    b.size += d.size;
    b.priceSum += d.price * d.size;
    b.count += 1;
  }
}

// ─── Cumulative Position Chart ───

function ensurePosSeries(tid, name, color) {
  if (posSeriesMap[tid]) return;
  const series = posChart.addLineSeries({
    color: color,
    lineWidth: 2,
    priceLineVisible: true,
    priceLineColor: color,
    lastValueVisible: true,
    title: name,
    crosshairMarkerRadius: 5,
  });
  posSeriesMap[tid] = { series, info: { name, color }, cumulative: 0, dataPoints: [], lastTime: 0 };
}

function updatePositionChart(d) {
  if (!posSeriesMap[d.token_id]) return;
  const ps = posSeriesMap[d.token_id];
  // BUY adds to position, SELL subtracts
  const delta = d.side === 'BUY' ? d.size : -d.size;
  ps.cumulative += delta;

  const t = Math.floor(d.time);
  ps.lastTime = Math.max(ps.lastTime || 0, t);
  // Ensure strictly increasing time
  let useTime = ps.lastTime;
  if (ps.dataPoints.length > 0) {
    const lastPt = ps.dataPoints[ps.dataPoints.length - 1];
    if (useTime <= lastPt.time) useTime = lastPt.time + 1;
  }
  ps.lastTime = useTime;

  const point = { time: useTime, value: ps.cumulative };
  ps.dataPoints.push(point);
  ps.series.update(point);
}

function addTradeMarker(d) {
  if (!seriesMap[d.token_id]) return;
  const s = seriesMap[d.token_id];
  const t = Math.floor(d.time);
  s.markers.push({
    time: t,
    position: d.side === 'BUY' ? 'belowBar' : 'aboveBar',
    shape: d.side === 'BUY' ? 'arrowUp' : 'arrowDown',
    color: s.info.color,
    text: `${d.side} ${d.size.toFixed(1)}${d.count > 1 ? ' ('+d.count+'x)' : ''}`,
    size: Math.min(3, Math.max(0.5, d.size / 20)),
  });
  if (s.markers.length > 50) s.markers = s.markers.slice(-50);
  const sorted = [...s.markers].sort((a,b) => a.time - b.time);
  for (let i = 1; i < sorted.length; i++) {
    if (sorted[i].time <= sorted[i-1].time) sorted[i].time = sorted[i-1].time + 1;
  }
  s.markers = sorted;
  s.series.setMarkers(sorted);
}

// ─── SSE Connection ───
function connectSSE() {
  if (eventSource) eventSource.close();
  eventSource = new EventSource('/api/stream');

  eventSource.addEventListener('init', (e) => {
    const state = JSON.parse(e.data);
    for (const [tid, info] of Object.entries(state)) {
      ensureSeries(tid, info.name, info.color);
      ensurePosSeries(tid, info.name, info.color);
      if (info.prices.length) {
        const data = info.prices.map(p => ({
          time: Math.floor(p.time),
          value: p.price,
        }));
        const deduped = deduplicateTimes(data);
        seriesMap[tid].series.setData(deduped);
      }
    }
    setStatus('Streaming');
  });

  eventSource.addEventListener('price', (e) => {
    const d = JSON.parse(e.data);
    ensureSeries(d.token_id, d.name, d.color);
    ensurePosSeries(d.token_id, d.name, d.color);
    const s = seriesMap[d.token_id];
    const t = Math.floor(d.time);
    s.lastTime = Math.max(s.lastTime || 0, t);
    s.series.update({ time: s.lastTime, value: d.price });
    latestPrices[d.token_id] = d.price;
    updateKelly();
  });

  eventSource.addEventListener('trade', (e) => {
    const d = JSON.parse(e.data);
    processTrade(d);
  });

  eventSource.onerror = () => setStatus('Reconnecting...');
}

function deduplicateTimes(data) {
  const seen = new Set();
  const result = [];
  for (const d of data) {
    let t = d.time;
    while (seen.has(t)) t++;
    seen.add(t);
    result.push({ time: t, value: d.value });
  }
  return result;
}

function ensureSeries(tid, name, color) {
  if (seriesMap[tid]) return;
  const series = chart.addLineSeries({
    color: color,
    lineWidth: 2,
    priceLineVisible: true,
    priceLineColor: color,
    lastValueVisible: true,
    title: name,
    crosshairMarkerRadius: 5,
  });
  seriesMap[tid] = { series, markers: [], info: { name, color }, lastTime: 0 };
}

// ─── API Helpers ───
async function loadSlug() {
  const slug = document.getElementById('slug-input').value.trim();
  if (!slug) return;

  setStatus('Loading...');

  await fetch('/api/clear', { method: 'POST' });
  clearLocalState();

  try {
    const r = await fetch(`/api/event?slug=${encodeURIComponent(slug)}`);
    if (!r.ok) {
      const err = await r.json();
      alert(err.error || 'Event not found');
      setStatus('Error');
      return;
    }
    currentEvent = await r.json();
    renderMarkets();
    setStatus('Select outcomes to track');
  } catch (e) {
    alert('Failed to load: ' + e.message);
    setStatus('Error');
  }
}

function renderMarkets() {
  const panel = document.getElementById('markets-panel');
  const content = document.getElementById('markets-content');
  let html = `<div class="event-title">${esc(currentEvent.title)}</div>`;

  let chipIdx = 0;
  for (const m of currentEvent.markets) {
    html += `<div class="market-group">`;
    html += `<div class="market-question">${esc(m.question)}</div>`;
    html += `<div class="token-chips">`;
    for (const tok of m.tokens) {
      const col = COLORS[chipIdx % COLORS.length];
      html += `<div class="chip" id="chip-${tok.id}"
                    style="--chip-color:${col}"
                    data-tid="${tok.id}" data-name="${esc(tok.name)}" data-color="${col}"
                    onclick="toggleToken(this)">${esc(tok.name)}</div>`;
      chipIdx++;
    }
    html += `</div></div>`;
  }
  content.innerHTML = html;
  panel.classList.add('open');
}

async function toggleToken(el) {
  const tid = el.dataset.tid;
  const name = el.dataset.name;
  const color = el.dataset.color;

  if (trackedSet.has(tid)) {
    trackedSet.delete(tid);
    el.classList.remove('active');
    await fetch('/api/untrack', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ token_id: tid }),
    });
    if (seriesMap[tid]) {
      chart.removeSeries(seriesMap[tid].series);
      delete seriesMap[tid];
    }
    if (posSeriesMap[tid]) {
      posChart.removeSeries(posSeriesMap[tid].series);
      delete posSeriesMap[tid];
    }
    removeKellyRow(tid);
  } else {
    trackedSet.add(tid);
    el.classList.add('active');
    await fetch('/api/track', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ token_id: tid, name, color }),
    });
    ensurePosSeries(tid, name, color);
    addKellyRow(tid, name, color);
    setStatus('Streaming');
  }
}

async function setMaxPoints() {
  const n = parseInt(document.getElementById('max-points').value);
  await fetch('/api/max_points', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ n }),
  });
}

async function clearAll() {
  await fetch('/api/clear', { method: 'POST' });
  clearLocalState();
  document.getElementById('markets-panel').classList.remove('open');
  currentEvent = null;
  document.getElementById('slug-input').value = '';
  setStatus('Idle');
}

function clearLocalState() {
  for (const tid of Object.keys(seriesMap)) {
    chart.removeSeries(seriesMap[tid].series);
  }
  for (const tid of Object.keys(posSeriesMap)) {
    posChart.removeSeries(posSeriesMap[tid].series);
  }
  seriesMap = {};
  posSeriesMap = {};
  trackedSet.clear();
  rawTrades = [];
  liveBuckets = {};
  kellyEstimates = {};
  kellyAutoMode = {};
  latestPrices = {};
  colorIdx = 0;
  document.getElementById('trade-feed').innerHTML = '';
  document.getElementById('kelly-rows').innerHTML = '<div class="kelly-empty">Track outcomes to calculate</div>';
}

// ─── Trade Feed ───
function addTradeRow(d) {
  const feed = document.getElementById('trade-feed');
  const t = new Date(d.time * 1000);
  const ts = t.toLocaleTimeString();
  const arrow = d.side === 'BUY' ? '▲' : '▼';
  const countLabel = (d.count && d.count > 1) ? ` <span style="color:var(--text-dim);font-size:10px">(${d.count} trades)</span>` : '';
  const row = document.createElement('div');
  row.className = 'trade-row';
  row.innerHTML = `
    <span class="trade-time">${ts}</span>
    <span class="trade-name" style="color:${d.color}">${esc(d.name)}</span>
    <span class="trade-side" style="color:${d.color}">${arrow} ${d.side}</span>
    <span class="trade-detail">${d.size.toFixed(2)} @ $${d.price.toFixed(3)}${countLabel}</span>
  `;
  feed.insertBefore(row, feed.firstChild);
  while (feed.children.length > 80) feed.removeChild(feed.lastChild);
}

// ─── Util ───
function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}
function setStatus(msg) {
  document.getElementById('status').innerHTML = `<span class="dot"></span>${esc(msg)}`;
}

// ─── Kelly Criterion Calculator ───
let kellyEstimates = {};
let kellyAutoMode = {};
let latestPrices = {};

function computeVWAP(tid) {
  const lookback = parseInt(document.getElementById('kelly-lookback').value);
  const now = Date.now() / 1000;
  let trades;
  if (lookback === 0) {
    trades = rawTrades.filter(t => t.token_id === tid);
  } else {
    trades = rawTrades.filter(t => t.token_id === tid && (now - t.time) <= lookback);
  }
  if (!trades.length) return null;
  let sumPV = 0, sumV = 0;
  for (const t of trades) {
    sumPV += t.price * t.size;
    sumV += t.size;
  }
  return sumV > 0 ? sumPV / sumV : null;
}

function addKellyRow(tid, name, color) {
  if (kellyAutoMode[tid] === undefined) kellyAutoMode[tid] = true;
  if (!kellyEstimates[tid]) kellyEstimates[tid] = '';
  const container = document.getElementById('kelly-rows');
  const empty = container.querySelector('.kelly-empty');
  if (empty) empty.remove();
  if (document.getElementById(`kelly-${tid}`)) return;

  const isAuto = kellyAutoMode[tid];
  const row = document.createElement('div');
  row.className = 'kelly-row';
  row.id = `kelly-${tid}`;
  row.innerHTML = `
    <div class="kelly-dot" style="background:${color}"></div>
    <div class="kelly-name" style="color:${color}">${esc(name)}</div>
    <div class="kelly-market-price" id="kmp-${tid}">—</div>
    <button class="kelly-mode-toggle ${isAuto ? 'auto' : ''}" id="kmode-${tid}"
            onclick="toggleKellyMode('${tid}')">${isAuto ? 'auto' : 'manual'}</button>
    <div class="kelly-auto-val" id="kauto-${tid}" style="display:${isAuto ? 'block' : 'none'}">—</div>
    <input class="kelly-prob-input" type="number" min="0" max="100" step="1"
           placeholder="p%"
           id="kinput-${tid}"
           data-tid="${tid}"
           oninput="onKellyInput(this)"
           value="${kellyEstimates[tid]}"
           style="display:${isAuto ? 'none' : 'block'}">
    <div class="kelly-result zero" id="kres-${tid}">—</div>
  `;
  container.appendChild(row);
  updateKelly();
}

function toggleKellyMode(tid) {
  kellyAutoMode[tid] = !kellyAutoMode[tid];
  const isAuto = kellyAutoMode[tid];
  const btn = document.getElementById(`kmode-${tid}`);
  const autoVal = document.getElementById(`kauto-${tid}`);
  const input = document.getElementById(`kinput-${tid}`);
  if (btn) {
    btn.textContent = isAuto ? 'auto' : 'manual';
    btn.classList.toggle('auto', isAuto);
  }
  if (autoVal) autoVal.style.display = isAuto ? 'block' : 'none';
  if (input) input.style.display = isAuto ? 'none' : 'block';
  updateKelly();
}

function removeKellyRow(tid) {
  const row = document.getElementById(`kelly-${tid}`);
  if (row) row.remove();
  delete kellyEstimates[tid];
  delete kellyAutoMode[tid];
  delete latestPrices[tid];
  const container = document.getElementById('kelly-rows');
  if (!container.children.length) {
    container.innerHTML = '<div class="kelly-empty">Track outcomes to calculate</div>';
  }
}

function onKellyInput(el) {
  kellyEstimates[el.dataset.tid] = el.value;
  updateKelly();
}

function updateKelly() {
  const bankroll = parseFloat(document.getElementById('kelly-bankroll').value) || 0;

  for (const [tid, info] of Object.entries(seriesMap)) {
    const c = latestPrices[tid];
    const mpEl = document.getElementById(`kmp-${tid}`);
    const resEl = document.getElementById(`kres-${tid}`);
    const autoEl = document.getElementById(`kauto-${tid}`);
    if (!mpEl || !resEl) continue;

    if (c !== undefined && c !== null) {
      mpEl.textContent = (c * 100).toFixed(1) + '%';
    }

    let p;
    if (kellyAutoMode[tid]) {
      const vwap = computeVWAP(tid);
      if (vwap !== null && autoEl) {
        autoEl.textContent = (vwap * 100).toFixed(1) + '%';
        p = vwap;
      } else {
        if (autoEl) autoEl.textContent = '—';
        resEl.className = 'kelly-result zero';
        resEl.innerHTML = '—';
        continue;
      }
    } else {
      const pRaw = parseFloat(kellyEstimates[tid]);
      if (isNaN(pRaw) || c === undefined || c === null) {
        resEl.className = 'kelly-result zero';
        resEl.innerHTML = '—';
        continue;
      }
      p = pRaw / 100;
    }

    if (c === undefined || c === null) {
      resEl.className = 'kelly-result zero';
      resEl.innerHTML = '—';
      continue;
    }

    const f = c < 1 ? (p - c) / (1 - c) : 0;

    if (f > 0.001) {
      const bet = bankroll * f;
      resEl.className = 'kelly-result pos';
      resEl.innerHTML = `${(f*100).toFixed(1)}%<span class="kelly-subtext">$${bet.toFixed(2)} YES</span>`;
    } else if (f < -0.001) {
      const fNo = c > 0 ? ((1-p) - (1-c)) / c : 0;
      const bet = bankroll * Math.max(0, fNo);
      resEl.className = 'kelly-result neg';
      resEl.innerHTML = `${(fNo*100).toFixed(1)}%<span class="kelly-subtext">$${bet.toFixed(2)} NO</span>`;
    } else {
      resEl.className = 'kelly-result zero';
      resEl.innerHTML = `0%<span class="kelly-subtext">no edge</span>`;
    }
  }
}

setInterval(updateKelly, 2000);

document.getElementById('slug-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') loadSlug();
});

// ─── Init ───
connectSSE();
resizeChart();
</script>
</body>
</html>"""

# ─── Run ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print("\n  ╔══════════════════════════════════════╗")
    print("  ║   Polymarket Tracker                 ║")
    print("  ║   http://localhost:5000               ║")
    print("  ╚══════════════════════════════════════╝\n")
    app.run(host='0.0.0.0', port=5000, threaded=True)
