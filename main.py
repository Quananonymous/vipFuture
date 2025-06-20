import json
import hmac
import hashlib
import time
import threading
import urllib.request
import urllib.parse
import numpy as np
import websocket
import logging
import os
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from config import BINANCE_API_KEY, BINANCE_SECRET_KEY

# Cấu hình logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger()

# Lấy API key từ biến môi trường
# ========== HÀM HỖ TRỢ API ==========
def sign(query):
    try:
        return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    except Exception as e:
        logger.error(f"Sign error: {e}")
        return ""

def get_step_size(symbol):
    url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
    try:
        response = urllib.request.urlopen(url)
        data = json.loads(response.read())
        for s in data['symbols']:
            if s['symbol'] == symbol.upper():
                for f in s['filters']:
                    if f['filterType'] == 'LOT_SIZE':
                        return float(f['stepSize'])
    except Exception as e:
        logger.error(f"Error getting step size: {e}")
    return 0.001

def set_leverage(symbol, lev):
    try:
        ts = int(time.time() * 1000)
        params = {
            "symbol": symbol.upper(),
            "leverage": lev,
            "timestamp": ts
        }
        query = urllib.parse.urlencode(params)
        sig = sign(query)
        url = f"https://fapi.binance.com/fapi/v1/leverage?{query}&signature={sig}"
        req = urllib.request.Request(url, headers={'X-MBX-APIKEY': API_KEY}, method='POST')
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read())
            if 'leverage' in data:
                return True
    except Exception as e:
        logger.error(f"Error setting leverage: {e}")
    return False

def get_balance():
    try:
        ts = int(time.time() * 1000)
        params = {"timestamp": ts}
        query = urllib.parse.urlencode(params)
        sig = sign(query)
        url = f"https://fapi.binance.com/fapi/v2/account?{query}&signature={sig}"
        req = urllib.request.Request(url, headers={'X-MBX-APIKEY': API_KEY})
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read())
            for asset in data['assets']:
                if asset['asset'] == 'USDT':
                    return float(asset['availableBalance'])
    except Exception as e:
        logger.error(f"Error getting balance: {e}")
    return 0

def place_order(symbol, side, qty):
    try:
        ts = int(time.time() * 1000)
        params = {
            "symbol": symbol.upper(),
            "side": side,
            "type": "MARKET",
            "quantity": qty,
            "timestamp": ts
        }
        query = urllib.parse.urlencode(params)
        sig = sign(query)
        url = f"https://fapi.binance.com/fapi/v1/order?{query}&signature={sig}"
        req = urllib.request.Request(url, headers={'X-MBX-APIKEY': API_KEY}, method='POST')
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read())
    except Exception as e:
        logger.error(f"Error placing order: {e}")
    return None

def cancel_all_orders(symbol):
    try:
        ts = int(time.time() * 1000)
        params = {"symbol": symbol.upper(), "timestamp": ts}
        query = urllib.parse.urlencode(params)
        sig = sign(query)
        url = f"https://fapi.binance.com/fapi/v1/allOpenOrders?{query}&signature={sig}"
        req = urllib.request.Request(url, headers={'X-MBX-APIKEY': API_KEY}, method='DELETE')
        urllib.request.urlopen(req)
        return True
    except Exception as e:
        logger.error(f"Error canceling orders: {e}")
    return False

def get_current_price(symbol):
    try:
        url = f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol.upper()}"
        with urllib.request.urlopen(url) as response:
            data = json.loads(response.read())
            return float(data['price'])
    except Exception as e:
        logger.error(f"Error getting price: {e}")
    return 0

def get_positions(symbol=None):
    try:
        ts = int(time.time() * 1000)
        params = {"timestamp": ts}
        if symbol:
            params["symbol"] = symbol.upper()
            
        query = urllib.parse.urlencode(params)
        sig = sign(query)
        url = f"https://fapi.binance.com/fapi/v2/positionRisk?{query}&signature={sig}"
        req = urllib.request.Request(url, headers={'X-MBX-APIKEY': API_KEY})
        with urllib.request.urlopen(req) as response:
            positions = json.loads(response.read())
            
            if symbol:
                for pos in positions:
                    if pos['symbol'] == symbol.upper():
                        return [pos]
            
            return positions
    except Exception as e:
        logger.error(f"Error getting positions: {e}")
    return []

