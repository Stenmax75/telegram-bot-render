import asyncio
import logging
import re
import os
import contextlib
from typing import Union, Optional, Any
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
from database import NotFoundError # Импортируем NotFoundError для обработки ошибок БД

# --- КОНФИГУРАЦИЯ И ЗАПУСК ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
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

# Инициализация
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=storage)

# FSM States
class ChannelForm(StatesGroup):
    waiting_for_channel_link = State()

# --- HELPERS ---

async def is_member(user_id: int, channel_id: Union[int, str]) -> bool:
    """Проверяем членство. Используется CREATOR вместо OWNER."""
    try:
        member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
        # CREATOR - владелец, ADMINISTRATOR - админ, MEMBER - подписчик
        return member.status in (
            ChatMemberStatus.MEMBER, 
            ChatMemberStatus.ADMINISTRATOR, 
            ChatMemberStatus.CREATOR
        )
    except (TelegramForbiddenError, TelegramBadRequest):
        return False
    except Exception:
        logger.exception("Неожиданная ошибка в is_member")
        return False

def format_link_for_button(link: str) -> str:
    """
    Гарантирует, что ссылка имеет формат URL (http/https) для inline-кнопки.
    """
    link = link.strip()
    
    # 1. Если это @username, преобразуем в полную ссылку
    if link.startswith('@'):
        return f"https://t.me/{link.lstrip('@')}"
    
    # 2. Если ссылка не начинается с http/https, делаем ее ссылкой на t.me
    if not link.lower().startswith(('http://', 'https://')):
        # Избегаем двойного t.me/ в ссылке
        link = link.replace('t.me/', '').replace('https://t.me/', '')
        link = link.lstrip('/')
        return f"https://t.me/{link}"
    
    # 3. Иначе возвращаем ссылку как есть
    return link


async def safe_edit_or_reply(update_obj: Union[types.CallbackQuery, types.Message], text: str, reply_markup: Optional[Any] = None):
    """Безопасно редактирует сообщение колбэка или отвечает на сообщение."""
    is_callback = isinstance(update_obj, types.CallbackQuery)
    message_obj = update_obj.message if is_callback else update_obj
    
    try:
        if is_callback and message_obj:
            # Сначала пытаемся редактировать
            await message_obj.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
            # Важно: answer() для колбэка вызывается в конце хендлера, если нет ошибок
        else:
            # Если это обычное сообщение, просто отвечаем
            await message_obj.answer(text, reply_markup=reply_markup, parse_mode="HTML")
    except TelegramBadRequest as e:
        # Если редактирование не удалось (например, сообщение слишком старое или текст не изменился)
        if message_obj and "message is not modified" not in str(e):
            logger.warning(f"Ошибка редактирования: {e}. Отправка нового сообщения.")
            # Отправка нового сообщения как запасной вариант
            await message_obj.answer(text, reply_markup=reply_markup, parse_mode="HTML")
        elif message_obj and "message is not modified" in str(e):
            # Игнорируем ошибку, если текст не изменился
            pass
        elif not message_obj:
             logger.error("TelegramBadRequest, но message_obj отсутствует.")
    except Exception:
        logger.exception("Ошибка при отправке/редактировании сообщения")


# --- KEYBOARDS ---

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
    valid_url = format_link_for_button(channel_link)
    builder.button(text="✅ Подписаться на канал", url=valid_url)
    builder.button(text="Подписка оформлена", callback_data=f"sub_done:{channel_id}")
    builder.adjust(1)
    return builder.as_markup()

# --- HANDLERS ---

