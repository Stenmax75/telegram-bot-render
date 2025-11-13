import asyncio
import logging
import re
import os
import contextlib
# ВАЖНОЕ ИСПРАВЛЕНИЕ #1: Добавлен Dict
from typing import Union, Optional, Any, Dict
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ChatMemberStatus
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
# ИСПРАВЛЕНИЕ ОШИБКИ: Добавлен ReplyKeyboardBuilder в импорт
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from datetime import timedelta

# REDIS / STORAGE
from aiogram.fsm.storage.redis import RedisStorage, Redis
# from aiogram.fsm.storage.memory import MemoryStorage # Не нужен, если используем Redis/Fallback

# Конфиг и БД (убедитесь, что REQUIRED_CHANNEL_ID в config_1 - int)
from config_1 import BOT_TOKEN, REQUIRED_CHANNEL_ID
# ВАЖНОЕ ИСПРАВЛЕНИЕ #2: Добавлен класс Database для тайп хинтов
from database import db, NotFoundError, Database

# --- КОНФИГУРАЦИЯ И ЗАПУСК ---\
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# CONSTANTS
BOT_SUPPORT_CHANNEL_USERNAME = '@life_in_stile' # Переименовано для ясности
BOT_SUPPORT_CHANNEL_URL = f"https://t.me/{BOT_SUPPORT_CHANNEL_USERNAME.lstrip('@')}"
CHECK_INTERVAL_SECONDS = 300 # 5 минут

# Инициализация Redis для FSM
REDIS_DSN = os.getenv("REDIS_DSN") 

if REDIS_DSN:
    # Используем полный URI (DSN) для подключения к Redis (ИСПРАВЛЕНИЕ ОШИБКИ ПОДКЛЮЧЕНИЯ)
    redis_storage = Redis.from_url(REDIS_DSN) 
else:
    # Fallback к старому способу через HOST/PORT или localhost
    redis_storage = Redis(host=os.getenv("REDIS_HOST", "localhost"), port=os.getenv("REDIS_PORT", 6379))
    
storage = RedisStorage(redis_storage)

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=storage)

# --- FSM STATES ---
class ChannelRegistration(StatesGroup):
    waiting_for_channel = State()
    
class UnsubSkip(StatesGroup):
    waiting_for_skip_confirmation = State()
    

# --- KEYBOARDS ---

def get_main_menu_keyboard(has_channels: bool) -> types.ReplyKeyboardMarkup:
    """Генерирует основное меню."""
    # ИСПРАВЛЕНИЕ ОШИБКИ: Используем импортированный ReplyKeyboardBuilder напрямую
    builder = ReplyKeyboardBuilder() 
    builder.button(text="➕ Получить Подписчика")
    builder.button(text="📋 Мои Каналы")
    if has_channels:
        builder.button(text="❌ Удалить Канал")
    builder.adjust(2)
    return builder.as_markup(resize_keyboard=True)

def get_back_button() -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад в Меню", callback_data="main_menu")
    return builder.as_markup()
    
def get_channel_control_keyboard(channels) -> types.InlineKeyboardMarkup:
    """Генерирует клавиатуру для управления каналами."""
    builder = InlineKeyboardBuilder()
    for channel in channels:
        debt = channel['subscribers_needed']
        button_text = f"⚙️ {channel['title']} ({debt}👤)"
        builder.button(text=button_text, callback_data=f"manage_channel_{channel['channel_id']}")
    
    builder.button(text="➕ Зарегистрировать Новый", callback_data="register_new_channel")
    builder.button(text="⬅️ Назад в Меню", callback_data="main_menu")
    builder.adjust(1)
    return builder.as_markup()

def get_sub_keyboard(channel_link: str, channel_id: int) -> types.InlineKeyboardMarkup:
    """Генерирует клавиатуру для подписки."""
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Подписаться", url=channel_link)
    builder.button(text="✅ Проверить подписку", callback_data=f"check_sub_{channel_id}")
    builder.button(text="➡️ Пропустить", callback_data="skip_sub")
    builder.adjust(1, 2)
    return builder.as_markup()