# ========== TÍNH CHỈ BÁO KỸ THUẬT ==========
def calc_rsi(prices, period=14):
    if len(prices) < period + 1:
        return None
    
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    
    if avg_loss == 0:
        return 100.0
    
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1 + rs))

def calc_ema(prices, period=21):
    if len(prices) < period:
        return None
    
    ema = np.mean(prices[:period])
    k = 2 / (period + 1)
    
    for price in prices[period:]:
        ema = price * k + ema * (1 - k)
    
    return ema

# ========== QUẢN LÝ WEBSOCKET HIỆU QUẢ ==========
class WebSocketManager:
    def __init__(self):
        self.connections = {}
        self.executor = ThreadPoolExecutor(max_workers=10)
        self._lock = threading.Lock()
        
    def add_symbol(self, symbol, callback):
        symbol = symbol.upper()
        with self._lock:
            if symbol not in self.connections:
                self._create_connection(symbol, callback)
                
    def _create_connection(self, symbol, callback):
        stream = f"{symbol.lower()}@trade"
        url = f"wss://fstream.binance.com/ws/{stream}"
        
        def on_message(ws, message):
            try:
                data = json.loads(message)
                if 'p' in data:
                    price = float(data['p'])
                    self.executor.submit(callback, price)
            except Exception as e:
                logger.error(f"Message error for {symbol}: {e}")
                
        def on_error(ws, error):
            logger.error(f"WebSocket error for {symbol}: {error}")
            time.sleep(5)
            self._reconnect(symbol, callback)
            
        def on_close(ws, close_status_code, close_msg):
            logger.info(f"WebSocket closed for {symbol}: {close_status_code} - {close_msg}")
            if symbol in self.connections:
                time.sleep(5)
                self._reconnect(symbol, callback)
                
        ws = websocket.WebSocketApp(
            url,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close
        )
        
        thread = threading.Thread(target=ws.run_forever, daemon=True)
        thread.start()
        
        self.connections[symbol] = {
            'ws': ws,
            'thread': thread,
            'callback': callback
        }
        logger.info(f"WebSocket started for {symbol}")
        
    def _reconnect(self, symbol, callback):
        logger.info(f"Reconnecting WebSocket for {symbol}")
        self.remove_symbol(symbol)
        self._create_connection(symbol, callback)
        
    def remove_symbol(self, symbol):
        symbol = symbol.upper()
        with self._lock:
            if symbol in self.connections:
                try:
                    self.connections[symbol]['ws'].close()
                except:
                    pass
                del self.connections[symbol]
                logger.info(f"WebSocket removed for {symbol}")
                
    def stop(self):
        for symbol in list(self.connections.keys()):
            self.remove_symbol(symbol)

