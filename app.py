from flask import Flask, render_template, jsonify, request
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler
from binance.client import Client
from binance.exceptions import BinanceAPIException
import pandas as pd
import numpy as np
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import os
import logging
import time

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# Configuration from environment variables
class Config:
    # Binance API
    BINANCE_API_KEY = os.getenv('BINANCE_API_KEY', '')
    BINANCE_API_SECRET = os.getenv('BINANCE_API_SECRET', '')
    
    # Email settings
    SMTP_SERVER = os.getenv('SMTP_SERVER', 'smtp.gmail.com')
    SMTP_PORT = int(os.getenv('SMTP_PORT', 587))
    FROM_EMAIL = os.getenv('FROM_EMAIL', '')
    FROM_PASSWORD = os.getenv('FROM_PASSWORD', '')
    TO_EMAIL = os.getenv('TO_EMAIL', '')
    
    # Monitoring settings
    TOP_COINS = int(os.getenv('TOP_COINS', 500))
    PROXIMITY_PERCENT = float(os.getenv('PROXIMITY_PERCENT', 2.0))
    CHECK_INTERVAL_MINUTES = int(os.getenv('CHECK_INTERVAL_MINUTES', 5))
    EMA_PERIODS = [20, 50, 200]
    TIMEFRAMES = ['1h', '4h', '1d']

# Initialize Binance client
try:
    client = Client(Config.BINANCE_API_KEY, Config.BINANCE_API_SECRET)
    # Test connection
    client.ping()
    logger.info("Binance client initialized successfully")
except Exception as e:
    logger.error(f"Binance client error: {e}")
    client = None

# Store monitoring data
monitoring_data = {
    'timestamp': None,
    'coins': [],
    'alerts': [],
    'status': 'initializing'
}

def calculate_ema(close_prices, period):
    """Calculate Exponential Moving Average"""
    if len(close_prices) < period:
        return None
    series = pd.Series(close_prices)
    return series.ewm(span=period, adjust=False).mean().iloc[-1]

def fetch_binance_data(symbol, interval='1h', limit=300):
    """Fetch historical data from Binance"""
    if not client:
        return None
    try:
        klines = client.get_klines(symbol=symbol, interval=interval, limit=limit)
        df = pd.DataFrame(klines, columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_asset_volume', 'number_of_trades',
            'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'
        ])
        
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col])
        
        return df
    except Exception as e:
        logger.error(f"Error fetching {symbol}: {e}")
        return None

def check_ema_conditions(symbol, timeframe, df, ema_period):
    """Check if price meets EMA conditions"""
    if df is None or len(df) < ema_period:
        return None
    
    close_prices = df['close'].tolist()
    current_price = close_prices[-1]
    current_ema = calculate_ema(close_prices, ema_period)
    
    if current_ema is None:
        return None
    
    distance_pct = abs((current_price - current_ema) / current_ema * 100)
    
    # Check for crossover
    prev_price = close_prices[-2]
    prev_ema = calculate_ema(close_prices[:-1], ema_period)
    
    crossed_above = prev_price <= prev_ema and current_price > current_ema if prev_ema else False
    crossed_below = prev_price >= prev_ema and current_price < current_ema if prev_ema else False
    
    return {
        'symbol': symbol,
        'timeframe': timeframe,
        'ema_period': ema_period,
        'price': round(current_price, 4),
        'ema': round(current_ema, 4),
        'distance_pct': round(distance_pct, 2),
        'crossed_above': crossed_above,
        'crossed_below': crossed_below,
        'close_to_ema': distance_pct <= Config.PROXIMITY_PERCENT,
        'timestamp': datetime.now().isoformat()
    }

