import sqlite3
import logging
import requests
import cairosvg
import os
import re
import asyncio
# import httpx # Не нужен
from io import BytesIO
from dotenv import load_dotenv
# --- ДОБАВЛЕНО для расчета даты по сроку ---
from datetime import datetime, timedelta, time # Добавляем time
from dateutil.relativedelta import relativedelta # Используем для корректного добавления месяцев
# -----------------------------------------

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardRemove,
    constants
)
from telegram.error import TelegramError, TimedOut
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from database import (
    init_db,
    save_client,
    delete_client_from_db,
    update_client_status,
    extend_client,
    get_client_by_name,
    get_all_clients,
)

# === ЗАГРУЗКА НАСТРОЕК ===
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DB_DIR = os.getenv("DB_DIR", "db")
DEFAULT_SESSION_PASSWORD = os.getenv("SESSION_PASSWORD")

# === СЕРВЕРЫ ===
SERVERS = {
        os.getenv("SERVER1_KEY"): {"name": os.getenv("SERVER1_NAME"), "url": os.getenv("SERVER1_URL")},
        os.getenv("SERVER2_KEY"): {"name": os.getenv("SERVER2_NAME"), "url": os.getenv("SERVER2_URL")},
        os.getenv("SERVER3_KEY"): {"name": os.getenv("SERVER3_NAME"), "url": os.getenv("SERVER3_URL")}
}

# === ДОПУСКАЕМЫЕ ПОЛЬЗОВАТЕЛИ ===
allowed_users_str = os.getenv("ALLOWED_USERS", "")
try:
    ALLOWED_USERS = [int(user_id.strip()) for user_id in allowed_users_str.split(',') if user_id.strip()]
except ValueError:
    logging.error("Ошибка чтения ALLOWED_USERS.")
    ALLOWED_USERS = []

def is_authorized(user_id: int) -> bool:
    if not ALLOWED_USERS: logging.warning("ALLOWED_USERS пуст."); return False
    return user_id in ALLOWED_USERS

# === ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ для получения пути к БД ===
def get_db_path_for_user(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    server_key = context.user_data.get('server_key')
    if server_key and server_key in SERVERS:
        return os.path.join(DB_DIR, f"{server_key}.db")
    # logging.warning("Ключ сервера не найден в user_data.")
    return None

# === ФУНКЦИИ ДЛЯ РАБОТЫ С API ===
# (без изменений)
def create_session(base_url: str, password: str):
    try: response = requests.post(f"{base_url}/api/session", json={"password": password}, timeout=10); response.raise_for_status(); return response.cookies
    except requests.exceptions.RequestException as e: logging.error(f"Ошибка сессии {base_url}: {e}"); return None
def get_api_clients(cookies, base_url: str):
    if not cookies: return None
    try: response = requests.get(f"{base_url}/api/wireguard/client", cookies=cookies, timeout=10); response.raise_for_status(); return response.json()
    except requests.exceptions.RequestException as e: logging.error(f"Ошибка get_clients {base_url}: {e}"); return None
    except requests.exceptions.JSONDecodeError as e: logging.error(f"Ошибка JSON {base_url}: {e}"); return None
def get_api_client_configuration(client_id, cookies, base_url: str):
    if not cookies: return None
    try: response = requests.get(f"{base_url}/api/wireguard/client/{client_id}/configuration", cookies=cookies, timeout=10); response.raise_for_status(); return response.text
    except requests.exceptions.RequestException as e: logging.error(f"Ошибка конфига {client_id} с {base_url}: {e}"); return None
def get_api_qr_code_svg(client_id, cookies, base_url: str):
    if not cookies: return None
    try: response = requests.get(f"{base_url}/api/wireguard/client/{client_id}/qrcode.svg", cookies=cookies, timeout=10); response.raise_for_status(); return response.content
    except requests.exceptions.RequestException as e: logging.error(f"Ошибка QR SVG {client_id} с {base_url}: {e}"); return None
def get_api_config_and_qr(client_name: str, base_url: str, password: str):
    cookies = create_session(base_url, password);
    if not cookies: return None, None, "Не удалось создать сессию."
    api_clients = get_api_clients(cookies, base_url);
    if api_clients is None: return None, None, "Не удалось получить список клиентов."
    client_data = next((c for c in api_clients if c["name"] == client_name), None)
    if not client_data: return None, None, f"Клиент '{client_name}' не найден на сервере."
    client_id = client_data["id"]; config = get_api_client_configuration(client_id, cookies, base_url)
    qr_svg = get_api_qr_code_svg(client_id, cookies, base_url); qr_png, qr_error = None, None
    if qr_svg:
        try: qr_png = cairosvg.svg2png(bytestring=qr_svg)
        except Exception as e: logging.error(f"Ошибка SVG->PNG {client_name}: {e}"); qr_error = "Ошибка QR SVG->PNG."
    else: qr_error = "Ошибка получения QR SVG."
    error_message = None
    if config is None and qr_png is None: error_message = "Не удалось получить ни конфиг, ни QR."
    elif config is None: error_message = "Конфиг не получен, но QR есть."
    elif qr_png is None: error_message = f"Конфиг получен, но {qr_error}"
    return config, qr_png, error_message
def create_client_api(client_name: str, base_url: str, password: str):
    cookies = create_session(base_url, password);
    if not cookies: return None, None, "Не удалось создать сессию."
    try:
        response = requests.post(f"{base_url}/api/wireguard/client", json={"name": client_name}, cookies=cookies, timeout=15)
        if response.status_code == 409: return None, None, f"Клиент '{client_name}' уже есть на сервере."
        response.raise_for_status()
    except requests.exceptions.RequestException as e: logging.error(f"Ошибка API создания {client_name}: {e}"); return None, None, f"Ошибка API создания '{client_name}'."
    api_clients = get_api_clients(cookies, base_url);
    if api_clients is None: return None, None, "Клиент создан (API), но ошибка получения данных."
    client_data = next((c for c in api_clients if c["name"] == client_name), None)
    if not client_data: return None, None, "Клиент создан (API), но не найден в списке."
    client_id = client_data["id"]; config = get_api_client_configuration(client_id, cookies, base_url)
    qr_svg = get_api_qr_code_svg(client_id, cookies, base_url); qr_png = None
    if qr_svg:
        try: qr_png = cairosvg.svg2png(bytestring=qr_svg)
        except Exception as e: logging.error(f"Ошибка SVG->PNG созд. {client_name}: {e}")
    error = None
    if config is None and qr_png is None: error = "Клиент создан (API), но ошибка получения конфига/QR."
    return config, qr_png, error
def delete_client_api(client_name: str, base_url: str, password: str):
    cookies = create_session(base_url, password);
    if not cookies: return False, "Не удалось создать сессию."
    api_clients = get_api_clients(cookies, base_url);
    if api_clients is None: logging.warning(f"Нет списка клиентов {base_url} перед удалением {client_name}.")
    client_data = next((c for c in api_clients if c["name"] == client_name), None) if api_clients else None
    if client_data:
        client_id = client_data["id"]
        try:
            response = requests.delete(f"{base_url}/api/wireguard/client/{client_id}", cookies=cookies, timeout=10)
            response.raise_for_status(); logging.info(f"Клиент '{client_name}' удален с API {base_url}.")
            return True, None
        except requests.exceptions.RequestException as e:
            logging.error(f"Ошибка API удаления {client_name}: {e}")
            return False, f"Ошибка API при удалении '{client_name}'."
    else:
        logging.info(f"Клиент '{client_name}' не найден на API {base_url}.")
        return True, None
def toggle_client_status_api(client_name: str, enable: bool, base_url: str, password: str):
    cookies = create_session(base_url, password);
    if not cookies: return False, "Не удалось создать сессию."
    api_clients = get_api_clients(cookies, base_url);
    if api_clients is None: return False, "Не удалось получить список клиентов."
    client_data = next((c for c in api_clients if c["name"] == client_name), None)
    if not client_data: return False, f"Клиент '{client_name}' не найден на сервере."
    client_id = client_data["id"]; action = "enable" if enable else "disable"
    try:
        response = requests.post(f"{base_url}/api/wireguard/client/{client_id}/{action}", cookies=cookies, timeout=10)
        response.raise_for_status()
        return True, f"Статус клиента '{client_name}' изменен на API ✅"
    except requests.exceptions.RequestException as e:
        logging.error(f"Ошибка API {action} {client_name}: {e}")
        return False, f"Ошибка API при изменении статуса '{client_name}'."

# === КЛАВИАТУРЫ ===
def get_main_keyboard():
    keyboard = [
        [KeyboardButton("📄 Скачать конфиг"), KeyboardButton("🇶 Запросить QR")],
        [KeyboardButton("➕ Создать клиента"), KeyboardButton("🗑️ Удалить клиента")],
        [KeyboardButton("⏳ Продлить срок действия"), KeyboardButton("👥 Список клиентов")],
        [KeyboardButton("🌐 Выбрать другой сервер")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)
def get_back_keyboard():
    return ReplyKeyboardMarkup([[KeyboardButton("⬅️ Назад")]], resize_keyboard=True, one_time_keyboard=False)
def get_creation_options_keyboard():
    keyboard = [
        [KeyboardButton("1 мес"), KeyboardButton("6 мес"), KeyboardButton("12 мес")],
        [KeyboardButton("🗓️ Указать дату")],
        [KeyboardButton("⬅️ Назад")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)

# === ОБРАБОТЧИКИ ===

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_authorized(user.id): logging.warning(f"Неавторизованный доступ: {user.id} ({user.username})"); await update.message.reply_text("⛔️ Нет доступа."); return
    context.user_data.clear()
    buttons = [[InlineKeyboardButton(s["name"], callback_data=f"select_server:{k}")] for k, s in SERVERS.items()]
    if not buttons: await update.message.reply_text("Ошибка: Серверы не настроены."); return
    reply_markup = InlineKeyboardMarkup(buttons)
    await update.message.reply_text(f"Привет, {user.first_name}! Выберите сервер:", reply_markup=reply_markup)
    await update.message.reply_text("...", reply_markup=ReplyKeyboardRemove())

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id): await update.message.reply_text("⛔️ Нет доступа."); return

    text = update.message.text
    logging.info(f"User {user_id} кнопка: {text}")

    action_text = text.split(" ", 1)[-1] if text.startswith(("📄", "🇶", "➕", "🗑️", "⏳", "👥", "🌐")) else text

    if action_text == "Выбрать другой сервер": await start(update, context); return

    db_path = get_db_path_for_user(context)
    server_name = context.user_data.get('server_name', 'Неизвестный')
    base_url = context.user_data.get('base_url')

    if not db_path or not base_url: await update.message.reply_text("Сервер не выбран. /start"); return

    context.user_data.pop("action", None); context.user_data.pop("duration", None); context.user_data.pop("extend_duration", None); context.user_data.pop("custom_expiry_date", None)
    await update.message.reply_text(f"📍 Сервер: {server_name} (БД: {os.path.basename(db_path)})")

    if action_text == "Скачать конфиг": context.user_data["action"] = "get_config"; await update.message.reply_text("Имя клиента для конфига:", reply_markup=get_back_keyboard())
    elif action_text == "Запросить QR": context.user_data["action"] = "get_qr"; await update.message.reply_text("Имя клиента для QR:", reply_markup=get_back_keyboard())
    elif action_text == "Удалить клиента": context.user_data["action"] = "delete_client"; await update.message.reply_text("Имя клиента для удаления:", reply_markup=get_back_keyboard())
    elif action_text == "Создать клиента": context.user_data["action"] = "select_creation_method"; await update.message.reply_text("Выберите срок действия или укажите дату:", reply_markup=get_creation_options_keyboard())
    elif action_text == "Продлить срок действия":
        context.user_data["action"] = "extend_select_duration"
        extend_duration_keyboard = ReplyKeyboardMarkup([ [KeyboardButton("1"), KeyboardButton("6"), KeyboardButton("12")], [KeyboardButton("⬅️ Назад")] ], resize_keyboard=True, one_time_keyboard=True)
        await update.message.reply_text("Продлить на (мес.):", reply_markup=extend_duration_keyboard)

    elif action_text == "Список клиентов":
        password = context.user_data.get('password', DEFAULT_SESSION_PASSWORD)
        await update.message.reply_text("Загрузка списка...", reply_markup=get_main_keyboard())
        try: db_clients = get_all_clients(db_path)
        except Exception as e: logging.error(f"Ошибка БД {db_path}: {e}"); await update.message.reply_text(f"Ошибка БД {server_name}."); return
        if not db_clients: await update.message.reply_text(f"Клиенты не найдены в БД {server_name}.", reply_markup=get_main_keyboard()); return
        cookies = create_session(base_url, password); api_clients = get_api_clients(cookies, base_url)
        output_messages = []; api_statuses = {}; api_error_flag = False
        if api_clients is not None: api_statuses = {c['name']: c.get('enabled', True) for c in api_clients}
        else: await update.message.reply_text("⚠️ Ошибка API статусов."); api_error_flag = True
        clients_found_on_server = False
        for name, expiry_date, db_status in db_clients:
            is_on_server = name in api_statuses; enabled = False
            if is_on_server: clients_found_on_server = True; enabled = api_statuses.get(name, False); emoji = "🟢" if enabled else "🔴"; status_text = "<b>enabled</b>" if enabled else "<b>disabled</b>"
            elif not api_error_flag: emoji = "❓"; status_text = f"<pre>нет на API</pre>"
            else: emoji = "⚠️"; status_text = "<pre>API N/A</pre>"
            expiry_str = f"<code>{expiry_date[:10] if expiry_date else '-'}</code>"
            message = f"{emoji} <b>{name}</b>\n📌 Статус: {status_text}\n⏳ До: {expiry_str}"
            inline_keyboard = None
            if is_on_server and not api_error_flag: inline_keyboard = InlineKeyboardMarkup([ [InlineKeyboardButton("▶️ Вкл" if not enabled else "▶️", callback_data=f"enable:{name}"), InlineKeyboardButton("⏹️ Выкл" if enabled else "⏹️", callback_data=f"disable:{name}")] ])
            output_messages.append({'text': message, 'reply_markup': inline_keyboard})
        if not output_messages: await update.message.reply_text(f"Список пуст {server_name}.", reply_markup=get_main_keyboard()); return
        await update.message.reply_text(f"Вывод списка ({len(output_messages)} шт.)...", reply_markup=get_main_keyboard())
        MESSAGE_LENGTH_LIMIT = 4096; logging.debug(f"Лимит: {MESSAGE_LENGTH_LIMIT}")
        for i, msg_data in enumerate(output_messages):
             try:
                 if len(msg_data['text']) > MESSAGE_LENGTH_LIMIT: logging.warning(f"Сообщ. {i} > лимита."); client_name_from_msg = re.search(r"<b>(.*?)</b>", msg_data['text']); name_to_log = client_name_from_msg.group(1) if client_name_from_msg else f"idx {i}"; await update.message.reply_text(f"⚠️ >лимит для {name_to_log}.")
                 else: await update.message.reply_text(text=msg_data['text'], parse_mode=constants.ParseMode.HTML, reply_markup=msg_data['reply_markup'])
                 await asyncio.sleep(0.2)
             except TelegramError as e: logging.error(f"Ошибка TG отпр. {i}: {e}"); await update.message.reply_text(f"Ошибка отпр. {i}: {e}"); await asyncio.sleep(0.5)
             except Exception as e: logging.error(f"Неизв. ошибка {i}: {e!r}"); await update.message.reply_text(f"Неизв. ошибка вывода."); await asyncio.sleep(1)
        await update.message.reply_text("--- Конец списка ---", reply_markup=get_main_keyboard())
        if not clients_found_on_server and db_clients and not api_error_flag: await update.message.reply_text(f"⚠️ Ни один клиент из БД не найден на API {server_name}.", reply_markup=get_main_keyboard())


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id): return

    text = update.message.text.strip();
    if not text: return

    if text == "⬅️ Назад":
        current_action = context.user_data.get('action'); logging.info(f"User {user_id} Назад. Отмена: {current_action}")
        context.user_data.pop("action", None); context.user_data.pop("duration", None); context.user_data.pop("extend_duration", None); context.user_data.pop("custom_expiry_date", None)
        await update.message.reply_text("Отменено.", reply_markup=get_main_keyboard()); return

    db_path = get_db_path_for_user(context)
    base_url = context.user_data.get('base_url')
    server_name = context.user_data.get('server_name', 'Неизвестный')
    password = context.user_data.get('password', DEFAULT_SESSION_PASSWORD)
    action = context.user_data.get("action")

    if not db_path or not base_url: await update.message.reply_text("Сервер не выбран. /start"); return
    if not action: logging.debug(f"Нет action для '{text}' от {user_id}"); return

    logging.info(f"User {user_id} Action: {action}, Input: '{text}', Server: {server_name}, DB: {os.path.basename(db_path)}")
    default_reply_markup = get_main_keyboard()

    try:
        if action == "select_creation_method":
            if text in ["1 мес", "6 мес", "12 мес"]:
                try: duration = int(text.split(" ")[0]); context.user_data["duration"] = duration; context.user_data["action"] = "create_client_duration"; await update.message.reply_text("Введите имя нового клиента:", reply_markup=get_back_keyboard())
                except ValueError: await update.message.reply_text("Ошибка. Выберите срок кнопкой.", reply_markup=get_creation_options_keyboard())
            elif text == "🗓️ Указать дату": context.user_data["action"] = "enter_custom_date"; await update.message.reply_text("Введите дату окончания ДД.ММ.ГГГГ:", reply_markup=get_back_keyboard())
            else: await update.message.reply_text("Выберите опцию с клавиатуры.", reply_markup=get_creation_options_keyboard())
            return

        elif action == "enter_custom_date":
            try:
                input_date = datetime.strptime(text, "%d.%m.%Y")
                if input_date.date() < datetime.now().date(): await update.message.reply_text("Дата не м.б. в прошлом. Введите ДД.ММ.ГГГГ:", reply_markup=get_back_keyboard()); return
                expiry_datetime = input_date.replace(hour=23, minute=59, second=59, microsecond=999999)
                expiry_date_str = expiry_datetime.isoformat(timespec='microseconds')
                context.user_data["custom_expiry_date"] = expiry_date_str; context.user_data["action"] = "create_client_custom_date"; await update.message.reply_text(f"Дата {text} принята. Имя нового клиента:", reply_markup=get_back_keyboard())
            except ValueError: await update.message.reply_text("Неверный формат. Введите ДД.ММ.ГГГГ:", reply_markup=get_back_keyboard())
            return

        elif action == "extend_select_duration" and text in ["1", "6", "12"]: context.user_data["extend_duration"] = int(text); context.user_data["action"] = "extend_client"; await update.message.reply_text("Имя клиента для продления:", reply_markup=get_back_keyboard())

        elif action in ["create_client_duration", "create_client_custom_date", "extend_client", "get_config", "get_qr", "delete_client"]:
            client_name = text
            final_expiry_date_str = None

            if action == "create_client_duration" or action == "create_client_custom_date":
                await update.message.reply_text(f"Создание '{client_name}'...", reply_markup=default_reply_markup)
                if action == "create_client_duration":
                    duration = context.user_data.get("duration")
                    if duration is None: raise ValueError("Срок (duration) не найден.")
                    try: expiry_dt = datetime.now() + relativedelta(months=duration); final_expiry_date_str = expiry_dt.replace(hour=23, minute=59, second=59, microsecond=999999).isoformat(timespec='microseconds'); logging.info(f"Рассчитана дата до {final_expiry_date_str}")
                    except Exception as e: logging.error(f"Ошибка расчета даты: {e}"); await update.message.reply_text("Ошибка расчета даты."); context.user_data.pop("action", None); context.user_data.pop("duration", None); return
                else:
                    final_expiry_date_str = context.user_data.get("custom_expiry_date")
                    if final_expiry_date_str is None: raise ValueError("Кастомная дата не найдена.")
                    logging.info(f"Используется кастомная дата: {final_expiry_date_str}")

                config, qr_png, error_api = create_client_api(client_name, base_url, password)
                if error_api: await update.message.reply_text(f"Ошибка API: {error_api}")
                if "уже существует" not in (error_api or ""):
                    try:
                        saved_to_db = save_client(db_path, client_name, final_expiry_date_str)
                        if saved_to_db:
                             if not error_api: await update.message.reply_text(f"Клиент '{client_name}' создан ✅ (до {final_expiry_date_str[:10]})", reply_markup=default_reply_markup)
                             else: await update.message.reply_text(f"Клиент '{client_name}' сохранен в БД (до {final_expiry_date_str[:10]}), но была проблема с API.", reply_markup=default_reply_markup)
                             if config: await update.message.reply_document(InputFile(BytesIO(config.encode('utf-8')), filename=f"{client_name}.conf"), caption=f"Конфиг {client_name}")
                             else: await update.message.reply_text("⚠️ Конфиг с API не получен.")
                             if qr_png: await update.message.reply_photo(BytesIO(qr_png), caption=f"QR-код {client_name}")
                             else: await update.message.reply_text("⚠️ QR-код с API не получен.")
                        else: await update.message.reply_text(f"Не удалось сохранить '{client_name}' в БД.", reply_markup=default_reply_markup)
                    except Exception as db_err: logging.error(f"Ошибка БД сохр. {client_name} в {db_path}: {db_err}"); await update.message.reply_text(f"Ошибка сохранения '{client_name}' в БД!", reply_markup=default_reply_markup)
                context.user_data.pop("duration", None); context.user_data.pop("custom_expiry_date", None)

            elif action == "extend_client":
                duration = context.user_data.get("extend_duration")
                if duration is None: raise ValueError("Срок продления не выбран.")
                try:
                    extended = extend_client(db_path, client_name, duration)
                    if extended: updated_client_info = get_client_by_name(db_path, client_name); new_expiry_date = updated_client_info[1] if updated_client_info and len(updated_client_info) > 1 and updated_client_info[1] else "не уст."; await update.message.reply_text(f"Срок '{client_name}' в БД продлён на {duration} мес. ✅\nДо: <code>{new_expiry_date}</code>", reply_markup=default_reply_markup, parse_mode=constants.ParseMode.HTML)
                    else: await update.message.reply_text(f"Клиент '{client_name}' не найден/не продлен в БД.", reply_markup=default_reply_markup)
                except Exception as db_err: logging.error(f"Ошибка БД продл. {client_name} в {db_path}: {db_err}"); await update.message.reply_text(f"Ошибка БД продл. '{client_name}'.")
                context.user_data.pop("extend_duration", None)

            elif action == "get_config":
                await update.message.reply_text(f"Запрос конфига '{client_name}'...", reply_markup=default_reply_markup)
                config, _, error = get_api_config_and_qr(client_name, base_url, password);
                if error and not config: await update.message.reply_text(f"Ошибка: {error}")
                elif config: await update.message.reply_document(InputFile(BytesIO(config.encode('utf-8')), filename=f"{client_name}.conf"), caption=f"Конфиг {client_name}\n\n{error or ''}")
                else: await update.message.reply_text(f"Неизв. ошибка конфига.")
                await update.message.reply_text("Выберите действие:", reply_markup=default_reply_markup)

            elif action == "get_qr":
                await update.message.reply_text(f"Запрос QR '{client_name}'...", reply_markup=default_reply_markup)
                _, qr_png, error = get_api_config_and_qr(client_name, base_url, password);
                if error and not qr_png: await update.message.reply_text(f"Ошибка: {error}")
                elif qr_png: await update.message.reply_photo(BytesIO(qr_png), caption=f"QR-код {client_name}\n\n{error or ''}")
                else: await update.message.reply_text(f"Неизв. ошибка QR.")
                await update.message.reply_text("Выберите действие:", reply_markup=default_reply_markup)

            elif action == "delete_client":
                await update.message.reply_text(f"Удаление '{client_name}'...", reply_markup=default_reply_markup)
                api_success, api_msg = delete_client_api(client_name, base_url, password)
                if not api_success: await update.message.reply_text(f"Ошибка API: {api_msg}. Удаление из БД отменено.")
                else:
                    try: deleted_from_db = delete_client_from_db(db_path, client_name); final_message = f"Клиент '{client_name}' удален с API ({'успешно' if api_msg is None else 'не найден'}) и из БД ({'успешно' if deleted_from_db else 'не найден'}). ✅"; await update.message.reply_text(final_message, reply_markup=default_reply_markup)
                    except Exception as db_err: logging.error(f"Ошибка БД удал. {client_name} из {db_path}: {db_err}"); await update.message.reply_text(f"Клиент '{client_name}' удален с API, но ОШИБКА удаления из БД!", reply_markup=default_reply_markup)

            context.user_data.pop("action", None)

        else:
             if action == "select_creation_method": await update.message.reply_text("Пожалуйста, выберите опцию с клавиатуры.", reply_markup=get_creation_options_keyboard())
             elif action == "enter_custom_date": await update.message.reply_text("Неверный формат. Введите дату как ДД.ММ.ГГГГ:", reply_markup=get_back_keyboard())
             elif action == "extend_select_duration": await update.message.reply_text("Некорр. ввод. Выберите срок или '⬅️ Назад'.", reply_markup=ReplyKeyboardMarkup([[KeyboardButton("1"), KeyboardButton("6"), KeyboardButton("12")], [KeyboardButton("⬅️ Назад")]], resize_keyboard=True, one_time_keyboard=True))
             else: logging.warning(f"Необработанный action '{action}' для '{text}'"); await update.message.reply_text("Неизв. действие.", reply_markup=default_reply_markup); context.user_data.pop("action", None)

    except Exception as e:
        logging.exception(f"Ошибка в handle_message: {e}")
        await update.message.reply_text("Внутр. ошибка.", reply_markup=default_reply_markup)
        context.user_data.pop("action", None); context.user_data.pop("duration", None); context.user_data.pop("extend_duration", None); context.user_data.pop("custom_expiry_date", None)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; user_id = query.from_user.id
    await query.answer()

    if not is_authorized(user_id):
        try: await query.edit_message_text("⛔️ Нет доступа.")
        except Exception: pass
        return

    if not query.data: return
    logging.info(f"User {user_id} inline: {query.data}")

    if query.data.startswith("select_server:"):
        server_key = query.data.split(":", 1)[1]
        if server_key in SERVERS:
            selected_server = SERVERS[server_key]; db_path = os.path.join(DB_DIR, f"{server_key}.db")
            try:
                init_db(db_path)
                logging.info(f"БД для {server_key} готова.")
            except Exception as e:
                 # --- ИСПРАВЛЕНО ЗДЕСЬ ---
                 logging.error(f"Не удалось инициализ. БД {db_path} при выборе сервера: {e}")
                 try:
                     await query.edit_message_text("⚠️ Ошибка инициализации БД сервера!", reply_markup=None)
                 except Exception:
                     pass
                 return
                 # --- КОНЕЦ ИСПРАВЛЕНИЯ ---
            context.user_data['server_key'] = server_key; context.user_data['base_url'] = selected_server['url']; context.user_data['server_name'] = selected_server['name']; context.user_data['password'] = DEFAULT_SESSION_PASSWORD
            try:
                await query.edit_message_text(f"Выбран: {selected_server['name']}", reply_markup=None)
            except Exception:
                pass
            await context.bot.send_message(chat_id=query.message.chat_id, text="Действие:", reply_markup=get_main_keyboard())
        else:
             try:
                 await query.edit_message_text("Ошибка: Неизв. сервер.", reply_markup=None)
             except Exception:
                 pass
        return

    db_path = get_db_path_for_user(context)
    base_url = context.user_data.get('base_url')
    server_name = context.user_data.get('server_name', 'N/A')
    password = context.user_data.get('password', DEFAULT_SESSION_PASSWORD)

    # --- ИСПРАВЛЕНО ЗДЕСЬ ---
    if not db_path or not base_url:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="Сервер не выбран. Пожалуйста, используйте /start.",
            reply_markup=get_main_keyboard()
        )
        try:
            if query.message:
                await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    # --- КОНЕЦ ИСПРАВЛЕНИЯ ---

    if query.data.startswith("enable:") or query.data.startswith("disable:"):
        try: action_cb, client_name = query.data.split(":", 1)
        except ValueError: logging.error(f"Некорр. callback вкл/выкл: {query.data}"); return

        enable = (action_cb == "enable")
        api_success, api_msg = toggle_client_status_api(client_name, enable, base_url, password)
        db_update_success = False
        if api_success:
            try:
                db_status = "enabled" if enable else "disabled"
                updated_in_db = update_client_status(db_path, client_name, db_status)
                if updated_in_db: db_update_success = True
                else: logging.warning(f"'{client_name}' не найден в {os.path.basename(db_path)} для update.")
            except Exception as db_err: logging.error(f"Ошибка БД update {client_name} в {os.path.basename(db_path)}: {db_err}")

        result_message = api_msg if api_msg else ("Успешно" if api_success else "Ошибка")
        if api_success and not db_update_success: result_message += "\n⚠️ БД не обновлена!"
        elif not api_success: result_message += "\n БД не изменена."

        current_status_text, emoji, is_enabled_now = "<pre>API N/A</pre>", "⚠️", None
        cookies = create_session(base_url, password)
        api_clients = get_api_clients(cookies, base_url) if cookies else None
        api_client = next((c for c in api_clients if c["name"] == client_name), None) if api_clients is not None else None

        if api_client: is_enabled_now = api_client.get('enabled', False); emoji = "🟢" if is_enabled_now else "🔴"; current_status_text = "<b>enabled</b>" if is_enabled_now else "<b>disabled</b>"
        elif api_clients is not None: emoji = "❓"; current_status_text = f"<pre>нет на API</pre>"

        try: client_db_info = get_client_by_name(db_path, client_name); expiry_str = f"<code>{client_db_info[1][:10] if client_db_info and len(client_db_info)>1 and client_db_info[1] else '-'}</code>"
        except Exception: expiry_str = "<i>ошибка БД</i>"

        new_message_text = f"{emoji} <b>{client_name}</b>\n📌 Статус: {current_status_text}\n⏳ До: {expiry_str}\n\n<i>{result_message}</i>"
        new_keyboard = None
        if api_client: new_keyboard = InlineKeyboardMarkup([ [InlineKeyboardButton("▶️ Вкл" if not is_enabled_now else "▶️", callback_data=f"enable:{client_name}"), InlineKeyboardButton("⏹️ Выкл" if is_enabled_now else "⏹️", callback_data=f"disable:{client_name}")] ])

        try:
            if query.message and (query.message.text != new_message_text or query.message.reply_markup != new_keyboard):
                 logging.debug(f"Попытка редактирования сообщения для {client_name}")
                 await query.edit_message_text(text=new_message_text, parse_mode=constants.ParseMode.HTML, reply_markup=new_keyboard)
                 logging.debug(f"Сообщение для {client_name} отредактировано.")
            elif query.message: logging.info(f"Сообщение для {client_name} не изменилось, редактирование пропущено.")
            else: logging.warning("Не удалось получить query.message для редактирования.")
        except TelegramError as e:
             logging.warning(f"Не удалось отредактировать сообщение для {client_name}. Ошибка Telegram: {e!r}")
             # НЕ отправляем новое сообщение
        except Exception as e:
             logging.error(f"Неизвестная ошибка при попытке редактирования сообщения {client_name}: {e!r}")