# ========== BOT CHÍNH VỚI CƠ CHẾ TỰ ĐÓNG LỆNH ==========
class IndicatorBot:
    def __init__(self, symbol, lev, percent, tp, sl, indicator, ws_manager):
        self.symbol = symbol.upper()
        self.lev = lev
        self.percent = percent
        self.tp = tp
        self.sl = sl
        self.indicator = indicator
        self.ws_manager = ws_manager
        self.status = "waiting"
        self.side = ""
        self.qty = 0
        self.entry = 0
        self.prices = []
        self._stop = False
        self.position_open = False
        self.last_trade_time = 0
        self.last_rsi = 50
        self.position_check_interval = 60
        self.last_position_check = 0
        self.last_error_log_time = 0
        
        self._last_status = None
        
        self.ws_manager.add_symbol(self.symbol, self._handle_price_update)
        
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.log(f"🟢 Bot khởi động cho {self.symbol}")

    def log(self, msg):
        now = datetime.now().strftime("%H:%M:%S")
        logger.info(f"[{now}] {msg}")

    def _handle_price_update(self, price):
        if self._stop: 
            return
            
        self.prices.append(price)
        if len(self.prices) > 100:
            self.prices = self.prices[-100:]

    def _run(self):
        while not self._stop:
            try:
                current_time = time.time()
                
                if current_time - self.last_position_check > self.position_check_interval:
                    self.check_position_status()
                    self.last_position_check = current_time
                
                if not self.position_open and self.status == "waiting":
                    signal = self.get_signal()
                    
                    if signal and current_time - self.last_trade_time > 60:
                        self.open_position(signal)
                        self.last_trade_time = current_time
                
                if self.position_open and self.status == "open":
                    self.check_tp_sl()
                
                time.sleep(1)
                
            except Exception as e:
                if time.time() - self.last_error_log_time > 10:
                    self.log(f"Bot error for {self.symbol}: {e}")
                    self.last_error_log_time = time.time()
                time.sleep(5)

    def stop(self):
        self._stop = True
        self.ws_manager.remove_symbol(self.symbol)
        try:
            cancel_all_orders(self.symbol)
        except Exception as e:
            if time.time() - self.last_error_log_time > 10:
                self.log(f"Lỗi hủy lệnh: {e}")
                self.last_error_log_time = time.time()
        self.log(f"🔴 Bot dừng cho {self.symbol}")

    def check_position_status(self):
        try:
            positions = get_positions(self.symbol)
            
            if not positions or len(positions) == 0:
                self.position_open = False
                self.status = "waiting"
                self.side = ""
                self.qty = 0
                self.entry = 0
                return
            
            for pos in positions:
                if pos['symbol'] == self.symbol:
                    position_amt = float(pos['positionAmt'])
                    
                    if abs(position_amt) > 0:
                        self.position_open = True
                        self.status = "open"
                        self.side = "BUY" if position_amt > 0 else "SELL"
                        self.qty = position_amt
                        self.entry = float(pos['entryPrice'])
                        return
            
            self.position_open = False
            self.status = "waiting"
            self.side = ""
            self.qty = 0
            self.entry = 0
            
        except Exception as e:
            if time.time() - self.last_error_log_time > 10:
                self.log(f"Lỗi kiểm tra vị thế: {e}")
                self.last_error_log_time = time.time()

    def check_tp_sl(self):
        if not self.position_open or not self.entry or not self.qty:
            return
            
        try:
            if len(self.prices) > 0:
                current_price = self.prices[-1]
            else:
                current_price = get_current_price(self.symbol)
                
            if current_price <= 0:
                return
                
            if self.side == "BUY":
                profit = (current_price - self.entry) * self.qty
            else:
                profit = (self.entry - current_price) * abs(self.qty)
                
            invested = self.entry * abs(self.qty) / self.lev
            roi = (profit / invested) * 100
            
            if roi >= self.tp:
                self.close_position(f"✅ Đạt TP {self.tp}% (ROI: {roi:.2f}%)")
            elif roi <= -self.sl:
                self.close_position(f"❌ Đạt SL {self.sl}% (ROI: {roi:.2f}%)")
                
        except Exception as e:
            if time.time() - self.last_error_log_time > 10:
                self.log(f"Lỗi kiểm tra TP/SL: {e}")
                self.last_error_log_time = time.time()

    def get_signal(self):
        if len(self.prices) < 40:
            return None
            
        prices_arr = np.array(self.prices)
        rsi_val = calc_rsi(prices_arr)
        
        if rsi_val is not None:
            if rsi_val <= 30: 
                return "BUY"
            if rsi_val >= 70: 
                return "SELL"
                    
        return None

    def open_position(self, side):
        self.check_position_status()
        
        if self.position_open:
            self.log(f"⚠️ {self.symbol} đã có vị thế mở, không vào lệnh mới")
            return
            
        try:
            cancel_all_orders(self.symbol)
            
            if not set_leverage(self.symbol, self.lev):
                self.log(f"Không thể đặt đòn bẩy {self.lev} cho {self.symbol}")
                return
            
            balance = get_balance()
            if balance <= 0:
                self.log(f"Không đủ số dư USDT cho {self.symbol}")
                return
            
            usdt_amount = balance * (self.percent / 100)
            price = get_current_price(self.symbol)
            if price <= 0:
                self.log(f"Lỗi lấy giá cho {self.symbol}")
                return
                
            step = get_step_size(self.symbol)
            if step <= 0:
                step = 0.001
            
            qty = (usdt_amount * self.lev) / price
            if step > 0:
                steps = qty / step
                qty = round(steps) * step
            
            qty = max(qty, 0)
            qty = round(qty, 8)
            
            min_qty = step
            self.log(f"ℹ️ {self.symbol} - Số lượng: {qty}, Step: {step}")
            
            if qty < min_qty:
                self.log(f"⚠️ Số lượng quá nhỏ ({qty}), không đặt lệnh")
                return
                
            res = place_order(self.symbol, side, qty)
            if not res:
                self.log(f"Lỗi khi đặt lệnh cho {self.symbol}")
                return
                
            executed_qty = float(res.get('executedQty', 0))
            if executed_qty <= 0:
                self.log(f"Lệnh không khớp, số lượng thực thi: {executed_qty}")
                return

            self.entry = float(res.get('avgPrice', price))
            self.side = side
            self.qty = executed_qty if side == "BUY" else -executed_qty
            self.status = "open"
            self.position_open = True
            
            self.log(f"✅ Đã vào lệnh {self.symbol} {side} tại {self.entry:.4f}")
            self.log(f"📊 Số lượng: {executed_qty}, Giá trị: {executed_qty * self.entry:.2f} USDT")
            self.log(f"🎯 TP: {self.tp}%, 🛡️ SL: {self.sl}%")

        except Exception as e:
            self.position_open = False
            self.log(f"❌ Lỗi khi vào lệnh {self.symbol}: {e}")

    def close_position(self, reason=""):
        try:
            cancel_all_orders(self.symbol)
            
            if abs(self.qty) > 0:
                close_side = "SELL" if self.side == "BUY" else "BUY"
                close_qty = abs(self.qty)
                
                step = get_step_size(self.symbol)
                if step > 0:
                    steps = close_qty / step
                    close_qty = round(steps) * step
                
                close_qty = max(close_qty, 0)
                close_qty = round(close_qty, 8)
                
                res = place_order(self.symbol, close_side, close_qty)
                if res:
                    price = float(res.get('avgPrice', 0))
                    self.log(f"Đã đóng lệnh {self.symbol} tại {price:.4f} {reason}")
                else:
                    self.log(f"Lỗi khi đóng lệnh {self.symbol}")
                    
            time.sleep(1)
            self.check_position_status()
            
            if self.position_open:
                self.log(f"⚠️ Vị thế {self.symbol} chưa đóng, thử đóng lại")
                self.close_position("Thử đóng lại")
                return
                    
            self.status = "waiting"
            self.side = ""
            self.qty = 0
            self.entry = 0
            self.position_open = False
            self.last_trade_time = time.time()
            
        except Exception as e:
            self.log(f"❌ Lỗi khi đóng lệnh {self.symbol}: {e}")