@dp.message(Command("start"))
async def start_handler(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username or f"id{user_id}"
    await state.clear()
    
    try:
        await db.add_user(user_id, username) # Добавляем/обновляем пользователя
        
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
    except Exception:
        logger.exception("Ошибка при старте или записи пользователя в БД")
        await message.answer("Произошла ошибка при запуске бота. Попробуйте позже.")


@dp.callback_query(F.data == "check_required_sub")
async def process_check_required_sub(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    try:
        if await is_member(user_id, REQUIRED_CHANNEL_ID):
            channel_info = await db.get_user_channel_info(user_id)
            
            await safe_edit_or_reply(
                callback,
                "✅ Вы успешно подписались на наш канал!\n\n"
                "Теперь вы можете обмениваться подписками.",
                reply_markup=get_main_keyboard(is_registered=channel_info is not None)
            )
        else:
            await callback.answer("❌ Подписка не найдена. Пожалуйста, подпишитесь.", show_alert=True)
    except Exception:
        logger.exception("Ошибка в process_check_required_sub")
        await callback.answer("Ошибка при проверке подписки.", show_alert=True)
    finally:
        await callback.answer() # Ответ на колбэк в конце


# FSM: register channel
@dp.callback_query(F.data == "register_channel")
async def register_channel_start(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if await db.get_user_channel_info(user_id):
        await callback.answer("У вас уже есть зарегистрированный канал.", show_alert=True)
        return
        
    await state.set_state(ChannelForm.waiting_for_channel_link)
    
    await safe_edit_or_reply(
        callback,
        "📝 Регистрация канала\n\n"
        "Отправьте публичную ссылку на ваш канал (например, @channel_name или https://t.me/channel_name).\n\n"
        "Важно: **бот должен быть админом** вашего канала (право приглашения)."
    )
    await callback.answer()


@dp.message(ChannelForm.waiting_for_channel_link)
async def process_channel_link(message: types.Message, state: FSMContext):
    link = message.text.strip()
    user_id = message.from_user.id

    if message.text.startswith('/'):
        await message.answer("❌ Вы находитесь в режиме регистрации канала. Отправьте, пожалуйста, только ссылку.")
        return

    # Извлекаем чистое имя пользователя из ссылки
    match = re.search(r'(?:@|t\.me/)([A-Za-z0-9_]{5,32})', link, re.IGNORECASE)

    if not match:
        await message.answer("❌ Некорректный формат ссылки. Используйте @username или https://t.me/username.")
        return

    channel_username = '@' + match.group(1) # Всегда работаем с @username

    try:
        chat = await bot.get_chat(chat_id=channel_username)
        channel_id = chat.id

        # Проверка прав администратора
        try:
            bot_member = await bot.get_chat_member(chat_id=channel_id, user_id=(await bot.get_me()).id)
            # Проверяем наличие прав администратора
            if bot_member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
                await message.answer("❌ Бот не является администратором в вашем канале. Пожалуйста, добавьте бота и дайте нужные права (особенно: **'Приглашать пользователей'**).")
                await state.clear()
                return
        except Exception:
            # Это может сработать, если канал не найден или приватный, но bot.get_chat уже сработало
            logger.exception(f"Не удалось проверить права бота в канале {channel_username}. Пропуск проверки.")

        # Проверка, не зарегистрирован ли канал уже другим пользователем
        if await db.is_channel_registered_by_other(channel_id, user_id):
            await message.answer("❌ Этот канал уже зарегистрирован другим пользователем.")
            await state.clear()
            return
            
        # --- КЛЮЧЕВОЙ ШАГ: РЕГИСТРАЦИЯ В БД ---
        await db.add_channel(user_id, channel_id, channel_username, chat.title)
        await state.clear()
        
        await message.answer(
            f"✅ Канал <b>{chat.title}</b> ({channel_username}) успешно зарегистрирован!\n\n"
            "Сейчас мы найдем вам первый канал для взаимной подписки..."
        )
        await start_exchange_process(message)

    except TelegramBadRequest as e:
        error_msg = str(e)
        logger.error(f"TelegramBadRequest в process_channel_link: {error_msg}")
        await message.answer(f"❌ Telegram API Ошибка: <b>{error_msg}</b>\n\nУбедитесь, что: \n1. Канал публичный и активен. \n2. Бот имеет все необходимые права администратора.")
    except Exception:
        # **ИЗМЕНЕНИЕ:** Добавляем logger.exception для вывода полного traceback в лог
        logger.exception("Критическая ошибка в process_channel_link (скорее всего, проблема с БД: database.py)")
        await message.answer("Произошла критическая ошибка. Попробуйте снова позже.")

@dp.message()
async def unhandled_message(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    logger.warning(f"Необработанное сообщение от {message.from_user.id}: {message.text}, State: {current_state}")
    if current_state:
        await message.answer(f"Вы находитесь в состоянии **{current_state.split(':')[-1]}**. Пожалуйста, следуйте инструкциям или нажмите /start для сброса.")
    else:
        await message.answer("Я не знаю, что делать с этим сообщением. Начните с команды /start.")

# --- EXCHANGE LOGIC ---

@dp.callback_query(F.data == "start_exchange")
async def start_exchange_process(update_obj: Union[types.CallbackQuery, types.Message]):
    is_callback = isinstance(update_obj, types.CallbackQuery)
    user_id = update_obj.from_user.id

    user_channel_info = await db.get_user_channel_info(user_id)
    
    if not user_channel_info:
        kb = InlineKeyboardBuilder().button(text="➕ Зарегистрировать канал", callback_data="register_channel").as_markup()
        await safe_edit_or_reply(
            update_obj,
            "⚠️ Ваш канал не зарегистрирован.\n\nЧтобы начать обмен, сначала зарегистрируйте свой канал:",
            reply_markup=kb
        )
        if is_callback:
            await update_obj.answer()
        return

    # Получаем целевой канал для подписки
    target_channel_info = await db.get_target_channel(user_id)
    if target_channel_info and all([target_channel_info.get(k) for k in ["channel_id", "link", "title"]]):
        
        target_channel_id = target_channel_info.get("channel_id")
        target_channel_link = target_channel_info.get("link")
        target_channel_title = target_channel_info.get("title")
        
        # Проверяем, не является ли пользователь уже подписчиком
        if await is_member(user_id, target_channel_id):
            await safe_edit_or_reply(
                update_obj,
                f"Вы уже подписаны на канал <b>{target_channel_title}</b>. Ищем следующий канал...",
                reply_markup=get_main_keyboard(is_registered=True)
            )
            if is_callback:
                await update_obj.answer()
            # Рекурсивный вызов для поиска следующего канала
            await start_exchange_process(update_obj) 
            return

        msg = (
            f"✨ Обмен Подписками\n\n"
            f"1. Подпишитесь на этот канал:\n"
            f"Канал: <b>{target_channel_title}</b>\n"
            f"Ссылка: <code>{target_channel_link}</code>\n\n"
            f"После подписки нажмите кнопку 'Подписка оформлена'."
        )
        
        await safe_edit_or_reply(
            update_obj, 
            msg, 
            reply_markup=get_subscription_keyboard(target_channel_link, target_channel_id)
        )
    else:
        if target_channel_info:
             logger.error(f"db.get_target_channel вернул неполные данные: {target_channel_info}")
        
        await safe_edit_or_reply(
            update_obj,
            "😴 Нет доступных каналов для обмена или ошибка в БД. Попробуйте позже.",
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
            await callback.answer("Некорректные данные (ожидается sub_done:ID).", show_alert=True)
            return
        
        # subscribed_channel_id (Канал B) - канал, на который подписался Пользователь A
        subscribed_channel_id = int(parts[1])

        if not await is_member(user_id, subscribed_channel_id):
            await callback.answer("❌ Мы не видим вашей подписки на целевой канал. Попробуйте еще раз.")
            return

        user_channel_info = await db.get_user_channel_info(user_id)
        if not user_channel_info:
            await callback.answer("Ошибка: Ваш канал не найден. Начните с /start.")
            return

        # subscriber_channel_id (Канал A) - канал, который должен получить подписчика
        subscriber_channel_id = user_channel_info.get("channel_id")
        subscriber_channel_link = user_channel_info.get("link")
        subscriber_channel_title = user_channel_info.get("title")

        # Получаем информацию о владельце канала B
        channel_b_owner_info = await db.get_channel_owner_info(subscribed_channel_id)
        if not channel_b_owner_info or not channel_b_owner_info.get("owner_id"):
            logger.error(f"Не найден владелец для канала ID: {subscribed_channel_id}")
            await callback.answer("Ошибка системы: не найден владелец канала B.")
            return

        channel_b_owner_id = channel_b_owner_info.get("owner_id")
        channel_b_title = channel_b_owner_info.get("title") 

        # Транзакция регистрации подписки и создания долга в БД
        new_subs_needed = await db.register_subscription_and_create_debt(
            subscriber_user_id=user_id,
            subscribed_channel_id=subscribed_channel_id,
            subscriber_channel_id=subscriber_channel_id
        )

        # Уведомление владельца канала B
        try:
            builder = InlineKeyboardBuilder()
            valid_url_a = format_link_for_button(subscriber_channel_link)
            builder.button(text=f"1️⃣ Подписаться на {subscriber_channel_title}", url=valid_url_a)
            
            # Формируем callback_data: confirm_reciprocal_sub:owner_b_id:channel_a_id:channel_b_id
            callback_data = f"confirm_reciprocal_sub:{channel_b_owner_id}:{subscriber_channel_id}:{subscribed_channel_id}" 
            
            builder.button(text="2️⃣ Я подписался взаимно", callback_data=callback_data)
            builder.adjust(1)

            await bot.send_message(
                chat_id=channel_b_owner_id,
                text=(
                    f"🎉 НОВАЯ ВЗАИМНАЯ ПОДПИСКА!\n\n"
                    f"На ваш канал <b>{channel_b_title}</b> только что подписался пользователь "
                    f"(@{username_a} / ID: <code>{user_id}</code>).\n\n"
                    f"Канал подрядчика: <b>{subscriber_channel_title}</b> ({subscriber_channel_link})\n\n"
                    "1) Подпишитесь на канал 1️⃣\n2) Нажмите 'Я подписался взаимно' 2️⃣"
                ),
                reply_markup=builder.as_markup()
            )
        except TelegramForbiddenError:
            logger.warning(f"Не удалось уведомить владельца канала B ({channel_b_owner_id}). Чат заблокирован.")
        except Exception:
            logger.exception(f"Не удалось уведомить владельца канала B ({channel_b_owner_id})")

        # Ответ пользователю A
        await safe_edit_or_reply(
            callback,
            "🎉 Подписка засчитана! Ваш канал добавлен в очередь. Вы получите уведомление при завершении обмена.",
            reply_markup=get_main_keyboard(is_registered=True)
        )
        await callback.answer("Подписка успешно засчитана!")

    except ValueError:
        logger.error(f"Критическая ошибка: ValueError при конвертации channel_id. Callback_data: {callback.data}")
        await callback.answer("Критическая ошибка в данных. Попробуйте снова.", show_alert=True)
    except Exception:
        logger.exception("Ошибка в process_subscription_done")
        await callback.answer("Произошла ошибка при обработке подписки.", show_alert=True)


@dp.callback_query(F.data.startswith("confirm_reciprocal_sub:"))
async def process_reciprocal_subscription(callback: types.CallbackQuery):
    try:
        parts = callback.data.split(":")
        
        # Ожидаем 4 части: 'confirm_reciprocal_sub', owner_b_id, channel_a_id, channel_b_id
        if len(parts) != 4:
            await callback.answer("Ошибка данных колбэка.", show_alert=True)
            return

        # owner_b_id - ID владельца, который должен нажать кнопку
        # channel_that_owes_id (Канал A) - канал, который должен получить подписчика
        # subscribed_channel_id (Канал B) - канал, на который изначально подписался Пользователь A
        
        owner_b_id = int(parts[1])
        channel_that_owes_id = int(parts[2])  
        subscribed_channel_id = int(parts[3]) 
        caller_id = callback.from_user.id

        # Проверяем, что тот, кто нажал — действительно владелец B
        if caller_id != owner_b_id:
            await callback.answer("Вы не можете подтверждать этот обмен (вы не владелец канала).", show_alert=True)
            return

        # Проверяем, что владелец B подписался на канал A
        if not await is_member(caller_id, channel_that_owes_id):
            await callback.answer("❌ Мы не видим вашей подписки на целевой канал. Пожалуйста, подпишитесь.")
            return

        # Получаем инфо о канале B (владельца)
        channel_b_info_from_owner = await db.get_channel_info_by_owner_id(owner_b_id)
        if not channel_b_info_from_owner:
            logger.error(f"Не найдена информация о канале B по ID владельца: {owner_b_id}")
            await callback.answer("Ошибка: не удалось найти информацию о вашем канале (B).")
            return

        channel_b_title = channel_b_info_from_owner.get("title")

        # Получаем владельца канала A
        owner_a_info = await db.get_channel_owner_info(channel_that_owes_id)
        if not owner_a_info:
            await callback.answer("Ошибка: Не найден владелец канала A.")
            return

        owner_a_id = owner_a_info.get("owner_id")
        channel_a_title = owner_a_info.get("title")

        # Выполняем транзакцию погашения долга в БД
        new_subs_needed = await db.fulfill_debt(
            subscriber_user_id=caller_id,
            subscribed_channel_id=subscribed_channel_id,
            channel_that_owes_id=channel_that_owes_id 
        )

        # Уведомляем владельца A (того, кто изначально подписался)
        try:
            await bot.send_message(
                chat_id=owner_a_id,
                text=(
                    f"🎉 ВЗАИМНАЯ ПОДПИСКА УСПЕШНА!\n\n"
                    f"На ваш канал <b>{channel_a_title}</b> подписался владелец канала: <b>{channel_b_title}</b>.\n\n"
                    f"Текущий баланс долга: <b>{new_subs_needed}</b>."
                ),
                reply_markup=get_main_keyboard(is_registered=True)
            )
        except Exception:
            logger.exception(f"Не удалось уведомить владельца A ({owner_a_id})")

        # Обновляем сообщение для владельца B (того, кто подтвердил)
        await safe_edit_or_reply(
             callback,
             f"👍 Подтверждение отправлено! Вы успешно завершили взаимную подписку на канал {channel_a_title}."
        )

        await callback.answer("Подписка подтверждена. Долг погашен.")
    except NotFoundError as e:
        logger.error(f"NotFoundError в process_reciprocal_subscription: {e}. Данные: {callback.data}")
        await callback.answer("❌ Активный долг для погашения не найден. Обмен уже завершен или ошибка в БД.", show_alert=True)
    except Exception:
        logger.exception("Ошибка в process_reciprocal_subscription")
        await callback.answer("Произошла ошибка при подтверждении взаимной подписки.", show_alert=True)

@dp.callback_query(F.data == "my_channel_stats")
async def show_my_channel_stats(callback: types.CallbackQuery):
    try:
        user_id = callback.from_user.id
        channel_info = await db.get_user_channel_info(user_id)
        if not channel_info:
            await callback.answer("Ваш канал не зарегистрирован.", show_alert=True)
            return

        # Использование ключей
        link = channel_info.get("link")
        title = channel_info.get("title")
        subs_needed = channel_info.get("subscribers_needed")
        
        text = (
            f"📊 Статистика вашего канала\n\n"
            f"Название: <b>{title}</b>\n"
            f"Ссылка: <code>{link}</code>\n"
            f"Баланс долга: **{subs_needed}**\n\n"
            f"Долг {subs_needed} означает, что столько подписчиков должен получить ваш канал."
        )
        await safe_edit_or_reply(callback, text, reply_markup=get_main_keyboard(is_registered=True))
        await callback.answer()
    except Exception:
        logger.exception("Ошибка в show_my_channel_stats")
        await callback.answer("Произошла ошибка при показе статистики.", show_alert=True)

# --- BACKGROUND TASK ---
async def check_for_unsubs(bot_instance: Bot, db_instance):
    """Фоновая задача проверки отписок и уменьшения долгов."""
    try:
        while True:
            await asyncio.sleep(30 * 60) # Проверка раз в 30 минут
            logger.info("Фоновая проверка отписок...")
            
            try:
                # Получаем активные подписки
                rows = await db_instance._fetch(
                    """
                    SELECT
                        s.id,
                        s.subscriber_user_id,
                        s.subscribed_channel_id,
                        c.owner_id,
                        s.channel_that_owes_id
                    FROM subscriptions s
                    JOIN channels c ON s.subscribed_channel_id = c.channel_id
                    WHERE s.is_active = TRUE
                    LIMIT 50
                    """
                )
            except Exception:
                logger.exception("Ошибка получения подписок из БД")
                continue

            for row in rows:
                sub_id = row.get("id")
                subscriber_id = row.get("subscriber_user_id")
                subscribed_channel_id = row.get("subscribed_channel_id")
                owner_id_of_subscribed = row.get("owner_id")
                channel_that_owes_id = row.get("channel_that_owes_id")
                
                # Защита от потенциальных None/ошибок типов
                if not all([sub_id, subscriber_id, subscribed_channel_id, owner_id_of_subscribed, channel_that_owes_id]):
                    logger.error(f"Пропущенная строка с None-значениями в check_for_unsubs: {row}")
                    continue

                try:
                    # Проверка на отписку
                    if not await is_member(subscriber_id, subscribed_channel_id):
                        logger.warning(f"Обнаружена отписка: sub_id={sub_id}, user={subscriber_id}")
                        
                        # Деактивация подписки
                        # Используем транзакцию или атомарное обновление для деактивации и уменьшения
                        await db_instance._execute("UPDATE subscriptions SET is_active = FALSE WHERE id = %s", sub_id)
                        
                        # Уменьшение долга канала A (если он есть)
                        owes_info = await db_instance.get_channel_owner_info(channel_that_owes_id)
                        owner_of_owes_id = owes_info.get("owner_id") if owes_info else None
                        
                        if owner_of_owes_id:
                            try:
                                # Атомарное уменьшение с защитой от отрицательных значений
                                await db_instance._execute(
                                    "UPDATE channels SET subscribers_needed = GREATEST(subscribers_needed - 1, 0) WHERE channel_id = %s", 
                                    channel_that_owes_id
                                )
                                await bot_instance.send_message(
                                    chat_id=owner_of_owes_id,
                                    text="❌ Ваш долг уменьшен на 1 в связи с аннулированием обмена (обнаружена отписка)."
                                )
                            except TelegramForbiddenError:
                                logger.warning(f"Не удалось уведомить должника об аннулировании. Чат заблокирован {owner_of_owes_id}.")
                            except Exception:
                                logger.exception("Ошибка уменьшения subscribers_needed или уведомления должника")
                                
                            # Уведомление владельца канала B (на который отписались)
                            try:
                                await bot_instance.send_message(
                                    chat_id=owner_id_of_subscribed,
                                    text=f"⚠️ Внимание! Пользователь <code>{subscriber_id}</code> отписался от вашего канала. Обмен аннулирован."
                                )
                            except TelegramForbiddenError:
                                logger.warning(f"Не удалось уведомить владельца канала об отписке. Чат заблокирован {owner_id_of_subscribed}.")
                            except Exception:
                                logger.exception("Не удалось уведомить владельца канала об отписке")
                                
                except Exception:
                    logger.exception(f"Ошибка при обработке строки отписок (sub_id: {sub_id})")
    except asyncio.CancelledError:
        logger.info("Фоновая задача check_for_unsubs отменена.")
    except Exception:
        logger.exception("Фоновая задача завершилась с ошибкой.")

# --- RUN ---
async def main():
    await db.connect()
    bg_task = None
    if db.pool:
        # Запускаем фоновую задачу только после успешного подключения к БД
        bg_task = asyncio.create_task(check_for_unsubs(bot, db))
        logger.info("Запущена фоновая задача check_for_unsubs.")

    logger.info("Бот запущен.")
    try:
        await dp.start_polling(bot)
    finally:
        if bg_task:
            bg_task.cancel()
            with contextlib.suppress(Exception):
                await bg_task
        await db.close()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
