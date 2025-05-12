import logging
import os
import json
import asyncio
import aiohttp
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, CommandStart, CommandObject, StateFilter
from aiogram.types import (
    Message, 
    CallbackQuery, 
    FSInputFile,
    ReplyKeyboardMarkup, 
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardRemove
)
from aiogram.filters import StateFilter
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

from vpn_users_utils import load_vpn_users, save_vpn_users

# --- Constants ---
TELEGRAM_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"
OPENROUTER_API_URL = "OPENROUTER_API_URL"
OPENROUTER_API_KEY = "OPENROUTER_API_KEY"
ADMIN_IDS = [403786501]
USERS_DB_FILE = "users.json"
HISTORY_LIMIT = 10

# --- Global Variables ---
user_histories = {}
pending_vpn_requests = {}

# --- Bot Settings ---
BOT_SETTINGS = {
    "temperature": 0.3,
    "max_tokens": 2048,
    "stream": True,
}

# --- State Classes ---
class BroadcastState(StatesGroup):
    waiting_for_message = State()

class BuyVPNState(StatesGroup):
    select_period = State()
    wait_payment = State()
    admin_grant = State()

# --- Клавиатура пользователя (ReplyKeyboardMarkup) ---
def get_user_keyboard(user_id=None):
    keyboard = [
        [KeyboardButton(text="💬 Написать GPT")],
        [KeyboardButton(text="Купить VPN")],
        [KeyboardButton(text="🧹 Очистить историю")],
        [KeyboardButton(text="ℹ️ Помощь")]
    ]
    if user_id is not None and is_admin(user_id):
        keyboard.append([KeyboardButton(text="Админ-меню")])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)

# Инициализация бота
storage = MemoryStorage()
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher(storage=storage)

# Загрузка данных о пользователях
async def load_users():
    """
    Load user data from JSON file. Creates default structure if file doesn't exist.
    
    Returns:
        dict: User data containing 'users' and 'blocked' lists
    """
    try:
        if not os.path.exists(USERS_DB_FILE):
            with open(USERS_DB_FILE, "w", encoding='utf-8') as f:
                json.dump({"users": {}, "blocked": []}, f)
        with open(USERS_DB_FILE, "r", encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"Error loading users: {e}")
        return {"users": {}, "blocked": []}

# Сохранение данных о пользователях
def save_users(data):
    """
    Save user data to JSON file.
    
    Args:
        data (dict): User data to save
    """
    try:
        with open(USERS_DB_FILE, "w", encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"Error saving users: {e}")

# Проверка, является ли пользователь администратором
def is_admin(user_id: int) -> bool:
    """
    Check if a user is an administrator.
    
    Args:
        user_id (int): Telegram user ID
        
    Returns:
        bool: True if user is admin, False otherwise
    """
    return user_id in ADMIN_IDS

# Проверка, заблокирован ли пользователь
def is_blocked(user_id: int, users_data: dict) -> bool:
    """
    Check if a user is blocked.
    
    Args:
        user_id (int): Telegram user ID
        users_data (dict): User database
        
    Returns:
        bool: True if user is blocked, False otherwise
    """
    return user_id in users_data.get("blocked", [])

# Функция для создания клавиатуры администратора
def get_admin_keyboard(cancel_button=False):
    keyboard = []

    # Основные кнопки администратора
    keyboard.append([
        types.InlineKeyboardButton(text="👥Пользователи", callback_data="admin_view_users"),
        types.InlineKeyboardButton(text="✅GPT", callback_data="admin_open_gpt_access"),
        types.InlineKeyboardButton(text="🚫GPT", callback_data="admin_close_gpt_access"),
    ])
    keyboard.append([
        types.InlineKeyboardButton(text="✉️Рассылка", callback_data="admin_broadcast"),
    ])

    # Добавляем кнопку "Отмена", если она нужна
    if cancel_button:
        keyboard.append([
            types.InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_broadcast")
        ])

    return types.InlineKeyboardMarkup(inline_keyboard=keyboard)

# Обработка кнопки меню блокировки/разблокировки
@dp.callback_query(lambda c: c.data == "admin_lock_menu")
async def show_lock_menu(callback: types.CallbackQuery):
    users_data = await load_users()
    buttons = []
    for uid, user in users_data["users"].items():
        username = user.get("username", "Без имени")
        if int(uid) in users_data.get("blocked", []):
            btn = types.InlineKeyboardButton(
                text=f"{username} ({uid}) ✅",
                callback_data=f"unblock_user_{uid}"
            )
        else:
            btn = types.InlineKeyboardButton(
                text=f"{username} ({uid}) ❌",
                callback_data=f"block_user_{uid}"
            )
        buttons.append([btn])
    if not buttons:
        buttons = [[types.InlineKeyboardButton(text="Нет пользователей", callback_data="admin_menu")]]
    buttons.append([types.InlineKeyboardButton(text="🔙 Назад", callback_data="admin_menu")])
    await callback.message.edit_text(
        "Список пользователей для блокировки/разблокировки:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons)
    )

# Обработка команды /admin
@dp.message(lambda msg: msg.text is not None and msg.text.startswith("Админ-меню"))
async def admin_command(message: Message):
    user_id = message.from_user.id

    # Проверяем, является ли пользователь администратором
    if not is_admin(user_id):
        await message.answer("У вас нет прав для выполнения этой команды.")
        return

    # Отправляем меню с кнопками
    await message.answer("Выберите действие:", reply_markup=get_admin_keyboard())

