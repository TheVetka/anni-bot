import os
import asyncio
import re
import json
from datetime import datetime, timezone, timedelta
import logging
from playwright.async_api import async_playwright
import requests

logging.basicConfig(level=logging.INFO)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')
STATE_FILE = "state.json"

THRESHOLDS = {
    "10 часов": 600,
    "5 часов": 300,
    "1 час": 60,
    "30 минут": 30
}

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"sent": [], "last_status": None, "last_time": None}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def format_time_left(minutes):
    if minutes < 0:
        return "Время вышло!"
    days = minutes // (60 * 24)
    hours = (minutes % (60 * 24)) // 60
    mins = minutes % 60
    parts = []
    if days > 0:
        parts.append(f"{days}д")
    if hours > 0 or days > 0:
        parts.append(f"{hours}ч")
    parts.append(f"{mins}м")
    return " ".join(parts)

def parse_time_string(time_str):
    """Парсит 'September 22, 2026 at 10:34 AM GMT+4'"""
    try:
        parts = time_str.split()
        months = {
            'January': 1, 'February': 2, 'March': 3, 'April': 4,
            'May': 5, 'June': 6, 'July': 7, 'August': 8,
            'September': 9, 'October': 10, 'November': 11, 'December': 12
        }
        
        month = months.get(parts[0], 1)
        day = int(parts[1].rstrip(','))
        year = int(parts[2])
        hour, minute = map(int, parts[4].split(':'))
        ampm = parts[5]
        
        if ampm == 'PM' and hour != 12:
            hour += 12
        elif ampm == 'AM' and hour == 12:
            hour = 0
        
        tz_offset = timedelta()
        if len(parts) > 6:
            tz_str = parts[6]
            if tz_str.startswith('GMT'):
                tz_val = tz_str[3:]
                sign = 1 if tz_val.startswith('+') else -1
                tz_clean = tz_val.lstrip('+-')
                if len(tz_clean) <= 2:
                    tz_offset = timedelta(hours=int(tz_clean) * sign)
                else:
                    tz_offset = timedelta(hours=int(tz_clean[:2]) * sign, 
                                         minutes=int(tz_clean[2:]) * sign)
            elif tz_str == 'EDT':
                tz_offset = timedelta(hours=-4)
            elif tz_str == 'EST':
                tz_offset = timedelta(hours=-5)
            elif tz_str == 'PDT':
                tz_offset = timedelta(hours=-7)
        
        dt = datetime(year, month, day, hour, minute, tzinfo=timezone(tz_offset))
        return dt.astimezone(timezone.utc)
    except Exception as e:
        logging.error(f"Ошибка парсинга: {e}")
        return None

async def get_annihilation_data():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        logging.info("📡 Загрузка Wynnpool...")
        await page.goto("https://www.wynnpool.com/annihilation", 
                       wait_until="networkidle", timeout=30000)
        await asyncio.sleep(3)
        
        status = "accurate"
        predicted = await page.query_selector('text=Predicted')
        if predicted:
            status = "predicted"
        
        target_time = None
        starts_at = await page.query_selector('text=Starts at:')
        if starts_at:
            text = await starts_at.inner_text()
            match = re.search(r'(\w+\s+\d{1,2},\s+\d{4}\s+at\s+\d{1,2}:\d{2}\s+[AP]M\s+[A-Z0-9:+-]+)', text)
            if match:
                target_time = parse_time_string(match.group(1))
        
        await browser.close()
        return target_time, status

def send_telegram(message):
    """Отправляет сообщение в Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        response = requests.post(url, json=data, timeout=10)
        response.raise_for_status()
        logging.info("✅ Сообщение отправлено")
    except Exception as e:
        logging.error(f"❌ Ошибка отправки: {e}")

async def main():
    state = load_state()
    
    target_time, status = await get_annihilation_data()
    
    if not target_time:
        logging.warning("⚠️ Не удалось получить данные")
        return
    
    now = datetime.now(timezone.utc)
    diff_minutes = int((target_time - now).total_seconds() / 60)
    time_formatted = format_time_left(diff_minutes)
    
    logging.info(f"📊 Статус: {status}, До спавна: {time_formatted}")
    
    if status == "accurate" and state.get("last_status") != "accurate":
        state["sent"] = []
        logging.info("🔄 Статус стал accurate, сбрасываем уведомления")
        send_telegram(f"✅ <b>Annihilation стал Accurate!</b>\n\n До спавна: {time_formatted}\n📅 {target_time.strftime('%Y-%m-%d %H:%M UTC')}")
    
    if status == "accurate":
        for threshold_name, threshold_minutes in THRESHOLDS.items():
            if (threshold_minutes - 10) <= diff_minutes <= threshold_minutes:
                if threshold_name not in state.get("sent", []):
                    msg = (
                        f"✅ <b>Annihilation — {threshold_name}</b>\n\n"
                        f"⏳ Осталось: <b>{time_formatted}</b>\n"
                        f"📅 {target_time.strftime('%Y-%m-%d %H:%M UTC')}\n\n"
                        f"<i>Время точное (Accurate)!</i>"
                    )
                    send_telegram(msg)
                    state.setdefault("sent", []).append(threshold_name)
                    logging.info(f"🔔 Отправлено: {threshold_name}")
    
    if diff_minutes < 0:
        state["sent"] = []
        logging.info(" Время прошло, сбрасываем")
    
    state["last_status"] = status
    state["last_time"] = target_time.isoformat()
    save_state(state)

if __name__ == "__main__":
    asyncio.run(main())
