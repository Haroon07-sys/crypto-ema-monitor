from flask import Flask, render_template, jsonify
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler
from binance.client import Client
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
    # Email settings (set these in Render dashboard)
    SMTP_SERVER = os.getenv('SMTP_SERVER', 'smtp.gmail.com')
    SMTP_PORT = int(os.getenv('SMTP_PORT', 587))
    FROM_EMAIL = os.getenv('FROM_EMAIL', '')
    FROM_PASSWORD = os.getenv('FROM_PASSWORD', '')
    TO_EMAIL = os.getenv('TO_EMAIL', '')
    
    # Monitoring settings
    TOP_COINS = int(os.getenv('TOP_COINS', 1000))
    PROXIMITY_PERCENT = float(os.getenv('PROXIMITY_PERCENT', 2.0))
    CHECK_INTERVAL_MINUTES = int(os.getenv('CHECK_INTERVAL_MINUTES', 5))
    EMA_PERIODS = [20, 50, 200]
    TIMEFRAMES = ['15m', '1h', '4h', '1d']

# Initialize Binance client (public data only)
client = Client("", "")

# Store monitoring data
monitoring_data = {
    'timestamp': None,
    'coins': [],
    'alerts': [],
    'settings': {
        'top_coins': Config.TOP_COINS,
        'proximity': Config.PROXIMITY_PERCENT,
        'ema_periods': Config.EMA_PERIODS,
        'timeframes': Config.TIMEFRAMES
    }
}

def calculate_ema(df, period):
    """Calculate Exponential Moving Average"""
    return df['close'].ewm(span=period, adjust=False).mean()

def fetch_binance_data(symbol, interval='1h', limit=300):
    """Fetch historical data from Binance"""
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

def check_ema_conditions(symbol, timeframe, df, ema_period=200):
    """Check if price meets EMA conditions"""
    if df is None or len(df) < ema_period:
        return None
    
    df['ema'] = calculate_ema(df, ema_period)
    current_price = df['close'].iloc[-1]
    current_ema = df['ema'].iloc[-1]
    
    distance_pct = abs((current_price - current_ema) / current_ema * 100)
    
    prev_price = df['close'].iloc[-2]
    prev_ema = df['ema'].iloc[-2]
    
    crossed_above = prev_price <= prev_ema and current_price > current_ema
    crossed_below = prev_price >= prev_ema and current_price < current_ema
    
    return {
        'symbol': symbol,
        'timeframe': timeframe,
        'ema_period': ema_period,
        'price': current_price,
        'ema': current_ema,
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
            <h2>Crypto EMA Alert</h2>
            <table style="border-collapse: collapse; width: 100%;">
                <tr><td style="padding: 8px; background: #f0f0f0;"><b>Symbol:</b></td><td style="padding: 8px;">{coin_data['symbol']}</td></tr>
                <tr><td style="padding: 8px; background: #f0f0f0;"><b>EMA Period:</b></td><td style="padding: 8px;">{coin_data['ema_period']}</td></tr>
                <tr><td style="padding: 8px; background: #f0f0f0;"><b>Timeframe:</b></td><td style="padding: 8px;">{coin_data['timeframe']}</td></tr>
                <tr><td style="padding: 8px; background: #f0f0f0;"><b>Price:</b></td><td style="padding: 8px;">${coin_data['price']:.8f}</td></tr>
                <tr><td style="padding: 8px; background: #f0f0f0;"><b>EMA:</b></td><td style="padding: 8px;">${coin_data['ema']:.8f}</td></tr>
                <tr><td style="padding: 8px; background: #f0f0f0;"><b>Distance:</b></td><td style="padding: 8px;">{coin_data['distance_pct']}%</td></tr>
                <tr><td style="padding: 8px; background: #f0f0f0;"><b>Time:</b></td><td style="padding: 8px;">{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</td></tr>
            </table>
            <p><a href="https://your-app.onrender.com">View Dashboard</a></p>
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
    try:
        tickers = client.get_ticker()
        usdt_pairs = [t for t in tickers if t['symbol'].endswith('USDT')]
        sorted_pairs = sorted(usdt_pairs, key=lambda x: float(x['quoteVolume']), reverse=True)
        return [t['symbol'] for t in sorted_pairs[:min(Config.TOP_COINS, len(sorted_pairs))]]
    except Exception as e:
        logger.error(f"Error fetching top coins: {e}")
        return []

def monitor_crypto():
    """Main monitoring function"""
    global monitoring_data
    
    logger.info(f"Starting monitoring cycle...")
    
    try:
        symbols = get_top_cryptos()
        logger.info(f"Monitoring {len(symbols)} coins")
        
        all_results = []
        new_alerts = []
        
        for i, symbol in enumerate(symbols[:100]):  # Limit to 100 for free tier
            if i % 20 == 0:
                logger.info(f"Progress: {i}/{len(symbols)} coins checked")
            
            for timeframe in Config.TIMEFRAMES:
                for ema_period in Config.EMA_PERIODS:
                    df = fetch_binance_data(symbol, interval=timeframe, limit=ema_period + 100)
                    result = check_ema_conditions(symbol, timeframe, df, ema_period)
                    
                    if result:
                        all_results.append(result)
                        
                        if result['crossed_above'] or result['crossed_below'] or result['close_to_ema']:
                            if result['crossed_above'] or result['crossed_below'] or result['distance_pct'] < 1.0:
                                send_email_alert(result)
                                new_alerts.append(result)
        
        monitoring_data = {
            'timestamp': datetime.now().isoformat(),
            'coins': all_results,
            'alerts': (monitoring_data.get('alerts', []) + new_alerts)[-100:],
            'settings': monitoring_data['settings']
        }
        
        logger.info(f"Cycle complete. Found {len(new_alerts)} new alerts.")
        
    except Exception as e:
        logger.error(f"Monitoring error: {e}")

# Start monitoring in background
scheduler = BackgroundScheduler()
scheduler.add_job(func=monitor_crypto, trigger="interval", minutes=Config.CHECK_INTERVAL_MINUTES)
scheduler.start()

# Run once at startup
time.sleep(2)
monitor_crypto()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/data')
def get_data():
    return jsonify(monitoring_data)

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'timestamp': datetime.now().isoformat()})

if __name__ == '__main__':
    port = int(os.getenv('PORT', 10000))
    app.run(host='0.0.0.0', port=port)