# Обработка callback-кнопки запроса доступа к GPT
@dp.callback_query(lambda c: c.data == "request_gpt_access")
async def process_gpt_access_request(callback: CallbackQuery):
    user_id = callback.from_user.id
    username = callback.from_user.username or "Unknown"
    users_data = await load_users()
    # Добавляем пользователя в базу, если его нет
    if str(user_id) not in users_data["users"]:
        users_data["users"][str(user_id)] = {"username": username, "gpt_access": False}
        save_users(users_data)

    # Оповещаем всех админов о запросе
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"Пользователь @{username} (ID: {user_id}) запросил доступ к GPT.",
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text="✅",
                                callback_data=f"admin_approve_gpt_{user_id}"
                            ),
                            types.InlineKeyboardButton(
                                text="❌",
                                callback_data=f"admin_decline_gpt_{user_id}"
                            )
                        ]
                    ]
                )
            )
        except Exception as e:
            logging.error(f"Ошибка при отправке уведомления админу {admin_id}: {e}")

    await callback.message.edit_text(
        "Запрос на доступ отправлен. Ожидайте решения."
    )
    await callback.answer()

# === ДОБАВЛЕНО: обработчик одобрения запроса на доступ ===
@dp.callback_query(lambda c: c.data and c.data.startswith("admin_approve_gpt_"))
async def admin_approve_gpt(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет прав.", show_alert=True)
        return
    user_id = callback.data.split("admin_approve_gpt_")[1]
    users_data = await load_users()
    if user_id in users_data["users"]:
        users_data["users"][user_id]["gpt_access"] = True
        save_users(users_data)
        try:
            await bot.send_message(int(user_id), "✅ Вам открыт доступ к GPT", reply_markup=get_user_keyboard())
        except Exception as e:
            logging.error(f"Ошибка при уведомлении пользователя {user_id}: {e}")
        await callback.message.edit_text("Доступ пользователю открыт.")
    else:
        await callback.answer("Пользователь не найден.", show_alert=True)

# === ДОБАВЛЕНО: обработчик отклонения запроса на доступ ===
@dp.callback_query(lambda c: c.data and c.data.startswith("admin_decline_gpt_"))
async def admin_decline_gpt(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет прав.", show_alert=True)
        return
    user_id = callback.data.split("admin_decline_gpt_")[1]
    try:
        await bot.send_message(int(user_id), "🚫 Ваш запрос на доступ к GPT был отклонён.", reply_markup=get_user_keyboard())
    except Exception as e:
        logging.error(f"Ошибка при уведомлении пользователя {user_id}: {e}")
    await callback.message.edit_text("Запрос отклонён.")

# Обработка открытия доступа через список
@dp.callback_query(lambda c: c.data and c.data.startswith("admin_grant_gpt_"))
async def admin_grant_gpt(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет прав.", show_alert=True)
        return
    user_id = callback.data.split("admin_grant_gpt_")[1]
    users_data = await load_users()
    if user_id in users_data["users"]:
        users_data["users"][user_id]["gpt_access"] = True
        save_users(users_data)
        logging.info(f"Админ {callback.from_user.id} выдал доступ к GPT пользователю {user_id}")
        try:
            await bot.send_message(int(user_id), "✅ Вам открыт доступ к GPT!", reply_markup=get_user_keyboard())
        except Exception as e:
            logging.error(f"Ошибка при уведомлении пользователя {user_id}: {e}")
        await callback.answer("Доступ открыт.", show_alert=True)
        # Обновить список
        users_data = await load_users()
        buttons = []
        for uid, info in users_data["users"].items():
            if not info.get("gpt_access", False):
                username = info.get("username", "Unknown")
                buttons.append([
                    types.InlineKeyboardButton(
                        text=f"{username} ({uid})",
                        callback_data=f"admin_grant_gpt_{uid}"
                    )
                ])
        if not buttons:
            buttons = [[types.InlineKeyboardButton(text="Нет пользователей без доступа", callback_data="admin_menu")]]
        buttons.append([types.InlineKeyboardButton(text="🔙 Назад", callback_data="admin_menu")])
        await callback.message.edit_text(
            "Выберите пользователя для ОТКРЫТИЯ доступа к GPT:",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons)
        )
    else:
        await callback.answer("Пользователь не найден.", show_alert=True)

# Обработка закрытия доступа через список
@dp.callback_query(lambda c: c.data and c.data.startswith("admin_revoke_gpt_"))
async def admin_revoke_gpt(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет прав.", show_alert=True)
        return
    user_id = callback.data.split("admin_revoke_gpt_")[1]
    users_data = await load_users()
    if user_id in users_data["users"]:
        users_data["users"][user_id]["gpt_access"] = False
        save_users(users_data)
        logging.info(f"Админ {callback.from_user.id} закрыл доступ к GPT пользователю {user_id}")
        try:
            await bot.send_message(int(user_id), "🚫 Ваш доступ к GPT был закрыт.", reply_markup=get_user_keyboard())
        except Exception as e:
            logging.error(f"Ошибка при уведомлении пользователя {user_id}: {e}")
        await callback.answer("Доступ закрыт.", show_alert=True)
        # Обновить список
        users_data = await load_users()
        buttons = []
        for uid, info in users_data["users"].items():
            if info.get("gpt_access", False):
                username = info.get("username", "Unknown")
                buttons.append([
                    types.InlineKeyboardButton(
                        text=f"{username} ({uid})",
                        callback_data=f"admin_revoke_gpt_{uid}"
                    )
                ])
        if not buttons:
            buttons = [[types.InlineKeyboardButton(text="Нет пользователей с доступом", callback_data="admin_menu")]]
        buttons.append([types.InlineKeyboardButton(text="🔙 Назад", callback_data="admin_menu")])
        await callback.message.edit_text(
            "Выберите пользователя для ЗАКРЫТИЯ доступа к GPT:",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons)
        )
    else:
        await callback.answer("Пользователь не найден.", show_alert=True)

# Обработчик открытия меню выдачи доступа к GPT
@dp.callback_query(lambda c: c.data == "admin_open_gpt_access")
async def open_gpt_access_menu(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет прав.", show_alert=True)
        return
    users_data = await load_users()
    buttons = []
    for uid, info in users_data["users"].items():
        if not info.get("gpt_access", False):
            username = info.get("username", "Unknown")
            buttons.append([
                types.InlineKeyboardButton(
                    text=f"{username} ({uid})",
                    callback_data=f"admin_grant_gpt_{uid}"
                )
            ])
    if not buttons:
        buttons = [[types.InlineKeyboardButton(text="Нет пользователей без доступа", callback_data="admin_menu")]]
    buttons.append([types.InlineKeyboardButton(text="🔙 Назад", callback_data="admin_menu")])
    await callback.message.edit_text(
        "Выберите пользователя для ОТКРЫТИЯ доступа к GPT:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await callback.answer()

# Обработчик открытия меню отзыва доступа к GPT
@dp.callback_query(lambda c: c.data == "admin_close_gpt_access")
async def close_gpt_access_menu(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет прав.", show_alert=True)
        return
    users_data = await load_users()
    buttons = []
    for uid, info in users_data["users"].items():
        if info.get("gpt_access", False):
            username = info.get("username", "Unknown")
            buttons.append([
                types.InlineKeyboardButton(
                    text=f"{username} ({uid})",
                    callback_data=f"admin_revoke_gpt_{uid}"
                )
            ])
    if not buttons:
        buttons = [[types.InlineKeyboardButton(text="Нет пользователей с доступом", callback_data="admin_menu")]]
    buttons.append([types.InlineKeyboardButton(text="🔙 Назад", callback_data="admin_menu")])
    await callback.message.edit_text(
        "Выберите пользователя для ЗАКРЫТИЯ доступа к GPT:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await callback.answer()

@dp.callback_query()
async def handle_admin_buttons(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id

    # Проверяем, является ли пользователь администратором
    if not is_admin(user_id):
        await callback.answer("У вас нет прав для выполнения этой команды.")
        return

    # Обработка кнопок
    if callback.data == "admin_view_users":
        users_data = await load_users()
        users_list = []
        for user_id, user_info in users_data["users"].items():
            username = user_info.get("username", "Unknown")
            gpt_access = user_info.get("gpt_access", False)
            users_list.append(f"{user_id} ({username}) | GPT: {'✅' if gpt_access else '❌'}")
        users_text = "\n".join(users_list) or "Нет пользователей."
        await callback.message.edit_text(
            f"Пользователи:\n{users_text}",
            reply_markup=get_admin_keyboard()
        )
    elif callback.data == "admin_block_user":
        users_data = await load_users()
        buttons = []
        for uid, info in users_data["users"].items():
            username = info.get("username", "Unknown")
            if int(uid) in users_data.get("blocked", []):
                btn = types.InlineKeyboardButton(
                    text=f"{username} ({uid}) ✅",
                    callback_data=f"unblock_user_{uid}"
                )
            else:
                btn = types.InlineKeyboardButton(
                    text=f"{username} ({uid}) ❌",
                    callback_data=f"block_user_{uid}"
                )
            buttons.append([btn])
        if not buttons:
            buttons = [[types.InlineKeyboardButton(text="Нет пользователей", callback_data="admin_menu")]]
        buttons.append([types.InlineKeyboardButton(text="🔙 Назад", callback_data="admin_menu")])
        await callback.message.edit_text(
            "Список пользователей для блокировки/разблокировки:",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons)
        )
    elif callback.data == "admin_menu":
        try:
            await callback.message.edit_text(
                "Админ-меню:",
                reply_markup=get_admin_keyboard()
            )
        except Exception:
            try:
                await callback.message.edit_reply_markup(reply_markup=get_admin_keyboard())
            except Exception as e2:
                await callback.answer("Ошибка: " + str(e2), show_alert=True)
                return
        await callback.answer()

# Обработчик кнопки блокировки пользователя
@dp.callback_query(lambda c: c.data and c.data.startswith("block_user_"))
async def block_user_callback(callback: CallbackQuery):
    print(f"block_user_callback: {callback.data}")
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет прав.", show_alert=True)
        return
    user_id = callback.data.split("block_user_")[1]
    users_data = await load_users()
    try:
        user_id_int = int(user_id)
    except Exception:
        await callback.answer("Ошибка ID пользователя.", show_alert=True)
        return
    if user_id_int not in [int(uid) for uid in users_data.get("users", {}).keys()]:
        await callback.answer("Пользователь не найден.", show_alert=True)
        return
    if user_id_int not in users_data.get("blocked", []):
        users_data["blocked"].append(user_id_int)
        save_users(users_data)
        await callback.answer("Пользователь заблокирован.", show_alert=True)
    else:
        await callback.answer("Пользователь уже заблокирован.", show_alert=True)
    await show_lock_menu(callback)
    return

# Обработчик кнопки разблокировки пользователя
@dp.callback_query(lambda c: c.data and c.data.startswith("unblock_user_"))
async def unblock_user_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет прав.", show_alert=True)
        return
    user_id = callback.data.split("unblock_user_")[1]
    users_data = await load_users()
    if int(user_id) in users_data.get("blocked", []):
        users_data["blocked"].remove(int(user_id))
        save_users(users_data)
        await callback.answer("Пользователь разблокирован.", show_alert=True)
    else:
        await callback.answer("Пользователь не был заблокирован.", show_alert=True)
    # Обновить список
    await show_lock_menu(callback)
    return

# Обработка кнопки "Рассылка"
@dp.callback_query(lambda c: c.data == "admin_broadcast")
async def admin_broadcast_callback(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if not is_admin(user_id):
        await callback.answer("У вас нет прав для выполнения этой команды.", show_alert=True)
        return
    await state.set_state(BroadcastState.waiting_for_message)
    try:
        await callback.message.edit_text(
            "Введите текст для рассылки. Он будет отправлен всем пользователям.",
            reply_markup=get_admin_keyboard(cancel_button=True)
        )
    except Exception:
        pass
    await callback.message.answer(
        "Пожалуйста, отправьте текст для рассылки отдельным сообщением:",
        reply_markup=get_admin_keyboard(cancel_button=True)
    )
    await callback.answer()

# Обработка кнопки "Статистика"
@dp.callback_query(lambda c: c.data == "admin_stats")
async def admin_stats_callback(callback: types.CallbackQuery):
    users_data = await load_users()
    total_users = len(users_data["users"])
    blocked_users = len(users_data["blocked"])
    stats_text = (
        f"📊 Статистика использования бота:\n"
        f"• Всего пользователей: {total_users}\n"
        f"• Заблокировано: {blocked_users}\n"
    )
    await callback.message.edit_text(
        stats_text,
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text="🔙 Назад", callback_data="admin_menu")]]
        )
    )

# Обработка кнопки "Главное меню"

# Обработка кнопки "Отмена" для рассылки
@dp.callback_query(lambda callback: callback.data == "cancel_broadcast", BroadcastState.waiting_for_message)
async def cancel_broadcast(callback: CallbackQuery, state: FSMContext):
    # Очищаем состояние
    await state.clear()

    # Возвращаемся в главное меню
    await callback.message.edit_text(
        "Выберите действие:",
        reply_markup=get_admin_keyboard()
    )
    await callback.answer()

# Обработка текста для рассылки
@dp.message(BroadcastState.waiting_for_message)
async def process_broadcast(message: Message, state: FSMContext):
    user_id = message.from_user.id
    if not is_admin(user_id):
        await message.answer("У вас нет прав для выполнения этой команды.")
        await state.clear()
        return

    broadcast_text = message.text
    users_data = await load_users()
    count = 0

    blocked_ids = set(str(uid) for uid in users_data.get("blocked", []))
    for uid, user_info in users_data.get("users", {}).items():
        if uid not in blocked_ids:
            try:
                await bot.send_message(chat_id=int(uid), text=broadcast_text)
                count += 1
                await asyncio.sleep(0.1)
            except Exception as e:
                logging.error(f"Ошибка при отправке сообщения пользователю {uid}: {e}")

    await message.answer(f"Рассылка завершена! Отправлено {count} пользователям.")
    await state.clear()
    await message.answer(
        "Админ-меню:",
        reply_markup=get_admin_keyboard()
    )

# Обработка команды /start
@dp.message(lambda msg: msg.text is not None and msg.text.startswith("/start"))
async def send_welcome(message: types.Message):
    """
    Handle the /start command.
    
    Args:
        message (Message): User's message
    """
    try:
        await message.answer(
            "Привет! Я бот, который может ответить на все твои вопросы. "
            "Вы можете отправить мне текст или изображение.",
            reply_markup=get_user_keyboard(message.from_user.id)
        )
    except Exception as e:
        logging.error(f"Error in send_welcome: {e}")
        await message.answer("Произошла ошибка при запуске бота. Попробуйте позже.")

# Обработка команды /help
@dp.message(lambda msg: msg.text is not None and msg.text.startswith("ℹ️ Помощь"))
async def help_command(message: types.Message):
    """
    Handle the help command.
    
    Args:
        message (Message): User's message
    """
    try:
        await message.answer(
            "ℹ️ Я могу:\n"
            "1. Отвечать на ваши вопросы\n"
            "2. Анализировать изображения\n"
            "3. Помогать с покупкой VPN\n\n"
            "Просто отправьте мне сообщение или фотографию!",
            reply_markup=get_user_keyboard()
        )
    except Exception as e:
        logging.error(f"Error in help_command: {e}")
        await message.answer("Произошла ошибка. Попробуйте позже.")

# --- Клавиатура для запроса доступа к GPT ---
def get_gpt_request_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔓 Запросить доступ к GPT", callback_data="request_gpt_access")]
        ]
    )

# Универсальный запрет на любые сообщения до разрешения доступа к GPT
from aiogram.filters import Command, CommandStart, CommandObject, StateFilter

# Обработка изображений (будет вызван только если доступ есть)
@dp.message(lambda msg: msg.photo is not None)
async def handle_image_message(message: types.Message):
    """
    Handle image messages from users.
    
    Args:
        message (Message): User's message containing photo
    """
    user_id = message.from_user.id
    username = message.from_user.username or "Unknown"

    try:
        # Load user data
        users_data = await load_users()

        # Add user to database if not exists
        if str(user_id) not in users_data["users"]:
            users_data["users"][str(user_id)] = {
                "username": username,
                "gpt_access": False
            }
            save_users(users_data)

        # Check if user is blocked
        if is_blocked(user_id, users_data):
            await message.answer(
                "Вы были заблокированы.",
                reply_markup=get_user_keyboard()
            )
            return

        # Check GPT access
        if not users_data["users"][str(user_id)].get("gpt_access", False):
            await message.answer(
                "Доступ к GPT пока не открыт. Запросите доступ.",
                reply_markup=get_gpt_request_keyboard()
            )
            return

        # Get image URL
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        image_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_info.file_path}"

        # Get caption or default text
        caption = message.caption or "Что на этом изображении?"

        logging.info(f"Received image from user {user_id} with caption: {caption}")

        # Add message to history
        user_histories.setdefault(user_id, [])
        user_histories[user_id].append({"role": "user", "content": caption})
        if len(user_histories[user_id]) > HISTORY_LIMIT:
            user_histories[user_id] = user_histories[user_id][-HISTORY_LIMIT:]

        # Process with OpenRouter
        await query_openrouter_stream(caption, message, image_url=image_url)

    except Exception as e:
        logging.error(f"Error in handle_image_message: {e}")
        await message.answer(
            "Произошла ошибка при обработке изображения. Попробуйте позже.",
            reply_markup=get_user_keyboard()
        )

