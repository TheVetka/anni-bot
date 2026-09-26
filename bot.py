import os
import asyncio
import json
import requests
from datetime import datetime, timezone, timedelta
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from flask import Flask
import threading
import time
import io
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from telegram import InlineQueryResultArticle, InputTextMessageContent
from telegram.ext import InlineQueryHandler

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

# === НАСТРОЙКИ ===
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))
STATE_FILE = "users.json"
HISTORY_FILE = "history.json"
BOSS_GIF_URL = "https://raw.githubusercontent.com/TheVetka/anni-bot/main/annihilation.gif"

START_TIME = time.time()

THRESHOLDS_DEF = {
    "10h": {"name_ru": "10 часов", "name_en": "10 hours", "mins": 600},
    "5h": {"name_ru": "5 часов", "name_en": "5 hours", "mins": 300},
    "1h": {"name_ru": "1 час", "name_en": "1 hour", "mins": 60},
    "30m": {"name_ru": "30 минут", "name_en": "30 minutes", "mins": 30}
}

# === СОСТОЯНИЕ ===
users = {}
spawn_history = []
cached_spawn_time = None
last_status = "predicted"
last_fetch_time = None
last_notified_status = "predicted"
CACHE_DURATION = 300 # Обновляем API каждые 5 минут (оно и так быстрое)
last_spawn_time = datetime(2026, 9, 25, 12, 55, tzinfo=timezone.utc)

# === I18n ===
LANG = {
    "ru": {
        "start": "👋 Привет, {name}!\n\n🎮 Я бот для отслеживания <b>Annihilation</b> через официальный API Wynncraft.\n\n📊 <b>Статусы:</b>\n• 📊 Предикт — точное время еще не объявлено сервером\n• ✅ Точное время — спавн официально запланирован\n\n💡 Уведомления приходят только в статусе «Точное время».",
        "status": "Статус: {status}",
        "time_left": "⏳ <b>До спавна:</b> {time}\n{bar}\n📅 {date}",
        "time_up": "⏰ <b>Время вышло!</b>",
        "no_data": "⚠️ <b>Нет данных</b> (сервер еще не объявил время)",
        "pred": "Предикт",
        "acc": "Точное время",
        "enabled": "Включены ✅",
        "disabled": "Выключены ❌",
        "history_title": "📜 <b>История спавнов:</b>",
        "no_history": "История пока пуста.",
        "ping": "🏓 Понг! Бот работает.\n⏱ Аптайм: {uptime}"
    },
    "en": {
        "start": "👋 Hi, {name}!\n\n🎮 I'm the <b>Annihilation</b> tracker bot via official Wynncraft API.\n\n📊 <b>Statuses:</b>\n• 📊 Predicted — exact time not yet announced\n• ✅ Accurate — spawn is officially scheduled\n\n💡 Notifications only trigger in 'Accurate' status.",
        "status": "Status: {status}",
        "time_left": "⏳ <b>Time left:</b> {time}\n{bar}\n📅 {date}",
        "time_up": "⏰ <b>Time is up!</b>",
        "no_data": "⚠️ <b>No data</b> (server hasn't announced time yet)",
        "pred": "Predicted",
        "acc": "Accurate",
        "enabled": "Enabled ✅",
        "disabled": "Disabled ❌",
        "history_title": "📜 <b>Spawn History:</b>",
        "no_history": "History is empty.",
        "ping": "🏓 Pong! Bot is alive.\n⏱ Uptime: {uptime}"
    }
}

def get_text(key, lang="ru", **kwargs):
    text = LANG.get(lang, LANG["ru"]).get(key, key)
    return text.format(**kwargs) if kwargs else text

# === FLASK ДЛЯ RENDER ===
app = Flask(__name__)
@app.route('/')
def home(): return "Бот работает!"
@app.route('/health')
def health(): return "OK"
def run_server(): app.run(host='0.0.0.0', port=10000)

# === УТИЛИТЫ ===
def load_data():
    global users, spawn_history
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                raw_users = json.load(f)
                for uid, data in raw_users.items():
                    if "thresholds" not in data: data["thresholds"] = {"10h": True, "5h": True, "1h": True, "30m": True}
                    if "lang" not in data: data["lang"] = "ru"
                    if "last_msg_id" not in data: data["last_msg_id"] = None
                    users[uid] = data
        except: users = {}
    
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                spawn_history = json.load(f)
        except: spawn_history = []

