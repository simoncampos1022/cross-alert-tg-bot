import requests
import numpy as np
from datetime import datetime, timezone
import time
import threading
from plyer import notification
import asyncio
import logging
import aiohttp

# Constants
BITGET_CANDLE_URL = "https://api.bitget.com/api/v2/mix/market/history-candles"
BITGET_FUTURES_TICKERS_URL = "https://api.bitget.com/api/v2/mix/market/tickers"
ETHUSDT_SYMBOL = "ETHUSDT"
PRODUCT_TYPE = "USDT-FUTURES"
INTERVAL = "15m"
FS_LENGTH = 9

# Telegram Configuration - UPDATE THESE VALUES
TELEGRAM_BOT_TOKEN = "8107354645:AAFKKSuglUYCDr3cpMPSU2oGyCyGa5Y_SdQ"  # Get from @BotFather
TELEGRAM_CHAT_ID = "7839829083"     # Your chat ID or group ID

class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
    
    async def send_message(self, message: str):
        """Send message to Telegram asynchronously"""
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.post(
                    f"{self.base_url}/sendMessage",
                    data={
                        "chat_id": self.chat_id,
                        "text": message,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True
                    }
                ) as response:
                    if response.status == 200:
                        logging.info("✅ Telegram message sent successfully")
                        return True
                    else:
                        logging.error(f"❌ Telegram error: {response.status}")
                        return False
        except Exception as e:
            logging.error(f"❌ Telegram send failed: {e}")
            return False