# Обработка текстовых сообщений (будет вызван только если доступ есть)
@dp.message(StateFilter(None), lambda msg: msg.text)
async def handle_text_message(message: Message, state: FSMContext):
    """
    Handle text messages from users.
    
    Args:
        message (Message): User's message
        state (FSMContext): FSM context
    """
    user_id = message.from_user.id
    username = message.from_user.username or "Unknown"
    user_input = message.text

    # --- Обработка кнопки очистки истории ---
    if user_input.strip() == "🧹 Очистить историю":
        user_histories[user_id] = []
        await message.answer("История диалога очищена!", reply_markup=get_user_keyboard())
        return

    # --- Обработка кнопки очистки истории ---
    if user_input.strip() == "💬 Написать GPT":
        await message.answer("Введите сообщение:", reply_markup=ReplyKeyboardRemove())
        return

    # Пропускаем обработку, если это команда VPN
    if user_input.strip() == "Купить VPN":
        return

    # Загружаем данные о пользователях
    users_data = await load_users()

    # Добавляем пользователя в базу данных, если его еще нет
    if str(user_id) not in users_data["users"]:
        users_data["users"][str(user_id)] = {"username": username, "gpt_access": False}
        save_users(users_data)

    # Проверяем, заблокирован ли пользователь
    if is_blocked(user_id, users_data):
        await message.answer("Вы были заблокированы.", reply_markup=get_user_keyboard())
        return

    # Проверяем доступ к GPT только для чата с GPT
    if not users_data["users"][str(user_id)].get("gpt_access", False):
        await message.answer(
            "Доступ к GPT пока не открыт. Запросите доступ.",
            reply_markup=get_gpt_request_keyboard()
        )
        return

    logging.info(f"Получено сообщение от пользователя {username}: {user_input}")

    # --- Добавляем сообщение пользователя в историю ---
    user_histories.setdefault(user_id, [])
    user_histories[user_id].append({"role": "user", "content": user_input})
    if len(user_histories[user_id]) > HISTORY_LIMIT:
        user_histories[user_id] = user_histories[user_id][-HISTORY_LIMIT:]

    try:
        await query_openrouter_stream(user_input, message)
    except Exception as e:
        logging.error(f"Ошибка при обращении к ИИ: {e}")
        await message.answer("Извините, произошла ошибка. Попробуйте позже.", reply_markup=get_user_keyboard())