def save_data():
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(spawn_history, f, ensure_ascii=False, indent=2)

def format_time_left(total_seconds):
    if total_seconds < 0: 
        return "Время вышло!", ""
    
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    mins = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    
    parts = []
    if days > 0: parts.append(f"{days}д")
    if hours > 0 or days > 0: parts.append(f"{hours}ч")
    if mins > 0 or hours > 0 or days > 0: parts.append(f"{mins}м")
    parts.append(f"{secs}с")
    time_str = " ".join(parts)
    
    # Прогресс-бар: 0% = 3 дня (259200 сек), 100% = 0 сек
    max_seconds = 259200  # 3 дня в секундах
    if total_seconds >= max_seconds:
        progress = 0
    elif total_seconds <= 0:
        progress = 100
    else:
        progress = int(((max_seconds - total_seconds) / max_seconds) * 100)
    
    filled = int(progress / 5)
    bar = "█" * filled + "░" * (20 - filled) + f" {progress}%"
    
    return time_str, bar

def get_average_interval_hours():
    """Считает средний интервал между спавнами на основе истории."""
    if len(spawn_history) < 2:
        return 80.0  # Запасной вариант, если истории мало
    
    intervals = []
    for i in range(1, len(spawn_history)):
        t1 = datetime.strptime(spawn_history[i-1]['time'], '%Y-%m-%d %H:%M UTC').replace(tzinfo=timezone.utc)
        t2 = datetime.strptime(spawn_history[i]['time'], '%Y-%m-%d %H:%M UTC').replace(tzinfo=timezone.utc)
        intervals.append((t2 - t1).total_seconds() / 3600)
    
    return sum(intervals) / len(intervals)

async def fetch_annihilation_data():
    global cached_spawn_time, last_status, last_fetch_time, last_notified_status, last_spawn_time
    try:
        logging.info("📡 Загрузка данных из официального API Wynncraft...")
        response = requests.get("https://api.wynncraft.com/v3/map/world-events", timeout=10)
        response.raise_for_status()
        data = response.json()
        
        annihilation_event = None
        for event in data:
            if event.get("name") == "Prelude to Annihilation":
                annihilation_event = event
                break
        
        if annihilation_event:
            schedule = annihilation_event.get("schedule")
            if schedule:
                target_time = datetime.fromisoformat(schedule.replace('Z', '+00:00'))
                cached_spawn_time = target_time
                last_status = "accurate"
                logging.info(f"✅ API: Точное время {target_time}")
            else:
                last_status = "predicted"
                if last_spawn_time:
                    # FEATURE 1: Используем динамический средний интервал вместо фиксированных 80 часов
                    avg_hours = get_average_interval_hours()
                    cached_spawn_time = last_spawn_time + timedelta(hours=avg_hours)
                    logging.info(f"📊 API: Предикт (расчет на основе среднего интервала {avg_hours:.1f}ч)")
                else:
                    cached_spawn_time = None
                    logging.info("📊 API: Предикт (нет данных о прошлом спавне)")
            
            last_fetch_time = datetime.now(timezone.utc)
            return cached_spawn_time, last_status
        else:
            logging.warning("⚠️ Событие Annihilation не найдено в API")
            return cached_spawn_time, last_status
            
    except Exception as e:
        logging.error(f"❌ Ошибка API: {e}")
        return cached_spawn_time, last_status

async def get_annihilation_data():
    global cached_spawn_time, last_status, last_fetch_time
    if cached_spawn_time is None or last_fetch_time is None:
        return await fetch_annihilation_data()
    if (datetime.now(timezone.utc) - last_fetch_time).total_seconds() > CACHE_DURATION:
        return await fetch_annihilation_data()
    return cached_spawn_time, last_status