def send_email_alert(coin_data):
    """Send email notification"""
    if not Config.FROM_EMAIL or not Config.FROM_PASSWORD:
        return False
        
    try:
        msg = MIMEMultipart()
        msg['From'] = Config.FROM_EMAIL
        msg['To'] = Config.TO_EMAIL
        
        direction = "ABOVE" if coin_data['crossed_above'] else "BELOW" if coin_data['crossed_below'] else "NEAR"
        msg['Subject'] = f"🚨 EMA {coin_data['ema_period']} Alert: {coin_data['symbol']} {direction} ({coin_data['timeframe']})"
        
        body = f"""
        <html>
        <body style="font-family: Arial, sans-serif;">
            <h2 style="color: #333;">🚀 Crypto EMA Alert</h2>
            <table style="border-collapse: collapse; width: 100%; max-width: 600px;">
                <tr style="background: #f0f0f0;"><td style="padding: 10px;"><b>Symbol:</b></td><td style="padding: 10px;">{coin_data['symbol']}</td></tr>
                <tr><td style="padding: 10px;"><b>EMA Period:</b></td><td style="padding: 10px;">{coin_data['ema_period']}</td></tr>
                <tr style="background: #f0f0f0;"><td style="padding: 10px;"><b>Timeframe:</b></td><td style="padding: 10px;">{coin_data['timeframe']}</td></tr>
                <tr><td style="padding: 10px;"><b>Current Price:</b></td><td style="padding: 10px;">${coin_data['price']:,.4f}</td></tr>
                <tr style="background: #f0f0f0;"><td style="padding: 10px;"><b>EMA:</b></td><td style="padding: 10px;">${coin_data['ema']:,.4f}</td></tr>
                <tr><td style="padding: 10px;"><b>Distance:</b></td><td style="padding: 10px; color: {'red' if coin_data['distance_pct'] > 5 else 'orange' if coin_data['distance_pct'] > 2 else 'green'};">{coin_data['distance_pct']}%</td></tr>
                <tr style="background: #f0f0f0;"><td style="padding: 10px;"><b>Action:</b></td><td style="padding: 10px; font-weight: bold;">{coin_data['crossed_above'] and '✅ CROSSED ABOVE' or coin_data['crossed_below'] and '📉 CROSSED BELOW' or f'⚠️ Within {coin_data["distance_pct"]}%'}</td></tr>
                <tr><td style="padding: 10px;"><b>Time:</b></td><td style="padding: 10px;">{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</td></tr>
            </table>
            <p style="margin-top: 20px;"><a href="https://crypto-ema-monitor-1.onrender.com">View Dashboard</a></p>
        </body>
        </html>
        """
        
        msg.attach(MIMEText(body, 'html'))
        
        server = smtplib.SMTP(Config.SMTP_SERVER, Config.SMTP_PORT)
        server.starttls()
        server.login(Config.FROM_EMAIL, Config.FROM_PASSWORD)
        server.send_message(msg)
        server.quit()
        
        logger.info(f"Email sent for {coin_data['symbol']}")
        return True
    except Exception as e:
        logger.error(f"Email failed: {e}")
        return False

def get_top_cryptos():
    """Get top cryptocurrencies by volume"""
    if not client:
        return []
    try:
        tickers = client.get_ticker()
        usdt_pairs = [t for t in tickers if t['symbol'].endswith('USDT')]
        sorted_pairs = sorted(usdt_pairs, key=lambda x: float(x['quoteVolume']), reverse=True)
        return [t['symbol'] for t in sorted_pairs[:Config.TOP_COINS]]
    except Exception as e:
        logger.error(f"Error fetching top coins: {e}")
        return []

def monitor_crypto():
    """Main monitoring function"""
    global monitoring_data
    
    if not client:
        monitoring_data['status'] = 'error: binance client not connected'
        logger.error("Cannot monitor: Binance client not initialized")
        return
    
    monitoring_data['status'] = 'scanning'
    logger.info(f"Starting monitoring cycle - checking top {Config.TOP_COINS} coins...")
    
    try:
        symbols = get_top_cryptos()[:100]  # Limit to 100 for performance
        logger.info(f"Monitoring {len(symbols)} coins")
        
        all_results = []
        new_alerts = []
        
        for i, symbol in enumerate(symbols):
            if i % 20 == 0:
                logger.info(f"Progress: {i}/{len(symbols)} coins checked")
            
            for timeframe in Config.TIMEFRAMES:
                df = fetch_binance_data(symbol, interval=timeframe, limit=300)
                
                for ema_period in Config.EMA_PERIODS:
                    result = check_ema_conditions(symbol, timeframe, df, ema_period)
                    
                    if result:
                        all_results.append(result)
                        
                        # Send alerts for significant events
                        if result['crossed_above'] or result['crossed_below']:
                            send_email_alert(result)
                            new_alerts.append(result)
                        elif result['close_to_ema'] and result['distance_pct'] < 1.0:
                            send_email_alert(result)
                            new_alerts.append(result)
        
        monitoring_data = {
            'timestamp': datetime.now().isoformat(),
            'coins': all_results,
            'alerts': (monitoring_data.get('alerts', []) + new_alerts)[-100:],
            'status': 'active',
            'coins_scanned': len(symbols)
        }
        
        logger.info(f"Cycle complete. Found {len(new_alerts)} new alerts.")
        
    except Exception as e:
        logger.error(f"Monitoring error: {e}")
        monitoring_data['status'] = f'error: {str(e)[:100]}'

# Start monitoring in background
scheduler = BackgroundScheduler()
scheduler.add_job(func=monitor_crypto, trigger="interval", minutes=Config.CHECK_INTERVAL_MINUTES)
scheduler.start()

# Run once at startup
time.sleep(5)
monitor_crypto()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/data')
def get_data():
    return jsonify(monitoring_data)

@app.route('/api/status')
def get_status():
    return jsonify({
        'status': monitoring_data.get('status', 'unknown'),
        'last_update': monitoring_data.get('timestamp'),
        'coins_monitored': len(monitoring_data.get('coins', [])),
        'binance_connected': client is not None
    })

@app.route('/health')
def health():
    return jsonify({
        'status': 'ok', 
        'timestamp': datetime.now().isoformat(),
        'binance': 'connected' if client else 'disconnected'
    })

if __name__ == '__main__':
    port = int(os.getenv('PORT', 10000))
    app.run(host='0.0.0.0', port=port)