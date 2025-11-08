import asyncio
import logging
import re
import os
import traceback
import contextlib
from typing import Union, Optional, Tuple, Any
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ChatMemberStatus
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command

# REDIS / STORAGE
from aiogram.fsm.storage.redis import RedisStorage, Redis
from aiogram.fsm.storage.memory import MemoryStorage

# Конфиг и БД (убедитесь, что REQUIRED_CHANNEL_ID в config_1 - int)
from config_1 import BOT_TOKEN, REQUIRED_CHANNEL_ID
from database import db

# Логирование
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# CONSTANTS
REQUIRED_CHANNEL_USERNAME = '@life_in_stile'
REQUIRED_CHANNEL_URL = f"https://t.me/{REQUIRED_CHANNEL_USERNAME.lstrip('@')}"

# REDIS STORAGE
REDIS_DSN = os.getenv("REDIS_DSN")
if REDIS_DSN:
    try:
        redis_instance = Redis.from_url(REDIS_DSN)
        storage = RedisStorage(redis=redis_instance)
        logger.info("FSM Storage: RedisStorage используется.")
    except Exception:
        logger.exception("Ошибка при подключении к Redis. Falling back to MemoryStorage.")
        storage = MemoryStorage()
else:
    logger.warning("REDIS_DSN не задан. Используется MemoryStorage.")
    storage = MemoryStorage()

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=storage)

# FSM
class ChannelForm(StatesGroup):
    waiting_for_channel_link = State()

# HELPERS
async def is_member(user_id: int, channel_id: int) -> bool:
    """Проверяем членство (актуально: может быть медленно при большом количестве вызовов)."""
    try:
        member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
        # Считаем, что все статусы, кроме LEFT/KICKED, означают членство (в т.ч. restricted)
        return member.status not in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED)
    except TelegramForbiddenError:
        # Бот не имеет доступа к чату, значит не может проверить -> считаем, что не член
        logger.warning("Нет доступа к информации о членстве (TelegramForbiddenError).")
        return False
    except TelegramBadRequest as e:
        logger.warning(f"TelegramBadRequest при проверке member: {e}")
        return False
    except Exception:
        logger.exception("Неожиданная ошибка в is_member")
        return False

def format_link_for_button(link: str) -> str:
    """
    Гарантирует, что ссылка имеет формат URL для кнопки (http/https).
    ЭТОТ БЛОК БЫЛ ИСПРАВЛЕН
    """
    # 1. Если это @username, преобразуем в полную ссылку
    if link.startswith('@'):
        return f"https://t.me/{link.lstrip('@')}"
    
    # 2. Если ссылка не начинается с http/https, делаем ее ссылкой на t.me
    if not link.lower().startswith(('http://', 'https://')):
        # Удаляем возможные t.me/ и добавляем префикс https://t.me/
        # Это исправляет случай, когда в базе лежит просто 'username' без @ или префикса.
        cleaned_link = link.lstrip('t.me/').lstrip('https://t.me/')
        return f"https://t.me/{cleaned_link}"
    
    # 3. Иначе возвращаем ссылку как есть (это полный http/https URL)
    return link


# KEYBOARDS
def get_main_keyboard(is_registered: bool = False):
    builder = InlineKeyboardBuilder()
    builder.button(text="🎯 Хочу Подписку (Обмен)", callback_data="start_exchange")
    if is_registered:
        builder.button(text="📊 Мой Канал (Баланс)", callback_data="my_channel_stats")
    else:
        builder.button(text="➕ Зарегистрировать канал", callback_data="register_channel")
    builder.adjust(1)
    return builder.as_markup()

def get_join_main_channel_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text=f"✅ Подписаться на {REQUIRED_CHANNEL_USERNAME}", url=REQUIRED_CHANNEL_URL)
    builder.button(text="Проверить подписку", callback_data="check_required_sub")
    builder.adjust(1)
    return builder.as_markup()

def get_subscription_keyboard(channel_link: str, channel_id: int):
    builder = InlineKeyboardBuilder()
    valid_url = format_link_for_button(channel_link) # Здесь используется исправленный хелпер
    builder.button(text="✅ Подписаться на канал", url=valid_url)
    # Мы всё ещё передаём channel_id в callback, но проверяем caller_id при обработке
    builder.button(text="Подписка оформлена", callback_data=f"sub_done:{channel_id}")
    builder.adjust(1)
    return builder.as_markup()