def get_main_kb(user_data):
    lang = user_data.get("lang", "ru")
    is_enabled = user_data.get("enabled", False)
    toggle_text = "🔕 " + ("Выключить уведомления" if lang=="ru" else "Disable Notifications") if is_enabled else "🔔 " + ("Включить уведомления" if lang=="ru" else "Enable Notifications")
    toggle_action = "disable" if is_enabled else "enable"
    
    history_text = "📜 " + ("История" if lang=="ru" else "History")
    graph_text = "📊 " + ("График" if lang=="ru" else "Graph")
    
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle_text, callback_data=toggle_action)],
        [InlineKeyboardButton("⚙️ " + ("Настройки" if lang=="ru" else "Settings"), callback_data="settings")],
        [InlineKeyboardButton("⏱ " + ("Проверить таймер" if lang=="ru" else "Check Timer"), callback_data="check_timer")],
        [InlineKeyboardButton(history_text, callback_data="show_history"),
         InlineKeyboardButton(graph_text, callback_data="show_graph")],
        [InlineKeyboardButton("🌐 Wynncraft Wiki", url="https://wynncraft.wiki.gg/wiki/Prelude_to_Annihilation")]
    ])

def get_settings_kb(user_data):
    lang = user_data.get("lang", "ru")
    rows = []
    for key, val in THRESHOLDS_DEF.items():
        name = val["name_ru"] if lang == "ru" else val["name_en"]
        is_on = user_data.get("thresholds", {}).get(key, True)
        icon = "✅" if is_on else "❌"
        rows.append([InlineKeyboardButton(f"{icon} {name}", callback_data=f"toggle_{key}")])
    
    lang_btn = "🇬🇧 English" if lang == "ru" else "🇷🇺 Русский"
    lang_act = "lang_en" if lang == "ru" else "lang_ru"
    rows.append([InlineKeyboardButton(lang_btn, callback_data=lang_act)])
    rows.append([InlineKeyboardButton("🔙 " + ("Назад" if lang=="ru" else "Back"), callback_data="back_to_menu")])
    return InlineKeyboardMarkup(rows)

# === ОБРАБОТЧИКИ ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid not in users:
        users[uid] = {"enabled": False, "sent": [], "thresholds": {"10h": True, "5h": True, "1h": True, "30m": True}, "lang": "ru", "last_msg_id": None, "username": update.effective_user.first_name}
        save_data()
    
    text = get_text("start", users[uid]["lang"], name=update.effective_user.first_name)
    text += f"\n\n🔔 {get_text('enabled' if users[uid]['enabled'] else 'disabled', users[uid]['lang'])}"
    await update.message.reply_text(text, reply_markup=get_main_kb(users[uid]), parse_mode='HTML')

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uptime_sec = int(time.time() - START_TIME)
    uptime_str = f"{uptime_sec // 86400}д {(uptime_sec % 86400) // 3600}ч {(uptime_sec % 3600) // 60}м"
    await update.message.reply_text(get_text("ping", "ru", uptime=uptime_str))

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID and ADMIN_ID != 0:
        await update.message.reply_text("❌ Доступ запрещен.")
        return
    total = len(users)
    active = sum(1 for u in users.values() if u.get("enabled"))
    uptime_sec = int(time.time() - START_TIME)
    await update.message.reply_text(f"📊 <b>Статистика:</b>\n👥 Всего пользователей: {total}\n🔔 Активных подписок: {active}\n⏱ Аптайм: {uptime_sec} сек", parse_mode='HTML')

async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = users.get(str(update.effective_user.id), {}).get("lang", "ru")
    if not spawn_history:
        await update.message.reply_text(get_text("no_history", lang))
        return
    text = get_text("history_title", lang) + "\n"
    for h in spawn_history[-5:]:
        text += f"• {h['time']} ({h['status']})\n"
    await update.message.reply_text(text, parse_mode='HTML')

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await check_timer(update, context, is_new_msg=True)

