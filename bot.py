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

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

# === НАСТРОЙКИ ===
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))
STATE_FILE = "users.json"
HISTORY_FILE = "history.json"
BOSS_GIF_URL = "https://raw.githubusercontent.com/TheVetka/anni-bot/main/annihilation.gif" # Замени на свою ссылку!

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
last_spawn_time = None
CACHE_DURATION = 300 # Обновляем API каждые 5 минут (оно и так быстрое)

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

def format_time_left(minutes):
    if minutes < 0: return "Время вышло!", ""
    total_seconds = int(minutes * 60)
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
    
    max_mins = 4320 # 3 дня
    if minutes >= max_mins: progress = 0
    elif minutes <= 0: progress = 100
    else: progress = int(((max_mins - minutes) / max_mins) * 100)
    
    filled = int(progress / 5)
    bar = "█" * filled + "░" * (20 - filled) + f" {progress}%"
    return time_str, bar

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
                # Если есть расписание - это "Точное время"
                target_time = datetime.fromisoformat(schedule.replace('Z', '+00:00'))
                cached_spawn_time = target_time
                last_status = "accurate"
                logging.info(f"✅ API: Точное время {target_time}")
            else:
                # Если schedule == null, рассчитываем ПРЕДИКТ сами
                last_status = "predicted"
                if last_spawn_time:
                    # Прибавляем средние 3.5 дня (84 часа) к последнему спавну
                    cached_spawn_time = last_spawn_time + timedelta(hours=84)
                    logging.info(f"📊 API: Предикт (расчетное время: {cached_spawn_time})")
                else:
                    cached_spawn_time = None
                    logging.info("📊 API: Предикт (время неизвестно, нет данных о прошлом спавне)")
            
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
    
    history_text = "📜 " + ("История спавнов" if lang=="ru" else "Spawn History")
    
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle_text, callback_data=toggle_action)],
        [InlineKeyboardButton("⚙️ " + ("Настройки" if lang=="ru" else "Settings"), callback_data="settings")],
        [InlineKeyboardButton("⏱ " + ("Проверить таймер" if lang=="ru" else "Check Timer"), callback_data="check_timer")],
        [InlineKeyboardButton(history_text, callback_data="show_history")], # <-- НОВАЯ КНОПКА
        [InlineKeyboardButton("🌐 Wynncraft Wiki", url="https://wynncraft.wiki.gg/wiki/Prelude_to_Annihilation")]
    ])
    
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle_text, callback_data=toggle_action)],
        [InlineKeyboardButton("⚙️ " + ("Настройки" if lang=="ru" else "Settings"), callback_data="settings")],
        [InlineKeyboardButton("⏱ " + ("Проверить таймер" if lang=="ru" else "Check Timer"), callback_data="check_timer")],
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
        diff_minutes = int((target_utc - datetime.now(timezone.utc)).total_seconds() / 60)
        time_str, bar = format_time_left(diff_minutes)
        
        if diff_minutes > 0:
            text = f"{status_emoji} <b>{get_text('status', lang, status=status_text)}</b>\n\n" + get_text("time_left", lang, time=time_str, bar=bar, date=target_utc.strftime('%Y-%m-%d %H:%M UTC'))
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
            
        elif query.data == "back_to_menu":
            await query.edit_message_text("📋 " + ("Главное меню" if lang=="ru" else "Main Menu"), reply_markup=get_main_kb(user), parse_mode='HTML')

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
            
            # 1. Уведомление о смене статуса
            if status == "accurate" and last_notified_status == "predicted":
                last_notified_status = "accurate"
                for uid_str, user in users.items():
                    if user.get("enabled"):
                        try:
                            if user.get("last_msg_id"):
                                await application.bot.delete_message(chat_id=int(uid_str), message_id=user["last_msg_id"])
                            
                            msg = f"🔄 <b>{'Статус изменился!' if user['lang']=='ru' else 'Status changed!'}</b>\n\n{'Теперь время точное (Accurate)!' if user['lang']=='ru' else 'Time is now Accurate!'}\n⏳ {format_time_left(diff_minutes)[0]}"
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
                                    time_str, bar = format_time_left(diff_minutes)
                                    msg = f"✅ <b>Annihilation — {name}</b>\n\n⏳ {time_str}\n{bar}\n📅 {target_utc.strftime('%Y-%m-%d %H:%M UTC')}"
                                    
                                    sent_msg = await application.bot.send_animation(chat_id=uid, animation=BOSS_GIF_URL, caption=msg, parse_mode='HTML', reply_markup=get_main_kb(user))
                                    user["last_msg_id"] = sent_msg.message_id
                                    user.setdefault("sent", []).append(key)
                                    save_data()
                                except Exception as e: logging.error(f"Ошибка уведомления: {e}")
                    
                    # 3. История спавнов
                    if diff_minutes < 0 and "spawn_logged" not in user:
                        spawn_history.append({"time": target_utc.strftime('%Y-%m-%d %H:%M UTC'), "status": "Accurate"})
                        if len(spawn_history) > 10: spawn_history.pop(0)
                        global last_spawn_time
                        last_spawn_time = target_utc
                        user["spawn_logged"] = True
                        save_data()
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

async def main():
    logging.info("🚀 Запуск бота v2.0 (API Mode)...")
    load_data()
    
    threading.Thread(target=run_server, daemon=True).start()
    
    app_bot = Application.builder().token(TELEGRAM_TOKEN).build()
    app_bot.add_handler(CommandHandler("start", start))
    app_bot.add_handler(CommandHandler("ping", ping))
    app_bot.add_handler(CommandHandler("stats", stats))
    app_bot.add_handler(CommandHandler("history", history_cmd))
    app_bot.add_handler(CommandHandler("status", status))
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

if __name__ == "__main__":
    asyncio.run(main())