# --- Клавиатура для выбора периода VPN ---
def get_vpn_inline_keyboard() -> InlineKeyboardMarkup:
    """
    Create inline keyboard for VPN period selection.
    
    Returns:
        InlineKeyboardMarkup: Keyboard with VPN period options
    """
    try:
        keyboard = [
            [
                types.InlineKeyboardButton(text="1 месяц", callback_data="vpn_period_1m"),
                types.InlineKeyboardButton(text="3 месяца", callback_data="vpn_period_3m")
            ],
            [
                types.InlineKeyboardButton(text="6 месяцев", callback_data="vpn_period_6m"),
                types.InlineKeyboardButton(text="1 год", callback_data="vpn_period_1y")
            ]
        ]
        return types.InlineKeyboardMarkup(inline_keyboard=keyboard)
    except Exception as e:
        logging.error(f"Error creating VPN keyboard: {e}")
        return None

# Регистрация обработчиков
def register_handlers():
    """Register all message and callback handlers."""
    # VPN handlers
    dp.message.register(handle_buy_vpn, lambda msg: msg.text and msg.text.strip() == "Купить VPN")
    dp.callback_query.register(select_period_inline, StateFilter(BuyVPNState.select_period), lambda c: c.data.startswith("vpn_period_"))
    dp.callback_query.register(paid_inline, StateFilter(BuyVPNState.wait_payment), lambda c: c.data == "vpn_paid")
    dp.callback_query.register(cancel_vpn_purchase, lambda c: c.data == "vpn_cancel")
    dp.callback_query.register(reject_vpn_access, lambda c: c.data and c.data.startswith("vpn_reject_"))
    
    # Admin handlers
    dp.message.register(admin_command, lambda msg: msg.text is not None and msg.text.startswith("Админ-меню"))
    dp.callback_query.register(show_lock_menu, lambda c: c.data == "admin_lock_menu")
    dp.callback_query.register(process_gpt_access_request, lambda c: c.data == "request_gpt_access")
    dp.callback_query.register(admin_approve_gpt, lambda c: c.data and c.data.startswith("admin_approve_gpt_"))
    dp.callback_query.register(admin_decline_gpt, lambda c: c.data and c.data.startswith("admin_decline_gpt_"))
    dp.callback_query.register(admin_grant_gpt, lambda c: c.data and c.data.startswith("admin_grant_gpt_"))
    dp.callback_query.register(admin_revoke_gpt, lambda c: c.data and c.data.startswith("admin_revoke_gpt_"))
    dp.callback_query.register(open_gpt_access_menu, lambda c: c.data == "admin_open_gpt_access")
    dp.callback_query.register(close_gpt_access_menu, lambda c: c.data == "admin_close_gpt_access")
    dp.callback_query.register(handle_admin_buttons)
    dp.callback_query.register(block_user_callback, lambda c: c.data and c.data.startswith("block_user_"))
    dp.callback_query.register(unblock_user_callback, lambda c: c.data and c.data.startswith("unblock_user_"))
    dp.callback_query.register(admin_broadcast_callback, lambda c: c.data == "admin_broadcast")
    dp.callback_query.register(admin_stats_callback, lambda c: c.data == "admin_stats")
    dp.callback_query.register(cancel_broadcast, lambda callback: callback.data == "cancel_broadcast", BroadcastState.waiting_for_message)
    
    # Message handlers
    dp.message.register(send_welcome, lambda msg: msg.text is not None and msg.text.startswith("/start"))
    dp.message.register(help_command, lambda msg: msg.text is not None and msg.text.startswith("ℹ️ Помощь"))
    dp.message.register(handle_image_message, lambda msg: msg.photo is not None)
    dp.message.register(handle_text_message, StateFilter(None), lambda msg: msg.text)
    dp.message.register(process_broadcast, BroadcastState.waiting_for_message)
    
    # Chat member handler
    dp.chat_member.register(handle_new_chat_members)