async def check_timer(update: Update, context: ContextTypes.DEFAULT_TYPE, is_new_msg=False):

    uid = str(update.effective_user.id)
    lang = users.get(uid, {}).get("lang", "ru")
    
    if not is_new_msg:
        await update.callback_query.edit_message_text("⏳...", parse_mode='HTML')
    
    target_time, status = await get_annihilation_data()
    status_emoji = "📊" if status == "predicted" else "✅"
    status_text = get_text("pred", lang) if status == "predicted" else get_text("acc", lang)
    
    if target_time:
        target_utc = target_time.astimezone(timezone.utc)
        now = datetime.now(timezone.utc)
        diff_seconds = int((target_utc - now).total_seconds())
        diff_minutes = diff_seconds // 60  # Для проверки порогов уведомлений
        time_str, bar = format_time_left(diff_seconds)
        
        if diff_seconds > 0:
            text = f"{status_emoji} <b>{get_text('status', lang, status=status_text)}</b>\n\n" + get_text("time_left", lang, time=time_str, bar=bar, date=target_utc.strftime('%Y-%m-%d %H:%M UTC'))
        else:
            # Если время "вышло" но это Предикт
            if status == "predicted":
                text = f"{status_emoji} <b>{get_text('status', lang, status=status_text)}</b>\n\n"
                text += f"⚠️ <b>Ожидается спавн в любой момент!</b>\n"
                text += f" Расчётное время: {target_utc.strftime('%Y-%m-%d %H:%M UTC')}\n"
                text += f"<i>Точное время появится когда сервер объявит расписание</i>"
            else:
                text = f"{status_emoji} <b>{get_text('status', lang, status=status_text)}</b>\n\n" + get_text("time_up", lang)
    else:
        text = f"{status_emoji} <b>{get_text('status', lang, status=status_text)}</b>\n\n" + get_text("no_data", lang)
    
    kb = get_main_kb(users.get(uid, {})) if is_new_msg else InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 " + ("Обновить" if lang=="ru" else "Refresh"), callback_data="check_timer")],
        [InlineKeyboardButton("🔙 " + ("Меню" if lang=="ru" else "Menu"), callback_data="back_to_menu")]
    ])
    
    if is_new_msg:
        await update.message.reply_text(text, reply_markup=kb, parse_mode='HTML')
    else:
        await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode='HTML')

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = str(query.from_user.id)
    if uid not in users: users[uid] = {"enabled": False, "thresholds": {"10h": True, "5h": True, "1h": True, "30m": True}, "lang": "ru", "last_msg_id": None}
    
    user = users[uid]
    lang = user.get("lang", "ru")
    
    try:
        if query.data in ["enable", "disable"]:
            user["enabled"] = (query.data == "enable")
            if user["enabled"]: user["sent"] = []
            save_data()
            msg = "✅ " + ("Уведомления включены!" if lang=="ru" else "Notifications enabled!") if user["enabled"] else "🔕 " + ("Уведомления выключены." if lang=="ru" else "Notifications disabled.")
            await query.edit_message_text(msg, reply_markup=get_main_kb(user), parse_mode='HTML')
        
        elif query.data.startswith("toggle_"):
            key = query.data.split("_")[1]
            user["thresholds"][key] = not user["thresholds"].get(key, True)
            save_data()
            await query.edit_message_text("⚙️ " + ("Настройки уведомлений:" if lang=="ru" else "Notification settings:"), reply_markup=get_settings_kb(user), parse_mode='HTML')
        
        elif query.data.startswith("lang_"):
            user["lang"] = "en" if query.data == "lang_en" else "ru"
            save_data()
            await query.edit_message_text("⚙️ " + ("Настройки:" if lang=="ru" else "Settings:"), reply_markup=get_settings_kb(user), parse_mode='HTML')
            
        elif query.data == "settings":
            await query.edit_message_text("⚙️ " + ("Настройки уведомлений:" if lang=="ru" else "Notification settings:"), reply_markup=get_settings_kb(user), parse_mode='HTML')
            
        elif query.data == "check_timer":
            await check_timer(update, context, is_new_msg=False)

        elif query.data == "show_history":
            lang = user.get("lang", "ru")
            if not spawn_history:
                text = "📜 " + ("История спавнов пуста." if lang=="ru" else "Spawn history is empty.")
            else:
                text = "📜 <b>" + ("История последних спавнов:" if lang=="ru" else "Recent Spawns:") + "</b>\n"
                for h in reversed(spawn_history[-5:]):
                    text += f"• {h['time']} ({h['status']})\n"
            
            keyboard = [[InlineKeyboardButton("🔙 " + ("Назад" if lang=="ru" else "Back"), callback_data="back_to_menu")]]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        
        elif query.data == "show_graph":
            lang = user.get("lang", "ru")
            if len(spawn_history) < 2:
                text = "⚠️ " + ("Недостаточно данных для графика. Нужно минимум 2 спавна." if lang=="ru" else "Not enough data for graph. Need at least 2 spawns.")
                keyboard = [[InlineKeyboardButton("🔙 " + ("Назад" if lang=="ru" else "Back"), callback_data="back_to_menu")]]
                await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
            else:
                # Строим график
                import io
                import matplotlib
                matplotlib.use('Agg')
                import matplotlib.pyplot as plt
                
                times = []
                intervals = []
                for i in range(1, len(spawn_history)):
                    t1 = datetime.strptime(spawn_history[i-1]['time'], '%Y-%m-%d %H:%M UTC')
                    t2 = datetime.strptime(spawn_history[i]['time'], '%Y-%m-%d %H:%M UTC')
                    times.append(t2.strftime('%d.%m'))
                    intervals.append((t2 - t1).total_seconds() / 3600)
                
                plt.figure(figsize=(8, 4), dpi=100)
                plt.plot(times, intervals, marker='o', color='#4CAF50', linewidth=2, markersize=8)
                plt.fill_between(times, intervals, color='#4CAF50', alpha=0.2)
                plt.title('Интервалы между спавнами Annihilation (часы)', fontsize=12, fontweight='bold')
                plt.ylabel('Часов', fontsize=10)
                plt.grid(True, linestyle='--', alpha=0.6)
                plt.xticks(rotation=45)
                
                buf = io.BytesIO()
                plt.savefig(buf, format='png', bbox_inches='tight')
                buf.seek(0)
                plt.close()
                
                await query.message.reply_photo(photo=buf, caption="📊 График интервалов между последними спавнами.")
                await query.edit_message_text("📊 " + ("График отправлен выше!" if lang=="ru" else "Graph sent above!"), reply_markup=get_main_kb(user), parse_mode='HTML')
            
        elif query.data == "back_to_menu":
            # Показываем приветственное сообщение как при /start
            text = get_text("start", lang, name=query.from_user.first_name)
            text += f"\n\n🔔 {get_text('enabled' if user.get('enabled') else 'disabled', lang)}"
            await query.edit_message_text(text, reply_markup=get_main_kb(user), parse_mode='HTML')
        
        elif query.data == "show_history":
            lang = user.get("lang", "ru")
            if not spawn_history:
                text = "📜 " + ("История спавнов пуста." if lang=="ru" else "Spawn history is empty.")
            else:
                text = "📜 <b>" + ("История последних спавнов:" if lang=="ru" else "Recent Spawns:") + "</b>\n"
                for h in reversed(spawn_history[-5:]): # Показываем последние 5 в обратном порядке
                    text += f"• {h['time']} ({h['status']})\n"
            
            keyboard = [[InlineKeyboardButton("🔙 " + ("Назад" if lang=="ru" else "Back"), callback_data="back_to_menu")]]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
            
    except Exception as e:
        logging.error(f"Ошибка handler: {e}")

