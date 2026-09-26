import os
import asyncio
import re
import json
from datetime import datetime, timezone, timedelta
import logging
from playwright.async_api import async_playwright
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from flask import Flask
import threading

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
STATE_FILE = "users.json"

THRESHOLDS = {
    "10 часов": 600,
    "5 часов": 300,
    "1 час": 60,
    "30 минут": 30
}

users = {}

# === ПЕРЕМЕННЫЕ ДЛЯ КЭШИРОВАНИЯ ===
cached_spawn_time = None
last_status = "predicted"
last_fetch_time = None
CACHE_DURATION = 600  # Обновлять данные с сайта раз в 10 минут

# === FLASK СЕРВЕР ДЛЯ RENDER (чтобы не засыпал) ===
app = Flask(__name__)

@app.route('/')
def home():
    return "Бот работает!"

@app.route('/health')
def health():
    return "OK"

def run_server():
    app.run(host='0.0.0.0', port=10000)

def get_main_keyboard(user_data):
    """Генерирует клавиатуру в зависимости от статуса уведомлений пользователя."""
    is_enabled = user_data.get("enabled", False)
    
    # Умная кнопка: меняет текст и действие в зависимости от статуса
    toggle_text = "🔕 Выключить уведомления" if is_enabled else "🔔 Включить уведомления"
    toggle_action = "disable" if is_enabled else "enable"
    
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle_text, callback_data=toggle_action)],
        [InlineKeyboardButton("⏱ Проверить таймер", callback_data="check_timer")],
        [InlineKeyboardButton("🌐 Открыть Wynnpool", url="https://www.wynnpool.com/annihilation")]
    ])

def load_users():
    global users
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                users = json.load(f)
        except:
            users = {}

def save_users():
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)

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
                    tz_offset = timedelta(hours=int(tz_clean[:2]) * sign, minutes=int(tz_clean[2:]) * sign)
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

async def fetch_annihilation_data():
    global cached_spawn_time, last_status, last_fetch_time
    try:
        logging.info("📡 Загрузка данных с Wynnpool (Playwright)...")
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            
            await page.goto("https://www.wynnpool.com/annihilation", wait_until="networkidle", timeout=30000)
            await asyncio.sleep(3)
            
            status = "predicted"
            html = await page.content()
            
            # Ищем зелёный бейдж Accurate
            if ('bg-green' in html or 'text-green' in html) and ('>Accurate<' in html or '>Accurate</' in html):
                status = "accurate"
                logging.info("✅ Найден статус: Accurate")
            else:
                logging.info("📊 Статус: Предикт")
            
            target_time = None
            starts_at = await page.query_selector('text=Starts at:')
            if starts_at:
                text = await starts_at.inner_text()
                logging.info(f"Найдено время: {text}")
                match = re.search(r'(\w+\s+\d{1,2},\s+\d{4}\s+at\s+\d{1,2}:\d{2}\s+[AP]M\s+[A-Z0-9:+-]+)', text)
                if match:
                    target_time = parse_time_string(match.group(1))
            
            await browser.close()
            
            if target_time:
                cached_spawn_time = target_time
                last_status = status
                last_fetch_time = datetime.now(timezone.utc)
                logging.info(f"✅ Данные обновлены: Статус={status}, Время={target_time}")
            
            return cached_spawn_time, last_status
    except Exception as e:
        logging.error(f"❌ Ошибка Playwright: {e}")
        return cached_spawn_time, last_status
        