@dp.message(lambda msg: msg.text and msg.text.strip() == "Купить VPN")
async def handle_buy_vpn(message: Message, state: FSMContext):
    """
    Handle the initial VPN purchase request.
    
    Args:
        message (Message): User's message
        state (FSMContext): FSM context
    """
    try:
        logging.info(f"VPN purchase request from user {message.from_user.id}")
        
        # Проверяем, не заблокирован ли пользователь
        users_data = await load_users()
        if is_blocked(message.from_user.id, users_data):
            await message.answer(
                "Вы были заблокированы.",
                reply_markup=get_user_keyboard()
            )
            return

        # Создаем клавиатуру для выбора периода
        keyboard = get_vpn_inline_keyboard()
        if not keyboard:
            logging.error("Failed to create VPN keyboard")
            await message.answer(
                "Произошла ошибка при создании меню. Попробуйте позже.",
                reply_markup=get_user_keyboard()
            )
            return

        # Отправляем сообщение с клавиатурой
        await message.answer(
            "На какой период хотите приобрести VPN?",
            reply_markup=keyboard
        )
        
        # Устанавливаем состояние
        await state.set_state(BuyVPNState.select_period)
        logging.info(f"Set state to BuyVPNState.select_period for user {message.from_user.id}")
        
    except Exception as e:
        logging.error(f"Error in handle_buy_vpn: {e}")
        await message.answer(
            "Произошла ошибка при обработке запроса. Попробуйте позже.",
            reply_markup=get_user_keyboard()
        )