async def check_notifications(application: Application):
    global last_notified_status, spawn_history
    
    while True:
        try:
            target_time, status = await get_annihilation_data()
            if not target_time:
                await asyncio.sleep(300)
                continue
            
            target_utc = target_time.astimezone(timezone.utc)
            now = datetime.now(timezone.utc)
            diff_minutes = int((target_utc - now).total_seconds() / 60)
            diff_seconds = int((target_utc - now).total_seconds())
            diff_minutes = diff_seconds // 60  # Для проверки порогов
            
            # 1. Уведомление о смене статуса
            if status == "accurate" and last_notified_status == "predicted":
                last_notified_status = "accurate"
                for uid_str, user in users.items():
                    if user.get("enabled"):
                        try:
                            if user.get("last_msg_id"):
                                await application.bot.delete_message(chat_id=int(uid_str), message_id=user["last_msg_id"])
                            
                            msg = f"🔄 <b>{'Статус изменился!' if user['lang']=='ru' else 'Status changed!'}</b>\n\n{'Теперь время точное (Accurate)!' if user['lang']=='ru' else 'Time is now Accurate!'}\n⏳ {format_time_left(diff_seconds)[0]}"
                            sent_msg = await application.bot.send_animation(chat_id=int(uid_str), animation=BOSS_GIF_URL, caption=msg, parse_mode='HTML', reply_markup=get_main_kb(user))
                            user["last_msg_id"] = sent_msg.message_id
                            save_data()
                        except Exception as e: logging.error(f"Ошибка смены статуса: {e}")

            # 2. Уведомления по порогам
            if status == "accurate":
                for uid_str, user in users.items():
                    if not user.get("enabled"): continue
                    uid = int(uid_str)
                    
                    for key, t_def in THRESHOLDS_DEF.items():
                        if not user.get("thresholds", {}).get(key, True): continue
                        
                        if (t_def["mins"] - 5) <= diff_minutes <= t_def["mins"]:
                            if key not in user.get("sent", []):
                                try:
                                    if user.get("last_msg_id"):
                                        await application.bot.delete_message(chat_id=uid, message_id=user["last_msg_id"])
                                    
                                    name = t_def["name_ru"] if user["lang"]=="ru" else t_def["name_en"]
                                    time_str, bar = format_time_left(diff_seconds)
                                    msg = f"✅ <b>Annihilation — {name}</b>\n\n⏳ {time_str}\n{bar}\n📅 {target_utc.strftime('%Y-%m-%d %H:%M UTC')}"
                                    
                                    sent_msg = await application.bot.send_animation(chat_id=uid, animation=BOSS_GIF_URL, caption=msg, parse_mode='HTML', reply_markup=get_main_kb(user))
                                    user["last_msg_id"] = sent_msg.message_id
                                    user.setdefault("sent", []).append(key)
                                    save_data()
                                except Exception as e: logging.error(f"Ошибка уведомления: {e}")
                    
                    # 3. История спавнов и АВТО-сохранение последнего спавна
                    if diff_seconds < 0 and "spawn_logged" not in user:
                        spawn_history.append({"time": target_utc.strftime('%Y-%m-%d %H:%M UTC'), "status": "Accurate"})
                        if len(spawn_history) > 10: 
                            spawn_history.pop(0)
                        
                        # FEATURE 9: Автоматически обновляем last_spawn_time
                        global last_spawn_time
                        last_spawn_time = target_utc
                        
                        user["spawn_logged"] = True
                        save_data()
                        logging.info(f"💾 Автоматически сохранен новый last_spawn_time: {last_spawn_time}")
            else:
                last_notified_status = "predicted"
                for uid_str, user in users.items():
                    if user.get("sent") or user.get("spawn_logged"):
                        user["sent"] = []
                        user.pop("spawn_logged", None)
                        save_data()
                        
        except Exception as e:
            logging.error(f"Ошибка в check_notifications: {e}")
        await asyncio.sleep(300)

