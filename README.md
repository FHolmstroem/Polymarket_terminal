# Polymarket Live Tracker

![Python Version](https://img.shields.io/badge/python-3.8%2B-blue)
![Flask](https://img.shields.io/badge/Flask-Lightweight-lightgrey)
![License](https://img.shields.io/badge/license-MIT-green)

A lightning-fast, lightweight alternative to Dash/Plotly dashboards for tracking real-time Polymarket odds, trades, and position sizing. 

This tool provides a single-page UI that connects directly to the Polymarket Central Limit Order Book (CLOB) via WebSockets and pushes updates to the frontend using Server-Sent Events (SSE) for ultra-low latency.

**Disclaimer:** This tool is for educational and informational purposes only. It is not financial advice. 

![Screenshot](./screenshot.png) *(Note: Add a screenshot of your dashboard here and delete this note)*

## Features

* **Real-Time Price & Probability Charts:** 60fps rendering using TradingView's Canvas-based Lightweight Charts.
* **Live Trade Feed (Time & Sales):** Watch market orders execute in real-time, with an option to bundle trades by time intervals (1s, 2s, 5s, etc.) for easier reading in high-volume markets.
* **Cumulative Net Position Tracking:** Visualizes trade volume flow (Buys vs. Sells) to help gauge real-time market momentum.
* **Built-in Kelly Criterion Calculator:** Automatically calculates optimal bet sizing based on your bankroll. Choose between manual probability inputs or Auto-VWAP (Volume-Weighted Average Price) lookbacks.
* **Zero-Lag Updates:** Uses Server-Sent Events (SSE) to push only *new* data points to the browser. No full-page re-renders required.

## Architecture

Instead of relying on heavy Python-based dashboard frameworks, this tracker splits the workload efficiently:
1. **Backend (Flask):** Maintains the WebSocket connection to the Polymarket CLOB, polls the order book for best bids, and queues updates.
2. **Data Pipeline (SSE):** Flask streams the queued updates to the frontend natively.
3. **Frontend (Vanilla JS + HTML/CSS):** Ingests the SSE stream and updates the Canvas-based charts instantly.

## Quick Start

### Prerequisites
Make sure you have Python 3.8+ installed. 

### Installation

1. Clone this repository:
   git clone https://github.com/YOUR_USERNAME/polymarket-live-tracker.git
   cd polymarket-live-tracker

2. Install the required dependencies:
   pip install -r requirements.txt

3. Run the application:
   python polymarket_tracker.py

4. Open your browser and navigate to http://localhost:5000

## Usage

1. **Find an Event Slug:** Go to Polymarket, find a market you want to track, and look at the URL. For example, if the URL is `https://polymarket.com/event/bitcoin-above-70k`, the slug is `bitcoin-above-70k`.
2. **Load the Market:** Paste the slug into the top bar of the tracker and click "Load".
3. **Track Outcomes:** Click on the generated outcome chips (e.g., "Yes" or "No") to begin streaming live price data, trades, and position charts for those specific tokens.
4. **Calculate Sizing:** Expand the Kelly Criterion panel, input your bankroll, and set your perceived probability (or leave it on Auto) to calculate suggested bet sizes based on live market prices.

## License

Distributed under the MIT License. See `LICENSE` for more information.