# --- CHECK / VALIDATION HELPERS ---

async def is_member_of_required_channel(bot: Bot, user_id: int) -> bool:
    """Проверяет, подписан ли пользователь на обязательный канал."""
    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL_ID, user_id)
        # Проверяем, что пользователь является членом, а не только был забанен
        return member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]
    except Exception:
        logger.exception("Ошибка при проверке подписки на обязательный канал")
        return False

async def is_bot_admin_in_channel(bot: Bot, chat_id: Union[int, str]) -> bool:
    """Проверяет, является ли бот администратором канала с правами на приглашение."""
    try:
        member = await bot.get_chat_member(chat_id, bot.id)
        if member.status != ChatMemberStatus.ADMINISTRATOR and member.status != ChatMemberStatus.CREATOR:
            return False
        
        # Проверяем ключевые права, если они важны (взаимодействие с API)
        # В данном случае, достаточно быть администратором для get_chat_member и get_chat
        return True
    except TelegramBadRequest as e:
        # Chat not found, user not a member, etc.
        logger.warning(f"Ошибка проверки прав бота в канале {chat_id}: {e}")
        return False
    except Exception as e:
        logger.exception(f"Непредвиденная ошибка проверки прав бота: {e}")
        return False

async def get_channel_info_from_input(bot: Bot, input_text: str) -> Optional[Dict[str, Any]]:
    """Извлекает ID/Username канала и получает его основную информацию."""
    
    # Регулярное выражение для извлечения username или ID
    match = re.search(r'(?:t\.me/|@|t\.me/joinchat/|telegram\.me/joinchat/|t\.me/\+)?([a-zA-Z0-9_]+)$', input_text)
    channel_identifier = None
    if match:
        channel_identifier = match.group(1)
    else:
        # Попытка напрямую использовать текст (если это ID)
        if input_text.startswith('-100') and input_text[1:].isdigit():
             channel_identifier = int(input_text)
        elif input_text.startswith('@'):
             channel_identifier = input_text
        else:
             return None # Некорректный формат
    
    # Добавляем '@' для поиска по username, если его нет и это не число
    if isinstance(channel_identifier, str) and not channel_identifier.startswith('@') and not channel_identifier.isdigit():
        channel_identifier = '@' + channel_identifier
    
    try:
        # Используем get_chat, чтобы получить информацию о канале
        chat = await bot.get_chat(chat_id=channel_identifier)
        
        # Проверяем, что это именно канал (ChatType.CHANNEL) или супергруппа, используемая как канал
        if chat.type not in ['channel', 'supergroup']:
            return None # Не канал
            
        # Формируем ссылку/username
        link = chat.username if chat.username else f"https://t.me/c/{str(chat.id).replace('-100', '')}"
        username = chat.username if chat.username else None
        
        # Проверяем, что пользователь, который регистрирует, является создателем
        member = await bot.get_chat_member(chat.id, chat.id) # Проверка прав самого себя
        if member.status != ChatMemberStatus.CREATOR and member.status != ChatMemberStatus.ADMINISTRATOR:
            # Требуем, чтобы бот был админом
            return None 

        return {
            'channel_id': chat.id,
            'link': f"https://t.me/{link.lstrip('@')}" if link.startswith('@') else link,
            'username': username,
            'title': chat.title
        }
    except TelegramBadRequest as e:
        logger.warning(f"Ошибка Telegram при получении info о канале {channel_identifier}: {e.message}")
        return None
    except Exception as e:
        logger.exception(f"Непредвиденная ошибка при получении info о канале: {e}")
        return None


# --- HANDLERS ---