async def get_annihilation_data():
    global cached_spawn_time, last_status, last_fetch_time
    
    if cached_spawn_time is None or last_fetch_time is None:
        return await fetch_annihilation_data()
    
    time_since_fetch = (datetime.now(timezone.utc) - last_fetch_time).total_seconds()
    if time_since_fetch > CACHE_DURATION:
        return await fetch_annihilation_data()
    
    return cached_spawn_time, last_status

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.first_name
    
    if str(user_id) not in users:
        users[str(user_id)] = {"enabled": False, "sent": [], "username": username}
        save_users()
    
    user_data = users[str(user_id)] # Получаем актуальные данные пользователя
    
    text = (
        f"👋 Привет, {username}!\n\n"
        f"🎮 Я бот для отслеживания босса <b>Annihilation</b> в Wynncraft!\n\n"
        f"📊 <b>Статусы:</b>\n"
        f"• <b>Предикт</b> — примерное время\n"
        f"• <b>Точное время</b> — появляется за ~10 часов до спавна\n\n"
        f"💡 Уведомления приходят <b>только</b> когда статус становится «Точное время».\n\n"
        f"Текущий статус уведомлений: {'✅ Включены' if user_data.get('enabled') else '❌ Выключены'}\n\n"
        f"Используй кнопки ниже для управления:"
    )
    await update.message.reply_text(text, reply_markup=get_main_keyboard(user_data), parse_mode='HTML')

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = users.get(str(user_id), {"enabled": False, "sent": []})
    target_time, status = await get_annihilation_data()
    
    status_emoji = "📊" if status == "predicted" else "✅"
    status_text_rus = "Предикт" if status == "predicted" else "Точное время"
    
    text = f"{status_emoji} <b>Статус: {status_text_rus}</b>\n\n"
    
    if target_time:
        target_utc = target_time.astimezone(timezone.utc)
        now = datetime.now(timezone.utc)
        diff_minutes = int((target_utc - now).total_seconds() / 60)
        
        if diff_minutes > 0:
            text += f"⏳ <b>До спавна:</b> {format_time_left(diff_minutes)}\n"
            text += f"📅 {target_utc.strftime('%Y-%m-%d %H:%M UTC')}"
        else:
            text += "⏰ <b>Время вышло!</b>"
    else:
        text += "⚠️ <b>Нет данных</b>"
    
    text += f"\n\n🔔 {'Включены ✅' if user_data.get('enabled') else 'Выключены ❌'}"
    await update.message.reply_text(text, reply_markup=get_main_keyboard(user_data), parse_mode='HTML')

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    data = query.data
    
    if str(user_id) not in users:
        users[str(user_id)] = {"enabled": False, "sent": [], "username": query.from_user.first_name}
    
    user_data = users[str(user_id)]
    
    try:
        # Обрабатываем и включение, и выключение одной логикой
        if data == "enable" or data == "disable":
            new_state = (data == "enable")
            user_data["enabled"] = new_state
            
            if new_state:
                user_data["sent"] = [] # Сбрасываем историю при включении
            
            save_users()
            
            status_text = "✅ <b>Уведомления включены!</b>\n\nТеперь ты будешь получать оповещения, когда статус сменится на «Точное время»." if new_state else "🔕 <b>Уведомления выключены.</b>"
            
            # Возвращаем клавиатуру, которая автоматически обновится под новый статус
            await query.edit_message_text(status_text, reply_markup=get_main_keyboard(user_data), parse_mode='HTML')
        
        elif data == "check_timer":
            await query.edit_message_text("⏳ Загрузка...", parse_mode='HTML')
            target_time, status = await get_annihilation_data()
            
            status_emoji = "📊" if status == "predicted" else "✅"
            status_text_rus = "Предикт" if status == "predicted" else "Точное время"
            
            if target_time:
                target_utc = target_time.astimezone(timezone.utc)
                now = datetime.now(timezone.utc)
                diff_minutes = int((target_utc - now).total_seconds() / 60)
                
                if diff_minutes > 0:
                    text = f"{status_emoji} <b>{status_text_rus}</b>\n\n⏳ <b>До спавна:</b> {format_time_left(diff_minutes)}\n📅 {target_utc.strftime('%Y-%m-%d %H:%M UTC')}"
                else:
                    text = f"{status_emoji} <b>{status_text_rus}</b>\n\n⏰ <b>Время вышло!</b>"
            else:
                text = "⚠️ Нет данных"
            
            keyboard = [
                [InlineKeyboardButton("🔄 Обновить", callback_data="check_timer")],
                [InlineKeyboardButton("🔙 В главное меню", callback_data="back_to_menu")]
            ]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        
        elif data == "back_to_menu":
            text = (
                f"📋 <b>Главное меню</b>\n\n"
                f"🔔 Уведомления: {'Включены ✅' if user_data.get('enabled') else 'Выключены ❌'}\n\n"
                f"Используй кнопки ниже:"
            )
            await query.edit_message_text(text, reply_markup=get_main_keyboard(user_data), parse_mode='HTML')
    
    except Exception as e:
        logging.error(f"❌ Ошибка в button_handler: {e}")
        await query.answer("Произошла ошибка. Попробуй написать /start", show_alert=True)

async def check_notifications(application: Application):
    global cached_spawn_time, last_status
    
    while True:
        try:
            target_time, status = await get_annihilation_data()
            
            if not target_time:
                logging.warning("⚠️ Нет данных о спавне")
                await asyncio.sleep(300)
                continue
            
            target_utc = target_time.astimezone(timezone.utc)
            now = datetime.now(timezone.utc)
            diff_seconds = (target_utc - now).total_seconds()
            diff_minutes = int(diff_seconds / 60)
            
            logging.info(f"📊 Статус: {status}, До спавна: {format_time_left(diff_minutes)}")
            
            if status == "accurate":
                for user_id_str, user_data in users.items():
                    if not user_data.get("enabled"):
                        continue
                    
                    user_id = int(user_id_str)
                    
                    for threshold_name, threshold_minutes in THRESHOLDS.items():
                        if (threshold_minutes - 5) <= diff_minutes <= threshold_minutes:
                            if threshold_name not in user_data.get("sent", []):
                                try:
                                    msg = (
                                        f"✅ <b>Annihilation — {threshold_name}</b>\n\n"
                                        f"⏳ Осталось: <b>{format_time_left(diff_minutes)}</b>\n"
                                        f"📅 {target_utc.strftime('%Y-%m-%d %H:%M UTC')}\n\n"
                                        f"<i>Время точное!</i>"
                                    )
                                    await application.bot.send_message(chat_id=user_id, text=msg, parse_mode='HTML', reply_markup=get_main_keyboard(user_data))
                                    user_data.setdefault("sent", []).append(threshold_name)
                                    save_users()
                                    logging.info(f"✅ Уведомление отправлено: {user_id} - {threshold_name}")
                                except Exception as e:
                                    logging.error(f"❌ Ошибка отправки {user_id}: {e}")
                    
                    if diff_minutes < 0:
                        user_data["sent"] = []
                        save_users()
                        logging.info(f"🔄 Сброшены уведомления для {user_id}")
            else:
                for user_id_str, user_data in users.items():
                    if user_data.get("sent"):
                        user_data["sent"] = []
                        save_users()
                        logging.info(f"🔄 Сброшены уведомления (статус Предикт) для {user_id_str}")
            
        except Exception as e:
            logging.error(f"❌ Ошибка в check_notifications: {e}")
        
        await asyncio.sleep(300)

async def main():
    logging.info("🚀 Запуск бота...")
    load_users()

    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    logging.info("🌐 HTTP-сервер запущен на порту 10000")
    
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CallbackQueryHandler(button_handler))
    
    check_task = asyncio.create_task(check_notifications(application))
    
    await application.initialize()
    await application.start()
    await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    
    logging.info("✅ Бот запущен и готов к работе!")
    
    try:
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        check_task.cancel()
        await application.stop()
        await application.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