@dp.callback_query(StateFilter(BuyVPNState.select_period), lambda c: c.data.startswith("vpn_period_"))
async def select_period_inline(callback: CallbackQuery, state: FSMContext):
    """
    Handle VPN period selection.
    
    Args:
        callback (CallbackQuery): Callback query
        state (FSMContext): FSM context
    """
    try:
        logging.info(f"VPN period selection from user {callback.from_user.id}: {callback.data}")
        
        period_map = {
            "vpn_period_1m": {"period": "1 месяц", "price": 599, "days": 30},
            "vpn_period_3m": {"period": "3 месяца", "price": 1797, "days": 90},
            "vpn_period_6m": {"period": "6 месяцев", "price": 3594, "days": 180},
            "vpn_period_1y": {"period": "1 год", "price": 7188, "days": 365},
        }
        
        if callback.data not in period_map:
            logging.error(f"Invalid period selected: {callback.data}")
            await callback.answer("Неверный период. Попробуйте снова.", show_alert=True)
            return
            
        selected = period_map[callback.data]
        await state.update_data(
            period=selected["period"],
            price=selected["price"],
            days=selected["days"],
            username=callback.from_user.username or "Unknown"
        )
        
        pending_vpn_requests[callback.from_user.id] = await state.get_data()
        
        payment_text = (
            f"Вы выбрали: {selected['period']}\n"
            f"Стоимость: {selected['price']} рублей.\n\n"
            "Для оплаты переведите сумму по реквизитам СБП:\n"
            "Номер телефона: +79991234567\n"
            "После оплаты нажмите кнопку 'Оплачено'."
        )
        
        await callback.message.edit_text(
            payment_text,
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="Оплачено", callback_data="vpn_paid")],
                [types.InlineKeyboardButton(text="Отмена", callback_data="vpn_cancel")]
            ])
        )
        await state.set_state(BuyVPNState.wait_payment)
        await callback.answer()
        
    except Exception as e:
        logging.error(f"Error in select_period_inline: {e}")
        await callback.message.edit_text(
            "Произошла ошибка при выборе периода. Попробуйте позже.",
            reply_markup=get_user_keyboard()
        )
        await state.finish()