# HANDLERS
@dp.message(Command("start"))
async def start_handler(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username or f"id{user_id}"
    await state.clear()
    try:
        await db.add_user(user_id, username)
    except Exception:
        logger.exception("Ошибка при записи пользователя в БД")

    if not await is_member(user_id, REQUIRED_CHANNEL_ID):
        await message.answer(
            "👋 Добро пожаловать!\n\n"
            "Для начала работы с ботом, пожалуйста, подпишитесь на наш основной канал:",
            reply_markup=get_join_main_channel_keyboard()
        )
        return

    channel_info = await db.get_user_channel_info(user_id)
    await message.answer(
        "✅ Вы подписаны на наш канал. Выберите действие:",
        reply_markup=get_main_keyboard(is_registered=channel_info is not None)
    )

@dp.callback_query(F.data == "check_required_sub")
async def process_check_required_sub(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    try:
        if await is_member(user_id, REQUIRED_CHANNEL_ID):
            channel_info = await db.get_user_channel_info(user_id)
            try:
                await callback.message.edit_text(
                    "✅ Вы успешно подписались на наш канал!\n\n"
                    "Теперь вы можете обмениваться подписками.",
                    reply_markup=get_main_keyboard(is_registered=channel_info is not None)
                )
            except TelegramBadRequest:
                # Если нельзя отредактировать, отправляем новое сообщение
                await callback.message.answer(
                    "✅ Вы успешно подписались на наш канал!\n\n"
                    "Теперь вы можете обмениваться подписками.",
                    reply_markup=get_main_keyboard(is_registered=channel_info is not None)
                )
        else:
            await callback.answer("❌ Подписка не найдена. Пожалуйста, подпишитесь.", show_alert=True)
    except Exception:
        logger.exception("Ошибка в process_check_required_sub")
        await callback.answer("Ошибка при проверке подписки.", show_alert=True)

# FSM: register channel
@dp.callback_query(F.data == "register_channel")
async def register_channel_start(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if await db.get_user_channel_info(user_id):
        await callback.answer("У вас уже есть зарегистрированный канал.", show_alert=True)
        return
    await state.set_state(ChannelForm.waiting_for_channel_link)
    try:
        await callback.message.edit_text(
            "📝 Регистрация канала\n\n"
            "Отправьте публичную ссылку на ваш канал (например, @channel_name или https://t.me/channel_name).\n\n"
            "Важно: бот должен быть админом вашего канала (право приглашения).",
            reply_markup=None
        )
    except TelegramBadRequest:
        await callback.message.answer(
            "📝 Регистрация канала\n\n"
            "Отправьте публичную ссылку на ваш канал (например, @channel_name или https://t.me/channel_name).\n\n"
            "Важно: бот должен быть админом вашего канала (право приглашения)."
        )
    await callback.answer()

@dp.message(ChannelForm.waiting_for_channel_link)
async def process_channel_link(message: types.Message, state: FSMContext):
    link = message.text.strip()
    user_id = message.from_user.id

    if message.text.startswith('/'):
        await message.answer("❌ Вы находитесь в режиме регистрации канала. Отправьте, пожалуйста, только ссылку.")
        return

    # ИСПРАВЛЕНИЕ: Упрощаем регулярное выражение для надежного извлечения username.
    match = re.search(r'(?:@|t\.me/)([A-Za-z0-9_]{5,32})', link, re.IGNORECASE)

    if not match:
        await message.answer("❌ Некорректный формат ссылки. Используйте @username или https://t.me/username.")
        return

    # Форматируем полученное имя в формат, который гарантированно работает с bot.get_chat()
    raw_username = match.group(1)
    channel_username = '@' + raw_username 
    
    try:
        chat = await bot.get_chat(chat_id=channel_username)
        channel_id = chat.id

        # Рекомендация: проверить, что бот является админом в этом чате
        try:
            bot_member = await bot.get_chat_member(chat_id=channel_id, user_id=(await bot.get_me()).id)
            if bot_member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
                await message.answer("❌ Бот не является администратором в вашем канале. Пожалуйста, добавьте бота и дайте нужные права (особенно: 'Приглашать пользователей').")
                await state.clear()
                return
        except Exception:
            logger.exception("Не удалось проверить права бота в канале. Продолжаем, но рекомендуется проверить права.")

        await db.add_channel(user_id, channel_id, channel_username, chat.title)
        await state.clear()
        await message.answer(
            f"✅ Канал <b>{chat.title}</b> ({channel_username}) успешно зарегистрирован!\n\n"
            "Сейчас мы найдем вам первый канал для взаимной подписки..."
        )
        # Автозапуск обмена — передаём оригинальное message для удобства
        await start_exchange_process(message)

    except TelegramBadRequest as e:
        # 🚨 ДИАГНОСТИКА: Выводим точный текст ошибки от Telegram
        error_msg = str(e)
        logger.exception(f"TelegramBadRequest в process_channel_link: {error_msg}")
        await message.answer(f"❌ Telegram API Ошибка: **{error_msg}**\n\nУбедитесь, что: \n1. Канал публичный и активен. \n2. Бот имеет все необходимые права администратора (особенно 'Изменять информацию' и 'Приглашать пользователей').")
    except Exception:
        logger.exception("Критическая ошибка в process_channel_link")
        await message.answer("Произошла критическая ошибка. Попробуйте снова позже.")

@dp.message()
async def unhandled_message(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    logger.warning(f"Необработанное сообщение от {message.from_user.id}: {message.text}, State: {current_state}")
    if current_state:
        await message.answer(f"Вы находитесь в состоянии {current_state}. Пожалуйста, следуйте инструкциям или нажмите /start для сброса.")
    else:
        await message.answer("Я не знаю, что делать с этим сообщением. Начните с команды /start.")

# EXCHANGE LOGIC
@dp.callback_query(F.data == "start_exchange")
async def start_exchange_process(update_obj: Union[types.CallbackQuery, types.Message]):
    is_callback = isinstance(update_obj, types.CallbackQuery)
    message_obj = update_obj.message if is_callback else update_obj
    user_id = update_obj.from_user.id if is_callback else update_obj.from_user.id

    user_channel_info = await db.get_user_channel_info(user_id)
    # Функция для безопасного редактирования (с fallback)
    async def safe_reply_or_edit(text: str, reply_markup: Optional[Any] = None):
        try:
            if is_callback:
                await message_obj.edit_text(text, reply_markup=reply_markup)
            else:
                await message_obj.answer(text, reply_markup=reply_markup)
        except TelegramBadRequest:
            # fallback
            await message_obj.answer(text, reply_markup=reply_markup)
        except Exception:
            logger.exception("Ошибка при отправке/редактировании сообщения в start_exchange_process")

    if not user_channel_info:
        kb = InlineKeyboardBuilder().button(text="➕ Зарегистрировать канал", callback_data="register_channel").as_markup()
        await safe_reply_or_edit(
            "⚠️ Ваш канал не зарегистрирован.\n\nЧтобы начать обмен, сначала зарегистрируйте свой канал:",
            reply_markup=kb
        )
        if is_callback:
            await update_obj.answer()
        return

    target_channel_info = await db.get_target_channel(user_id)
    if target_channel_info:
        target_channel_id, target_channel_link, target_channel_title = target_channel_info
        msg = (
            f"✨ Обмен Подписками\n\n"
            f"1. Подпишитесь на этот канал:\n"
            f"Канал: <b>{target_channel_title}</b>\n"
            f"Ссылка: <code>{target_channel_link}</code>\n\n"
            f"После подписки нажмите кнопку 'Подписка оформлена'."
        )
        await safe_reply_or_edit(msg, reply_markup=get_subscription_keyboard(target_channel_link, target_channel_id))
    else:
        await safe_reply_or_edit(
            "😴 Нет доступных каналов для обмена. Попробуйте позже.",
            reply_markup=get_main_keyboard(is_registered=True)
        )

    if is_callback:
        await update_obj.answer()

@dp.callback_query(F.data.startswith("sub_done:"))
async def process_subscription_done(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    username_a = callback.from_user.username or f"id{user_id}"
    try:
        parts = callback.data.split(":")
        if len(parts) != 2:
            await callback.answer("Некорректные данные.", show_alert=True)
            return
        subscribed_channel_id = int(parts[1])

        if not await is_member(user_id, subscribed_channel_id):
            await callback.answer("❌ Мы не видим вашей подписки на целевой канал. Попробуйте еще раз.")
            return

        user_channel_info = await db.get_user_channel_info(user_id)
        if not user_channel_info:
            await callback.answer("Ошибка: Ваш канал не найден. Начните с /start.")
            return

        subscriber_channel_id = user_channel_info[0]
        subscriber_channel_link = user_channel_info[1]
        subscriber_channel_title = user_channel_info[2]

        channel_b_owner_info = await db.get_channel_owner_info(subscribed_channel_id)
        if not channel_b_owner_info:
            logger.error(f"Не найден владелец для канала ID: {subscribed_channel_id}")
            await callback.answer("Ошибка системы: не найден владелец канала B.")
            return

        channel_b_owner_id = channel_b_owner_info[0]
        channel_b_title = channel_b_owner_info[1]

        # Транзакция регистрации подписки и создания долга в БД
        try:
            await db.register_subscription_and_create_debt(
                subscriber_user_id=user_id,
                subscribed_channel_id=subscribed_channel_id,
                subscriber_channel_id=subscriber_channel_id
            )
        except Exception:
            logger.exception("Ошибка транзакции в register_subscription_and_create_debt")
            await callback.answer("❌ Ошибка при регистрации транзакции. Попробуйте снова.")
            return

        # Уведомление владельца канала B — с кнопками
        try:
            builder = InlineKeyboardBuilder()
            valid_url_a = format_link_for_button(subscriber_channel_link)
            builder.button(text=f"1️⃣ Подписаться на {subscriber_channel_title}", url=valid_url_a)
            # Важно: в callback мы передаём owner_id и channel A, но ниже проверяем совпадение caller_id == owner_id
            callback_data = f"confirm_reciprocal_sub:{channel_b_owner_