class FisherTransformBot:
    def __init__(self):
        self.candles = []
        self.fs = []
        self.tr = []
        self.value = []
        self.condition_lock = threading.Lock()
        self.last_alert_time = 0
        self.alert_cooldown = 60  # 1 minute cooldown
        
        self.telegram = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        self.telegram_loop = None
        
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    def fetch_candles(self):
        try:
            params = {
                "symbol": ETHUSDT_SYMBOL,
                "granularity": INTERVAL,
                "productType": PRODUCT_TYPE,
            }
            response = requests.get(BITGET_CANDLE_URL, params=params, timeout=10)
            response.raise_for_status()
            data = response.json().get("data", [])
            return [{
                "timestamp": datetime.fromtimestamp(int(entry[0]) / 1000, timezone.utc),
                "open": float(entry[1]),
                "high": float(entry[2]),
                "low": float(entry[3]),
                "close": float(entry[4])
            } for entry in data]
        except Exception as e:
            logging.error(f"Error fetching candles: {e}")
            return []

    def get_current_price(self):
        try:
            params = {'productType': PRODUCT_TYPE}
            response = requests.get(BITGET_FUTURES_TICKERS_URL, params=params, timeout=5)
            response.raise_for_status()
            data = response.json()
            for ticker in data.get('data', []):
                if ticker['symbol'] == ETHUSDT_SYMBOL:
                    return float(ticker['lastPr'])
            return None
        except Exception as e:
            logging.error(f"Price fetch error: {e}")
            return None

    def update_indicators(self):
        new_candles = self.fetch_candles()
        if not new_candles:
            logging.info("No new candles fetched.")
            return

        self.candles = sorted(new_candles, key=lambda x: x['timestamp'])

        highs = np.array([c['high'] for c in self.candles])
        lows = np.array([c['low'] for c in self.candles])
        median_price = (highs + lows) / 2

        length = len(median_price)
        fs = np.zeros(length)
        tr = np.zeros(length)
        value = np.zeros(length)

        for i in range(FS_LENGTH, length):
            max_h = np.max(median_price[i - FS_LENGTH + 1:i + 1])
            min_l = np.min(median_price[i - FS_LENGTH + 1:i + 1])
            if max_h == min_l:
                value[i] = 0
            else:
                val = 0.33 * 2 * ((median_price[i] - min_l) / (max_h - min_l) - 0.5) + 0.67 * value[i - 1]
                value[i] = np.clip(val, -0.999, 0.999)
            fs[i] = 0.5 * np.log((1 + value[i]) / (1 - value[i])) + 0.5 * fs[i - 1]
            tr[i] = fs[i - 1]

        self.fs = fs.tolist()
        self.tr = tr.tolist()
        self.value = value.tolist()

    def check_signal(self, current_time: str):
        if len(self.fs) < 3:
            return

        fs_now = self.fs[-1]
        tr_now = self.tr[-1]
        fs_prev = self.fs[-2]
        tr_prev = self.tr[-2]

        logging.info(f"[{current_time}] 15min indicator values fs:{fs_now:.4f}, tr:{tr_now:.4f}")

        # Detect crossovers
        if fs_prev < tr_prev and fs_now > tr_now:
            message = f"🚀 <b>Fisher LONG Cross</b>\n⏰ {current_time}\n📈 FS: <code>{fs_now:.4f}</code>\n📉 TR: <code>{tr_now:.4f}</code>"
            self.notify(message)
        elif fs_prev > tr_prev and fs_now < tr_now:
            message = f"🔻 <b>Fisher SHORT Cross</b>\n⏰ {current_time}\n📈 FS: <code>{fs_now:.4f}</code>\n📉 TR: <code>{tr_now:.4f}</code>"
            self.notify(message)

    async def _send_telegram(self, message: str):
        """Internal async Telegram sender"""
        if self.telegram:
            return await self.telegram.send_message(message)
        return False

    def notify(self, message: str):
        """Send notification via Telegram + desktop with cooldown"""
        current_time = time.time()
        
        # Check cooldown
        if current_time - self.last_alert_time < self.alert_cooldown:
            logging.info("⏳ Alert skipped due to cooldown")
            return
        
        self.last_alert_time = current_time
        logging.info(f"🔔 Sending alert: {message[:50]}...")
        
        # Send to Telegram (primary) - simplified sync version for reliability
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            data = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            }
            response = requests.post(url, data=data, timeout=10)
            telegram_success = response.status_code == 200
            logging.info(f"📱 Telegram: {'✅' if telegram_success else '❌'}")
        except Exception as e:
            logging.error(f"Telegram failed: {e}")
            telegram_success = False
        
        # Desktop notification (backup)
        print(f"🔔 TELEGRAM: {'✅' if telegram_success else '❌ FAILED'}\n{message}\n")
        
        try:
            notification.notify(
                title="Fisher Transform Bot",
                message=message[:100],
                app_name="Fisher Bot",
                timeout=10
            )
        except Exception as e:
            logging.warning(f"Desktop notification failed: {e}")

    def send_startup_notification(self):
        """Send startup confirmation message - NO cooldown check"""
        current_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        message = f"🤖 <b>Fisher Transform Bot STARTED</b>\n⏰ {current_time}\n📊 Monitoring <code>ETHUSDT</code> 15m\n⚙️ Fisher Length: <code>{FS_LENGTH}</code>\n🔔 Ready for LONG/SHORT signals!"
        
        logging.info("🚀 Sending startup notification...")
        
        # Send startup WITHOUT cooldown check
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            data = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            }
            response = requests.post(url, data=data, timeout=10)
            telegram_success = response.status_code == 200
            logging.info(f"📱 Startup Telegram: {'✅' if telegram_success else f'❌ {response.status_code}'}")
        except Exception as e:
            logging.error(f"Startup Telegram failed: {e}")
        
        print(f"🔔 STARTUP: {'✅' if 'telegram_success' in locals() and telegram_success else '❌ FAILED'}\n{message}\n")
        
        # Desktop notification
        try:
            notification.notify(
                title="Fisher Transform Bot STARTED",
                message=f"ETHUSDT 15m monitoring active",
                app_name="Fisher Bot",
                timeout=10
            )
        except Exception as e:
            logging.warning(f"Startup desktop notification failed: {e}")

    def run(self):
        logging.info("🚀 Starting Fisher Transform Bot...")
        logging.info(f"📱 Telegram Token: {'✅ SET' if TELEGRAM_BOT_TOKEN else '❌ MISSING'}")
        logging.info(f"📱 Telegram Chat ID: {'✅ SET' if TELEGRAM_CHAT_ID else '❌ MISSING'}")
        
        # Send startup notification FIRST
        self.send_startup_notification()
        time.sleep(2)  # Give time for notifications to send

        while True:
            try:
                now = datetime.now(timezone.utc)
                current_time_str = now.strftime("%Y-%m-%d %H:%M:%S UTC")
                self.update_indicators()
                self.check_signal(current_time_str)

                seconds_since_hour = now.minute * 60 + now.second
                next_15min = ((seconds_since_hour // 900) + 1) * 900 + 5
                sleep_seconds = next_15min - seconds_since_hour
                if sleep_seconds <= 0:
                    sleep_seconds += 900

                logging.info(f"💤 Sleeping {sleep_seconds}s until next 15min candle...")
                time.sleep(sleep_seconds)
            except KeyboardInterrupt:
                logging.info("🛑 Bot stopped by user")
                break
            except Exception as e:
                logging.error(f"Main loop error: {e}")
                time.sleep(30)


if __name__ == "__main__":
    bot = FisherTransformBot()
    bot.run()