@dp.callback_query(lambda c: c.data == "vpn_cancel")
async def cancel_vpn_purchase(callback: CallbackQuery, state: FSMContext):
    """
    Handle VPN purchase cancellation.
    
    Args:
        callback (CallbackQuery): Callback query
        state (FSMContext): FSM context
    """
    try:
        user_id = callback.from_user.id
        if user_id in pending_vpn_requests:
            del pending_vpn_requests[user_id]
        await state.finish()
        await callback.message.edit_text(
            "Покупка VPN отменена.",
            reply_markup=get_user_keyboard()
        )
        await callback.answer()
    except Exception as e:
        logging.error(f"Error in cancel_vpn_purchase: {e}")
        await callback.answer("Ошибка при отмене. Попробуйте позже.", show_alert=True)

@dp.callback_query(StateFilter(BuyVPNState.wait_payment), lambda c: c.data == "vpn_paid")
async def paid_inline(callback: CallbackQuery, state: FSMContext):
    """
    Handle VPN payment confirmation.
    
    Args:
        callback (CallbackQuery): Callback query
        state (FSMContext): FSM context
    """
    try:
        data = await state.get_data()
        user_id = callback.from_user.id
        username = callback.from_user.username or "Unknown"
        
        await callback.message.edit_text(
            "Ваша заявка принята. Ожидайте подтверждение оплаты от администратора."
        )
        
        # Notify admins
        admin_keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="✅ Подтвердить",
                    callback_data=f"vpn_grant_{user_id}"
                ),
                types.InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=f"vpn_reject_{user_id}"
                )
            ]
        ])
        
        admin_message = (
            f"Новая заявка на VPN!\n"
            f"От: @{username} (ID: {user_id})\n"
            f"Период: {data.get('period', 'N/A')}\n"
            f"Сумма: {data.get('price', 'N/A')} рублей"
        )
        
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=admin_message,
                    reply_markup=admin_keyboard
                )
            except Exception as e:
                logging.error(f"Failed to notify admin {admin_id}: {e}")
        
        await state.set_state(BuyVPNState.admin_grant)
        await callback.answer()
        
    except Exception as e:
        logging.error(f"Error in paid_inline: {e}")
        await callback.message.edit_text(
            "Произошла ошибка при обработке оплаты. Пожалуйста, свяжитесь с администратором.",
            reply_markup=get_user_keyboard()
        )
        await state.finish()

@dp.callback_query(lambda c: c.data and c.data.startswith("vpn_reject_"))
async def reject_vpn_access(callback: CallbackQuery):
    """
    Handle VPN access rejection by admin.
    
    Args:
        callback (CallbackQuery): Callback query
    """
    if not is_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
        
    try:
        user_id = int(callback.data.split("_")[-1])
        if user_id in pending_vpn_requests:
            del pending_vpn_requests[user_id]
            
        try:
            await bot.send_message(
                chat_id=user_id,
                text="❌ Ваша оплата не подтверждена. Пожалуйста, свяжитесь с администратором.",
                reply_markup=get_user_keyboard()
            )
        except Exception as e:
            logging.error(f"Failed to notify user {user_id} about rejection: {e}")
            
        await callback.message.edit_text("Доступ отклонен.")
        await callback.answer("Пользователь уведомлен об отказе.", show_alert=True)
        
    except Exception as e:
        logging.error(f"Error in reject_vpn_access: {e}")
        await callback.answer("Ошибка при отклонении доступа.", show_alert=True)