async def set_spawn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_spawn_time
    if update.effective_user.id != ADMIN_ID and ADMIN_ID != 0:
        await update.message.reply_text("❌ Доступ запрещен.")
        return
    
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Использование: /setspawn YYYY-MM-DD HH:MM\n"
            "Пример: /setspawn 2026-09-22 06:34\n"
            "⚠️ Время должно быть в UTC!"
        )
        return
    
    try:
        date_str = context.args[0]
        time_str = context.args[1]
        year, month, day = map(int, date_str.split('-'))
        hour, minute = map(int, time_str.split(':'))
        
        last_spawn_time = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
        
        spawn_history.append({
            "time": last_spawn_time.strftime('%Y-%m-%d %H:%M UTC'),
            "status": "Manual"
        })
        if len(spawn_history) > 10:
            spawn_history.pop(0)
        save_data()
        
        await update.message.reply_text(
            f"✅ Время последнего спавна установлено:\n"
            f"📅 {last_spawn_time.strftime('%Y-%m-%d %H:%M UTC')}\n\n"
            f"Теперь бот будет считать Предикт от этого времени (+84 часа)."
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}\n\nФормат: /setspawn YYYY-MM-DD HH:MM")

async def about_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    avg_h = get_average_interval_hours()
    text = (
        "🤖 <b>Anni-Bot v2.0</b>\n\n"
        "‍💻 <b>Разработчик:</b> @TheVetka\n"
        "📊 <b>Отслеживает:</b> Prelude to Annihilation (Wynncraft)\n"
        "🌐 <b>Источник данных:</b> api.wynncraft.com (v3)\n\n"
        f"📈 <b>Средний интервал спавна:</b> {avg_h:.1f} ч.\n"
        f"👥 <b>Пользователей:</b> {len(users)}\n"
        f"🔔 <b>Активных подписок:</b> {sum(1 for u in users.values() if u.get('enabled'))}"
    )
    await update.message.reply_text(text, parse_mode='HTML')