# ========== QUẢN LÝ BOT ==========
class BotManager:
    def __init__(self):
        self.ws_manager = WebSocketManager()
        self.bots = {}
        
    def add_bot(self, symbol, lev, percent, tp, sl, indicator):
        if symbol in self.bots:
            logger.warning(f"Bot for {symbol} is already running")
            return
            
        bot = IndicatorBot(
            symbol, lev, percent, tp, sl, 
            indicator, self.ws_manager
        )
        self.bots[symbol] = bot
        logger.info(f"✅ Đã thêm bot cho {symbol} với chỉ báo {indicator}")

    def stop_bot(self, symbol):
        bot = self.bots.get(symbol)
        if bot:
            bot.stop()
            if bot.status == "open":
                bot.close_position("⛔ Dừng bot thủ công")
            logger.info(f"⛔ Đã dừng bot cho {symbol}")
            del self.bots[symbol]
            
    def stop_all(self):
        for symbol, bot in list(self.bots.items()):
            self.stop_bot(symbol)
        self.ws_manager.stop()

# ========== CẤU HÌNH TỪ BIẾN MÔI TRƯỜNG ==========
def load_config_from_env():
    manager = BotManager()
    
    # Đọc cấu hình từ biến môi trường
    symbols = os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT").split(",")
    lev = int(os.getenv("LEVERAGE", 50))
    percent = float(os.getenv("PERCENT", 5.0))
    tp = float(os.getenv("TAKE_PROFIT", 10.0))
    sl = float(os.getenv("STOP_LOSS", 5.0))
    indicator = os.getenv("INDICATOR", "RSI")
    
    for symbol in symbols:
        manager.add_bot(
            symbol=symbol.strip(),
            lev=lev,
            percent=percent,
            tp=tp,
            sl=sl,
            indicator=indicator
        )
    
    return manager

if __name__ == "__main__":
    logger.info("🟢 Hệ thống đã sẵn sàng")
    logger.info(f"🔑 API Key: {'Đã cài đặt' if API_KEY else 'Chưa cài đặt'}")
    
    manager = load_config_from_env()
    
    try:
        # Giữ chương trình chạy
        while True:
            time.sleep(60)
            logger.info("🔄 Bot đang hoạt động...")
    except KeyboardInterrupt:
        logger.info("⛔ Nhận tín hiệu dừng, đang dừng bot...")
        manager.stop_all()
    except Exception as e:
        logger.error(f"Lỗi không mong muốn: {e}")
        manager.stop_all()