# Функция для отправки запроса к OpenRouter с потоковой передачей
async def query_openrouter_stream(prompt: str, message: Message, image_url: str = None):
    """
    Send a streaming request to OpenRouter API and handle the response.
    
    Args:
        prompt (str): User's input prompt
        message (Message): Original Telegram message
        image_url (str, optional): URL of the image if present
    """
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://yourwebsite.com",
        "X-Title": "Telegram Bot",
    }

    user_id = message.from_user.id
    messages = user_histories.get(user_id, [])[:]
    
    if image_url and messages and messages[-1]["role"] == "user":
        messages[-1]["content"] = [
            {"type": "text", "text": messages[-1]["content"]},
            {"type": "image_url", "image_url": {"url": image_url}}
        ]

    payload = {
        "model": "qwen/qwen2.5-vl-3b-instruct:free",
        "messages": messages if messages else [{"role": "user", "content": prompt}],
        "temperature": BOT_SETTINGS["temperature"],
        "max_tokens": BOT_SETTINGS["max_tokens"],
        "stream": BOT_SETTINGS["stream"],
    }

    sent_message = await message.answer(".")
    buffer = ""
    last_sent_text = ""
    last_update_time = asyncio.get_event_loop().time()
    wave_states = [".", "..", "...", ".."]
    wave_index = 0
    wave_task_running = True
    last_wave_text = ""

    async def animate_wave():
        nonlocal wave_index, wave_task_running, last_wave_text
        try:
            while wave_task_running:
                await asyncio.sleep(0.6)
                if not wave_task_running:
                    break
                new_text = wave_states[wave_index]
                if new_text != last_wave_text:
                    try:
                        await bot.edit_message_text(
                            chat_id=sent_message.chat.id,
                            message_id=sent_message.message_id,
                            text=new_text
                        )
                        last_wave_text = new_text
                    except Exception as e:
                        if "message is not modified" not in str(e):
                            logging.error(f"Animation error: {e}")
                wave_index = (wave_index + 1) % len(wave_states)
        except Exception as e:
            logging.error(f"Animation error: {e}")

    wave_task = asyncio.create_task(animate_wave())

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(OPENROUTER_API_URL, headers=headers, json=payload, timeout=30) as response:
                if response.status == 200:
                    async for chunk in response.content.iter_any():
                        if not wave_task_running:
                            break
                        try:
                            chunk_text = chunk.decode("utf-8")
                            for line in chunk_text.split("\n"):
                                if not line.strip():
                                    continue
                                if line.startswith("data: "):
                                    line_data = line[6:].strip()
                                    if line_data == "[DONE]":
                                        break
                                    try:
                                        json_data = json.loads(line_data)
                                        content = json_data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                                        if content:
                                            buffer += content

                                            current_time = asyncio.get_event_loop().time()
                                            if len(buffer) > 40 and current_time - last_update_time >= 4:
                                                new_text = buffer + "..."
                                                if new_text != last_sent_text:
                                                    try:
                                                        await bot.edit_message_text(
                                                            chat_id=sent_message.chat.id,
                                                            message_id=sent_message.message_id,
                                                            text=new_text
                                                        )
                                                        last_sent_text = new_text
                                                        last_update_time = current_time
                                                    except Exception as e:
                                                        if "message is not modified" not in str(e):
                                                            logging.error(f"Message update error: {e}")
                                    except json.JSONDecodeError:
                                        logging.error(f"JSON decode error in line: {line}")
                                    except Exception as e:
                                        logging.error(f"Content processing error: {e}")
                        except Exception as e:
                            logging.error(f"Chunk processing error: {e}")
                    
                    if buffer and buffer != last_sent_text:
                        try:
                            await bot.edit_message_text(
                                chat_id=sent_message.chat.id,
                                message_id=sent_message.message_id,
                                text=buffer
                            )
                        except Exception as e:
                            if "message is not modified" not in str(e):
                                logging.error(f"Final message update error: {e}")
                else:
                    try:
                        error_data = await response.json()
                        error_message = error_data.get("error", {}).get("message", "Unknown error")
                        logging.error(f"OpenRouter error: {response.status}, {error_message}")
                        await bot.edit_message_text(
                            chat_id=sent_message.chat.id,
                            message_id=sent_message.message_id,
                            text=f"Error {response.status}: {error_message}"
                        )
                    except Exception as e:
                        logging.error(f"Error response processing error: {e}")
                        await bot.edit_message_text(
                            chat_id=sent_message.chat.id,
                            message_id=sent_message.message_id,
                            text=f"Error {response.status}: Failed to process error message"
                        )
    except asyncio.TimeoutError:
        await bot.edit_message_text(
            chat_id=sent_message.chat.id,
            message_id=sent_message.message_id,
            text="Request timed out. Please try again."
        )
    except Exception as e:
        logging.error(f"Request error: {e}")
        await bot.edit_message_text(
            chat_id=sent_message.chat.id,
            message_id=sent_message.message_id,
            text="An error occurred. Please try again later."
        )
    finally:
        wave_task_running = False
        try:
            await wave_task
        except Exception as e:
            logging.error(f"Wave task cleanup error: {e}")

    if buffer:
        user_histories.setdefault(user_id, [])
        user_histories[user_id].append({"role": "assistant", "content": buffer})
        if len(user_histories[user_id]) > HISTORY_LIMIT:
            user_histories[user_id] = user_histories[user_id][-HISTORY_LIMIT:]

# Автоотправка /start при входе пользователя в чат
@dp.chat_member()
async def handle_new_chat_members(event: types.ChatMemberUpdated):
    if event.new_chat_member.status == "member":
        try:
            await bot.send_message(
                chat_id=event.chat.id,
                text="/start"
            )
        except Exception:
            pass

# Запуск бота
async def main():
    """
    Main function to start the bot.
    """
    try:
        logging.info("Starting bot...")
        register_handlers()  # Register all handlers
        await dp.start_polling(bot)
    except Exception as e:
        logging.error(f"Error in main: {e}")
    finally:
        logging.info("Bot stopped")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped manually")
    except Exception as e:
        logging.error(f"Critical error: {e}")