async def main():
    logging.info("🚀 Запуск бота v2.0 (API Mode)...")
    load_data()
    
    threading.Thread(target=run_server, daemon=True).start()
    
    app_bot = Application.builder().token(TELEGRAM_TOKEN).build()
    app_bot.add_handler(CommandHandler("start", start))
    app_bot.add_handler(CommandHandler("ping", ping))
    app_bot.add_handler(CommandHandler("stats", stats))
    app_bot.add_handler(CommandHandler("history", history_cmd))
    app_bot.add_handler(CommandHandler("graph", graph_cmd))
    app_bot.add_handler(CommandHandler("about", about_cmd))
    app_bot.add_handler(CommandHandler("status", status))
    app_bot.add_handler(CommandHandler("setspawn", set_spawn))
    app_bot.add_handler(InlineQueryHandler(inline_query))
    app_bot.add_handler(CallbackQueryHandler(button_handler))
    
    task = asyncio.create_task(check_notifications(app_bot))
    
    await app_bot.initialize()
    await app_bot.start()
    await app_bot.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    logging.info("✅ Бот готов!")
    
    try:
        while True: await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        task.cancel()
        await app_bot.stop()
        await app_bot.shutdown()

async def graph_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(spawn_history) < 2:
        await update.message.reply_text("⚠️ Недостаточно данных для построения графика. Нужно минимум 2 спавна.")
        return
    
    # Собираем данные
    times = []
    intervals = []
    for i in range(1, len(spawn_history)):
        t1 = datetime.strptime(spawn_history[i-1]['time'], '%Y-%m-%d %H:%M UTC')
        t2 = datetime.strptime(spawn_history[i]['time'], '%Y-%m-%d %H:%M UTC')
        times.append(t2.strftime('%d.%m'))
        intervals.append((t2 - t1).total_seconds() / 3600)
    
    # Рисуем график
    plt.figure(figsize=(8, 4), dpi=100)
    plt.plot(times, intervals, marker='o', color='#4CAF50', linewidth=2, markersize=8)
    plt.fill_between(times, intervals, color='#4CAF50', alpha=0.2)
    plt.title('Интервалы между спавнами Annihilation (часы)', fontsize=12, fontweight='bold')
    plt.ylabel('Часов', fontsize=10)
    plt.grid(True, linestyle='--', alpha=0.6)
    plt.xticks(rotation=45)
    
    # Сохраняем в память
    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight')
    buf.seek(0)
    plt.close()
    
    await update.message.reply_photo(photo=buf, caption="📊 График интервалов между последними спавнами.")

async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query
    target_time, status = await get_annihilation_data()
    
    if target_time:
        target_utc = target_time.astimezone(timezone.utc)
        diff_seconds = int((target_utc - datetime.now(timezone.utc)).total_seconds())
        time_str, _ = format_time_left(diff_seconds)
        status_text = "✅ Точное время" if status == "accurate" else "📊 Предикт"
        
        result_text = f"{status_text}\n⏳ До спавна: {time_str}\n📅 {target_utc.strftime('%Y-%m-%d %H:%M UTC')}"
    else:
        result_text = "⚠️ Время спавна пока неизвестно."

    results = [
        InlineQueryResultArticle(
            id="1",
            title="Статус Annihilation",
            description=result_text,
            input_message_content=InputTextMessageContent(
                message_text=f"🎮 <b>Annihilation Status</b>\n{result_text}",
                parse_mode='HTML'
            )
        )
    ]
    await update.inline_query.answer(results, cache_time=60)

if __name__ == "__main__":
    asyncio.run(main())