@dp.message(Command("start"))
async def start_command(message: types.Message, state: FSMContext):
    await state.clear()
    await db.add_user(message.from_user.id, message.from_user.username)
    
    # Проверка подписки на обязательный канал
    if not await is_member_of_required_channel(bot, message.from_user.id):
        await message.answer(
            f"Для начала работы необходимо подписаться на наш основной канал: [Канал поддержки]({BOT_SUPPORT_CHANNEL_URL}).\n\n"
            "После подписки нажмите /start снова.",
            reply_markup=types.ReplyKeyboardRemove()
        )
        return

    # Получаем каналы пользователя для отображения меню
    user_channels = await db.get_user_channels_info(message.from_user.id)
    has_channels = len(user_channels) > 0
    
    await message.answer(
        "👋 Добро пожаловать! Выберите действие в меню:",
        reply_markup=get_main_menu_keyboard(has_channels)
    )

# Обработка кнопки "Назад в Меню"
@dp.callback_query(F.data == "main_menu")
async def process_main_menu_callback(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()

    user_channels = await db.get_user_channels_info(callback.from_user.id)
    has_channels = len(user_channels) > 0
    
    await callback.message.edit_text(
        "👋 Выберите действие в меню:",
        reply_markup=None # Удаляем инлайн-клавиатуру, если она была
    )
    # Отправляем новое сообщение с основным меню (ReplyKeyboard)
    await callback.message.answer(
        "Главное меню:",
        reply_markup=get_main_menu_keyboard(has_channels)
    )

# --- РЕГИСТРАЦИЯ КАНАЛА ---

@dp.message(F.text == "📋 Мои Каналы")
@dp.callback_query(F.data == "register_new_channel")
async def show_my_channels(target_update: Union[types.Message, types.CallbackQuery], state: FSMContext):
    
    user_id = target_update.from_user.id
    
    if isinstance(target_update, types.CallbackQuery):
        await target_update.answer()

    # Проверка подписки на обязательный канал
    if not await is_member_of_required_channel(bot, user_id):
        text = (
            f"Для работы необходимо подписаться на наш основной канал: [Канал поддержки]({BOT_SUPPORT_CHANNEL_URL}).\n\n"
            "После подписки нажмите /start снова."
        )
        if isinstance(target_update, types.CallbackQuery):
             await target_update.message.edit_text(text, reply_markup=get_back_button())
        else:
            await target_update.answer(text, reply_markup=get_back_button())
        return


    channels = await db.get_user_channels_info(user_id)
    count = len(channels)
    
    text = f"🗂️ **Ваши зарегистрированные каналы ({count})**:\n"
    
    if count == 0:
        text += "У вас пока нет зарегистрированных каналов. Нажмите '➕ Зарегистрировать Новый' ниже."
        keyboard = get_channel_control_keyboard(channels)
    else:
        text += "Выберите канал для управления или нажмите '➕ Зарегистрировать Новый':"
        keyboard = get_channel_control_keyboard(channels)
    
    if isinstance(target_update, types.CallbackQuery):
        await target_update.message.edit_text(text, reply_markup=keyboard)
    else:
        await target_update.answer(text, reply_markup=keyboard)
        

@dp.message(F.text.in_(["➕ Зарегистрировать Новый", "❌ Удалить Канал"]))
async def start_registering_channel(message: types.Message, state: FSMContext):
    await state.clear()
    
    # Проверка подписки на обязательный канал
    if not await is_member_of_required_channel(bot, message.from_user.id):
        await message.answer(
            f"Для работы необходимо подписаться на наш основной канал: [Канал поддержки]({BOT_SUPPORT_CHANNEL_URL}).\n\n"
            "После подписки нажмите /start снова.",
            reply_markup=get_back_button()
        )
        return

    await message.answer(
        "Отправьте мне ссылку (@username) или публичную ссылку-приглашение на ваш **публичный** канал.\n\n"
        "**Важно:** Бот должен быть администратором в этом канале.",
        reply_markup=get_back_button()
    )
    await state.set_state(ChannelRegistration.waiting_for_channel)


@dp.message(ChannelRegistration.waiting_for_channel)
async def process_channel_id_or_username(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    input_text = message.text
    
    await state.clear() # Снимаем состояние

    # 1. Проверка формата и получение информации о канале
    channel_info = await get_channel_info_from_input(bot, input_text)
    
    if channel_info is None:
        await message.answer(
            "❌ Некорректный формат ссылки/username или бот не является администратором в канале. Попробуйте снова или нажмите '⬅️ Назад в Меню'.",
            reply_markup=get_back_button()
        )
        await state.set_state(ChannelRegistration.waiting_for_channel)
        return

    channel_id = channel_info['channel_id']
    channel_title = channel_info['title']
    channel_link = channel_info['link']
    
    # 2. Проверка, не зарегистрирован ли канал другим пользователем
    if await db.is_channel_registered_by_other(channel_id, user_id):
        await message.answer(
            "❌ Этот канал уже зарегистрирован другим пользователем. Если это ошибка, свяжитесь с поддержкой.",
            reply_markup=get_back_button()
        )
        return
        
    # 3. Проверка, не зарегистрирован ли канал уже этим пользователем
    existing_channel = await db.get_channel_info_by_channel_id(channel_id)
    if existing_channel:
        # Проверка owner_id не нужна, т.к. is_channel_registered_by_other уже проверил. 
        # Если канал найден, значит, он уже зарегистрирован этим пользователем
        await message.answer(
            f"⚠️ Канал **{channel_title}** уже был вами зарегистрирован.\n"
            "Для просмотра ваших каналов нажмите '📋 Мои Каналы'.",
            reply_markup=get_main_menu_keyboard(True)
        )
        return

    # 4. Регистрация канала
    if await db.add_channel(user_id, channel_id, channel_link, channel_title):
        await message.answer(
            f"✅ Канал **{channel_title}** успешно добавлен в систему!\n"
            "Вы можете добавить еще каналов, или нажать '➕ Получить Подписчика', чтобы начать работу.",
            reply_markup=get_main_menu_keyboard(True)
        )
    else:
        await message.answer(
            "❌ Произошла ошибка при регистрации канала в базе данных. Попробуйте позже.",
            reply_markup=get_back_button()
        )

# --- ПОЛУЧЕНИЕ ПОДПИСКИ (ОСНОВНОЙ ФУНКЦИОНАЛ) ---

@dp.message(F.text == "➕ Получить Подписчика")
async def start_get_sub(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    
    # 0. Проверка подписки на обязательный канал
    if not await is_member_of_required_channel(bot, user_id):
        await message.answer(
            f"Для работы необходимо подписаться на наш основной канал: [Канал поддержки]({BOT_SUPPORT_CHANNEL_URL}).",
            reply_markup=get_back_button()
        )
        return
        
    # 1. Проверка наличия зарегистрированных каналов у пользователя
    user_channels = await db.get_user_channels_info(user_id)
    if not user_channels:
        await message.answer(
            "❌ У вас нет зарегистрированных каналов. Зарегистрируйте канал, нажав '📋 Мои Каналы'.",
            reply_markup=get_main_menu_keyboard(False)
        )
        return
        
    # 2. Выбор канала, который получит "долг" (Канал A)
    channel_to_receive_sub = await db.get_user_channel_with_highest_debt(user_id)
    if not channel_to_receive_sub:
         await message.answer(
            "❌ Не удалось определить ваш канал, который должен получить подписчика. Пожалуйста, обратитесь в поддержку.",
            reply_markup=get_back_button()
        )
         return
    
    # 3. Получение следующего канала для подписки (Канал B)
    # Используем данные из FSM, чтобы не предлагать каналы, которые уже пропустили
    state_data = await state.get_data()
    excluded_ids = state_data.get('excluded_channel_ids', [])
    
    target_channel = await db.get_target_channel(user_id, excluded_ids)
    
    if not target_channel:
        await message.answer(
            "🎉 Поздравляем! Вы подписались на все доступные каналы.\n"
            "Подождите, пока в системе появятся новые каналы.",
            reply_markup=get_main_menu_keyboard(True)
        )
        await state.clear()
        return

    # Сохраняем информацию о текущей задаче
    await state.update_data(
        target_channel_id=target_channel['channel_id'],
        channel_to_receive_sub_id=channel_to_receive_sub['channel_id']
    )
    
    # 4. Отправка предложения
    await message.answer(
        f"➡️ **Ваше задание:**\n"
        f"Подпишитесь на канал **{target_channel['title']}** (долг: {target_channel['subscribers_needed']}👤).\n\n"
        f"После проверки подписки, ваш канал **{channel_to_receive_sub['title']}** получит +1 в очередь на подписку (долг: {channel_to_receive_sub['subscribers_needed']}👤).",
        reply_markup=get_sub_keyboard(target_channel['link'], target_channel['channel_id'])
    )

@dp.callback_query(F.data.startswith("check_sub_"))
async def check_subscription(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    target_channel_id = int(callback.data.split('_')[2])
    
    state_data = await state.get_data()
    # Проверяем, что ID из кнопки совпадает с ID в состоянии
    if state_data.get('target_channel_id') != target_channel_id:
        await callback.answer("Ошибка: Проверьте, что вы нажали кнопку для текущей задачи.", show_alert=True)
        # Отправляем в меню для сброса
        await callback.message.answer("⚠️ Ваша текущая задача устарела. Нажмите '➕ Получить Подписчика' снова.", reply_markup=get_main_menu_keyboard(True))
        await state.clear()
        return
        
    await callback.answer("Проверяю подписку...", show_alert=False)
    
    # 1. Проверка подписки
    is_subscribed = False
    # ПЕРЕОПРЕДЕЛЕНИЕ: Проверяем подписку на целевой канал
    try:
        member = await bot.get_chat_member(target_channel_id, user_id)
        is_subscribed = member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]
    except Exception:
        logger.exception(f"Ошибка при проверке подписки пользователя {user_id} на канал {target_channel_id}")
        is_subscribed = False

    if not is_subscribed:
        await callback.message.edit_text(
            "❌ Подписка не найдена. Пожалуйста, подпишитесь и повторите проверку.",
            reply_markup=callback.message.reply_markup
        )
        return

    # 2. Подписка подтверждена, регистрируем "долг"
    channel_that_owes_id = state_data['channel_to_receive_sub_id']
    
    try:
        new_debt = await db.register_subscription_and_create_debt(
            subscriber_user_id=user_id, 
            subscribed_channel_id=target_channel_id, 
            channel_that_owes_id=channel_that_owes_id
        )
    except NotFoundError:
        # Это может случиться, если канал пользователя был удален после выбора задачи.
        await callback.message.edit_text(
            "❌ Ошибка! Ваш канал-получатель подписки не найден. Нажмите '📋 Мои Каналы' для проверки.",
            reply_markup=get_back_button()
        )
        return
    except Exception:
        logger.exception("Критическая ошибка при регистрации долга")
        await callback.message.edit_text(
            "❌ Критическая ошибка базы данных при регистрации подписки. Повторите попытку.",
            reply_markup=get_back_button()
        )
        return
        
    await state.clear() # Задача выполнена, сбрасываем состояние

    # 3. Уведомление об успехе
    await callback.message.edit_text(
        f"✅ **Поздравляем!** Ваша подписка на канал {target_channel_id} успешно засчитана.\n\n"
        f"Ваш канал (ID: {channel_that_owes_id}) теперь в очереди на получение **+1** подписчика (Текущий долг: **{new_debt}**👤).",
        reply_markup=get_main_menu_keyboard(True)
    )

@dp.callback_query(F.data == "skip_sub")
async def skip_subscription(callback: types.CallbackQuery, state: FSMContext):
    
    state_data = await state.get_data()
    target_channel_id = state_data.get('target_channel_id')
    
    if target_channel_id is None:
        await callback.answer("Ошибка: Задача для пропуска не найдена.", show_alert=True)
        return
        
    # Добавляем канал в список пропущенных ID
    excluded_ids = state_data.get('excluded_channel_ids', [])
    if target_channel_id not in excluded_ids:
        excluded_ids.append(target_channel_id)
        await state.update_data(excluded_channel_ids=excluded_ids)
    
    # Отправляем сообщение об успешном пропуске и сразу предлагаем новый
    await callback.answer("Канал пропущен.", show_alert=False)
    await callback.message.delete()
    
    # Вызываем функцию получения следующей подписки, используя текущее состояние с исключениями
    await start_get_sub(callback.message, state)


# --- ФОНОВАЯ ПРОВЕРКА ОТПИСОК (TASK) ---
async def check_for_unsubs(bot: Bot, db: Database):
    """Фоновая задача для проверки активных подписок на отписку."""
    while True:
        await asyncio.sleep(CHECK_INTERVAL_SECONDS) # Проверка раз в N секунд
        
        logger.info("Запуск фоновой проверки подписок...")

        try:
            # Получаем список активных подписок для проверки
            subscriptions_to_check = await db.get_active_subscriptions_to_check(limit=50)
            
            if not subscriptions_to_check:
                logger.info("Нет активных подписок для проверки.")
                continue

            for sub in subscriptions_to_check:
                sub_id = sub['id']
                subscriber_user_id = sub['subscriber_user_id']
                subscribed_channel_id = sub['subscribed_channel_id']
                channel_that_owes_id = sub['channel_that_owes_id']
                owner_id_of_subscribed = sub['owner_id'] # Владелец канала, на который подписались

                try:
                    # 1. Проверяем подписку
                    is_subscribed = False
                    try:
                        member = await bot.get_chat_member(subscribed_channel_id, subscriber_user_id)
                        is_subscribed = member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]
                    except TelegramBadRequest:
                        # Канал не найден, или пользователь заблокирован. Считаем как отписку
                        logger.warning(f"Ошибка при проверке: Канал {subscribed_channel_id} не найден. Считаем отпиской.")
                        is_subscribed = False
                    except Exception:
                        logger.exception(f"Ошибка при проверке подписки (sub_id: {sub_id})")
                        continue # Пропускаем эту строку и переходим к следующей

                    # 2. Если отписался
                    if not is_subscribed:
                        logger.warning(f"Обнаружена отписка! user: {subscriber_user_id}, channel: {subscribed_channel_id}")
                        
                        # 3. Погашаем "долг" (уменьшаем subscribers_needed)
                        new_debt = await db.fulfill_debt(channel_that_owes_id)

                        # 4. Уведомление пользователя, который отписался
                        try:
                            await bot.send_message(
                                chat_id=subscriber_user_id,
                                text=f"⚠️ **Внимание!** Обнаружена ваша отписка от канала {subscribed_channel_id}.\n"
                                     f"В качестве штрафа, ваш канал (ID: {channel_that_owes_id}) потерял 1 место в очереди. "
                                     f"Текущий долг: **{new_debt}**👤."
                            )
                        except TelegramForbiddenError:
                            logger.warning(f"Не удалось уведомить подписчика об отписке. Чат заблокирован {subscriber_user_id}.")
                        except Exception:
                            logger.exception("Не удалось уведомить подписчика об отписке")

                        # 5. Уведомление владельца, от которого отписались (владелец subscribed_channel_id)
                        try:
                            await bot.send_message(
                                chat_id=owner_id_of_subscribed,
                                text=f"🔔 **Хорошая новость!** Один из подписчиков, которого вы получили по обмену, "
                                     f"отписался от канала {subscribed_channel_id}.\n"
                                     f"Его 'долг' был аннулирован."
                            )
                        except TelegramForbiddenError:
                            logger.warning(f"Не удалось уведомить владельца канала об отписке. Чат заблокирован {owner_id_of_subscribed}.")
                        except Exception:
                            logger.exception("Не удалось уведомить владельца канала об отписке")
                                
                except Exception:
                    logger.exception(f"Ошибка при обработке строки отписок (sub_id: {sub_id})")
        except asyncio.CancelledError:
            logger.info("Фоновая задача check_for_unsubs отменена.")
            break # Выход из цикла при отмене
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
    # Логика запуска (остается прежней)
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