# === ГЛОБАЛЬНЫЙ ОБРАБОТЧИК ОШИБОК ===
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.error(f"Исключение при обработке апдейта {update}:", exc_info=context.error)
    if isinstance(context.error, TimedOut):
        logging.warning("Таймаут Telegram API.")
        if isinstance(update, Update) and update.effective_chat:
             try:
                 if update.effective_message and update.effective_message.text: await context.bot.send_message(chat_id=update.effective_chat.id, text="⏳ Сервер Telegram не ответил. Попробуйте еще раз.")
             except Exception as e_inner: logging.error(f"Ошибка отпр. сообщ. о таймауте: {e_inner}")
        return
    if isinstance(context.error, TelegramError): logging.warning(f"Ошибка Telegram: {context.error}")
    if isinstance(update, Update) and update.effective_chat:
        try: await context.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ Внутренняя ошибка бота.")
        except Exception as e: logging.error(f"Ошибка отпр. сообщ. об ошибке: {e}")

# === ЗАПУСК ===
def main():
    if not TELEGRAM_TOKEN: print("CRITICAL: Нет TELEGRAM_TOKEN"); logging.critical("Нет TOKEN"); return
    if not ALLOWED_USERS: print("CRITICAL: Нет ALLOWED_USERS"); logging.critical("Нет ALLOWED_USERS"); return
    if not SERVERS: print("CRITICAL: SERVERS пуст"); logging.critical("SERVERS пуст"); return

    try:
        if not os.path.exists(DB_DIR): os.makedirs(DB_DIR); print(f"Создана директория БД: {DB_DIR}"); logging.info(f"Создана директория БД: {DB_DIR}")
        for server_key in SERVERS.keys(): db_path_init = os.path.join(DB_DIR, f"{server_key}.db"); init_db(db_path_init)
        logging.info("Все базы данных успешно инициализированы.")
    except Exception as e: print(f"CRITICAL: Ошибка инициализации БД: {e}"); logging.critical(f"Ошибка инициализации БД: {e}"); return

    logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram.vendor.ptb_urllib3.urllib3").setLevel(logging.WARNING)

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .connect_timeout(15.0)
        .read_timeout(30.0)
        .write_timeout(10.0)
        # .pool_timeout(30.0) # Можно раскомментировать
        .build()
    )

    main_menu_options = [ "📄 Скачать конфиг", "🇶 Запросить QR", "➕ Создать клиента", "🗑️ Удалить клиента", "⏳ Продлить срок действия", "👥 Список клиентов", "🌐 Выбрать другой сервер" ]
    app.add_handler(MessageHandler(filters.Regex(f"^({'|'.join(map(re.escape, main_menu_options))})$") & filters.ChatType.PRIVATE, handle_buttons))

    app.add_handler(CommandHandler("start", start, filters=filters.ChatType.PRIVATE))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_message))
    app.add_error_handler(error_handler)

    logging.info("Бот запускается...")
    print("Бот запускается...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    try: from dateutil.relativedelta import relativedelta; from datetime import datetime, timedelta, time
    except ImportError: print("ОШИБКА: Не установлена python-dateutil. Выполните: pip install python-dateutil"); exit(1)
    main()
